"""write_scan_gate.py
Codex PostToolUse gate that closes the shell-write blind spot (global, standalone).

The per-language gates only see edit-tool calls (apply_patch/Edit/Write/...),
so files written by shell commands - `exec_command` running python scripts,
PowerShell here-strings, compilers - were never checked. This gate registers
against `exec_command` and detects what the command actually changed on disk:

  1. snapshot-diff the session workspace for watched source files
     (py/rs/ps1+cmd/m/cpp/js+ts/html) - added or (size, mtime) changed;
  2. delegate each changed file to its sibling gate script in this directory
     via the standard hook payload (their advisories are file-derived and
     deterministic as usual);
  3. deliver the combined advisory once.

On a workspace's first sighting the snapshot is baselined; only files modified
in the last 15 minutes are checked then (catches in-flight work without
dumping a backlog of every pre-existing finding). Steady state is exact:
unchanged files are never re-checked, so an identical file state yields an
identical advisory and the injected context prefix stays prefill-cache
friendly.

Deliberately standalone: stdlib only. The sibling gates are invoked as
subprocesses with a synthesized payload - they stay standalone too.

Kill switches:
  WRITE_SCAN_GATE=0     env var, disables the gate entirely
  .edit-gate-disable    marker file in a project: scan skipped there (audit only)

CLI:
  write_scan_gate.py --force DIR   check every watched file under DIR once,
                                   ignoring the snapshot (retro-check)

Audit log: ~/.codex/logs/write_scan_gate.jsonl
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HARD_CAP_SECONDS = 45.0
DELEGATE_TIMEOUT = 30.0
MAX_FILES_PER_RUN = 8
MAX_WALKED_FILES = 60_000
ADVISORY_MAX_CHARS = 1600
DEDUPE_TTL_SECONDS = 6 * 3600
FIRST_SEEN_RECENT_SECONDS = 15 * 60

# suffix -> sibling gate script
GATE_BY_SUFFIX = {
    ".py": "py_edit_gate.py",
    ".rs": "rs_edit_gate.py",
    ".ps1": "win_edit_gate.py",
    ".psm1": "win_edit_gate.py",
    ".psd1": "win_edit_gate.py",
    ".cmd": "win_edit_gate.py",
    ".bat": "win_edit_gate.py",
    ".m": "ml_edit_gate.py",
    ".c": "cpp_edit_gate.py",
    ".cc": "cpp_edit_gate.py",
    ".cpp": "cpp_edit_gate.py",
    ".cxx": "cpp_edit_gate.py",
    ".h": "cpp_edit_gate.py",
    ".hh": "cpp_edit_gate.py",
    ".hpp": "cpp_edit_gate.py",
    ".hxx": "cpp_edit_gate.py",
    ".js": "js_edit_gate.py",
    ".mjs": "js_edit_gate.py",
    ".cjs": "js_edit_gate.py",
    ".jsx": "js_edit_gate.py",
    ".ts": "js_edit_gate.py",
    ".mts": "js_edit_gate.py",
    ".cts": "js_edit_gate.py",
    ".tsx": "js_edit_gate.py",
    ".html": "html_edit_gate.py",
    ".htm": "html_edit_gate.py",
    ".xhtml": "html_edit_gate.py",
}
WATCHED_SUFFIXES = set(GATE_BY_SUFFIX)

IGNORE_DIR_NAMES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "virtualenv",
    "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".tox", ".eggs", "build", "dist", "site-packages",
    ".codex", ".idea", ".vscode", "target", "appdata",
}


# ── Logging / state ──────────────────────────────────────────────────────────
def _log_dir() -> Path:
    override = os.environ.get("WRITE_SCAN_GATE_LOG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".codex" / "logs"


def _rotate(path: Path, limit: int = 1_000_000) -> None:
    try:
        if path.exists() and path.stat().st_size > limit:
            older = path.with_suffix(path.suffix + ".1")
            if older.exists():
                older.unlink()
            path.rename(older)
    except OSError:
        pass


def audit(record: dict) -> None:
    try:
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "write_scan_gate.jsonl"
        _rotate(log)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _state_path() -> Path:
    return _log_dir() / "write_scan_gate.state.json"


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        cutoff = time.time() - DEDUPE_TTL_SECONDS
        pruned = {}
        for key, value in state.items():
            if key == "snapshots" and isinstance(value, dict):
                pruned[key] = value  # bounded separately in scan()
            elif isinstance(value, (int, float)) and value > cutoff:
                pruned[key] = value
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = _state_path()
        _rotate(path, 4_000_000)
        path.write_text(json.dumps(pruned), encoding="utf-8")
    except Exception:
        pass


def _sha1(text) -> str:
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:16]


def _now() -> float:
    return time.monotonic()


# ── Snapshot diff ────────────────────────────────────────────────────────────
def walk_watched(root: Path) -> dict[str, list[int]]:
    """relpath -> [size, mtime_ns] for watched source files under root."""
    found: dict[str, list[int]] = {}
    # Prune by dir NAME during the walk only - never by the root's own path
    # parts, or a workspace living under AppData\\... would be blind entirely.
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if len(found) >= MAX_WALKED_FILES:
                        return found
                    name = entry.name
                    if entry.is_dir(follow_symlinks=False):
                        if name.lower() not in IGNORE_DIR_NAMES:
                            stack.append(Path(entry.path))
                        continue
                    if Path(name).suffix.lower() not in WATCHED_SUFFIXES:
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    found[str(Path(entry.path).relative_to(root)).lower()] = [
                        st.st_size,
                        st.st_mtime_ns,
                    ]
        except OSError:
            continue
    return found


def changed_files(root: Path, state: dict) -> tuple[list[Path], int]:
    """(changed files newest-first-ish, walked count); mutates state snapshot."""
    key = str(root).lower()
    snapshots = state.setdefault("snapshots", {})
    previous = snapshots.get(key)
    current = walk_watched(root)
    if not isinstance(previous, dict):
        # First sighting: baseline. Check only in-flight work (fresh mtime).
        cutoff_ns = (time.time() - FIRST_SEEN_RECENT_SECONDS) * 1e9
        changed = [
            root / rel
            for rel, meta in current.items()
            if meta[1] >= cutoff_ns and not (root / rel).is_symlink()
        ]
    else:
        changed = [
            root / rel
            for rel, meta in current.items()
            if previous.get(rel) != meta and not (root / rel).is_symlink()
        ]
    snapshots[key] = current
    changed.sort()
    return changed, len(current)


# ── Delegation to the sibling gates ──────────────────────────────────────────
def delegate(path: Path, cwd: Path, deadline: float) -> str:
    """Run the language gate for one file; returns its advisory (may be '')."""
    script = Path(__file__).parent / GATE_BY_SUFFIX[path.suffix.lower()]
    if not script.is_file():
        return ""
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "exec_command",
            "cwd": str(cwd),
            "tool_input": {"file_path": str(path)},
        }
    )
    remaining = deadline - _now()
    if remaining <= 2.0:
        return ""
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=min(DELEGATE_TIMEOUT, remaining),
            check=False,
        )
    except Exception:
        return ""
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
        return str((data.get("hookSpecificOutput") or {}).get("additionalContext") or "")
    except Exception:
        return ""


def compose(changed: list[Path], advisories: list[str]) -> str:
    blocks: list[str] = []
    names = " | ".join(p.name for p in changed[:MAX_FILES_PER_RUN])
    if any(advisories):
        blocks.append(f"[write-scan] shell-written files checked: {names}")
        blocks.extend(a for a in advisories if a)
    if not blocks:
        return ""
    text = "\n".join(blocks)
    return text if len(text) <= ADVISORY_MAX_CHARS else text[: ADVISORY_MAX_CHARS - 3] + "..."


# ── Main ────────────────────────────────────────────────────────────────────
def check_paths(paths: list[Path], cwd: Path, started: float) -> tuple[str, list[str]]:
    deadline = started + HARD_CAP_SECONDS
    advisories: list[str] = []
    checked: list[str] = []
    for path in paths[:MAX_FILES_PER_RUN]:
        checked.append(str(path))
        advisory = delegate(path, cwd, deadline)
        if advisory:
            advisories.append(advisory)
    return compose(paths, advisories), checked


def main(argv: list[str]) -> int:
    started = _now()
    if os.environ.get("WRITE_SCAN_GATE", "1").strip().lower() in ("0", "off", "false"):
        return 0

    force_root: Path | None = None
    if len(argv) >= 2 and argv[0] == "--force":
        force_root = Path(argv[1])

    if force_root is None:
        raw = sys.stdin.buffer.read()
        if not raw.strip():
            return 0
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return 0
        if not isinstance(payload, dict):
            return 0
        cwd = Path(str(payload.get("cwd") or os.getcwd()))
    else:
        cwd = force_root

    try:
        cwd = cwd.resolve()
    except OSError:
        return 0
    if not cwd.is_dir():
        return 0

    state = _load_state()
    skipped: list[str] = []
    if force_root is not None:
        snapshot, walked = walk_watched(cwd), 0
        paths = sorted(cwd / rel for rel in snapshot)
    else:
        marker = cwd / ".edit-gate-disable"
        parent_marker = any(
            (p / ".edit-gate-disable").exists() for p in list(cwd.parents)[:3]
        )
        if marker.exists() or parent_marker:
            audit({"ts": round(time.time(), 2), "cwd": str(cwd),
                   "skipped": "scan skipped (.edit-gate-disable)"})
            return 0
        paths, walked = changed_files(cwd, state)

    advisory, checked = check_paths(paths, cwd, started)
    digest = _sha1(str(cwd) + "|".join(checked) + advisory)
    repeated = state.get(digest, 0) > time.time() - DEDUPE_TTL_SECONDS
    state[digest] = time.time()
    if repeated:
        advisory = ""
    audit(
        {
            "ts": round(time.time(), 2),
            "event": "PostToolUse" if force_root is None else "force",
            "tool": "exec_command",
            "cwd": str(cwd),
            "walked": walked,
            "changed": [str(p) for p in paths],
            "checked": checked,
            "advisory_chars": len(advisory),
            "skipped": skipped,
            "ms": int((_now() - started) * 1000),
        },
    )
    _save_state(state)
    if advisory:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": advisory,
                },
                "systemMessage": advisory.split("\n")[0][:200],
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # fail open, always
        audit({"error": f"{type(exc).__name__}: {exc}"})
        sys.exit(0)

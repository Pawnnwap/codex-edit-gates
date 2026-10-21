"""cpp_edit_gate.py
Codex PostToolUse gate for C/C++ edits (global, standalone).

Registered as a lifecycle hook in ~/.codex/config.toml, so every Codex session
on this machine gets it. Reads the hook payload on stdin, finds the C/C++ files
the agent just touched, then:

  tier 1  encoding   byte-level classification: UTF-16 (no compiler reads it),
                     non-ASCII in BOM-less text (MSVC falls back to the ANSI
                     codepage and garbles string literals unless /utf-8 or a
                     BOM is used), GBK-only (breaks off this CP936 box), bytes
                     valid in no encoding at all. A UTF-8 BOM is deliberately
                     NOT flagged: MSVC needs it to detect UTF-8.
  tier 2  syntax     clang++ -fsyntax-only with the file's own directory on the
                     include path. Only parse-class errors are reported
                     (expected/extraneous/unbalanced/...); semantic errors are
                     suppressed on purpose because they false-positive without
                     project context ("use of undeclared identifier" for a
                     sibling header's function is not a defect). A missing
                     project header voids the run: one deterministic note
                     replaces the findings.

The advisory contains only file-derived, deterministic findings, so an
identical file always yields an identical message - which keeps the injected
context prefix stable and friendly to prefill caching. Tool availability,
timeouts, and wall-clock notes go to the audit log instead. The gate never
blocks a tool call and fails open: any internal error means exit 0 with no
output.

Deliberately standalone: stdlib only, imports nothing from this repo.

Kill switches:
  CPP_EDIT_GATE=0        env var, disables the gate entirely
  .edit-gate-disable     marker file in a project: advisory-only there
                         (encoding still reports, no clang run)

Audit log: ~/.codex/logs/cpp_edit_gate.jsonl
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Budgets. clang++ -fsyntax-only is sub-second for small TUs but can crawl on
# heavy include graphs; the per-file timeout and the run deadline cap that.
HARD_CAP_SECONDS = 12.0
CLANG_TIMEOUT = 8.0
MAX_FILE_BYTES = 400_000
ADVISORY_MAX_CHARS = 1200
MAX_ITEMS_PER_SECTION = 8
DEDUPE_TTL_SECONDS = 6 * 3600
DISCOVERY_TTL_SECONDS = 3600

C_SUFFIXES = {".c"}
CPP_SUFFIXES = {".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
WATCHED_SUFFIXES = C_SUFFIXES | CPP_SUFFIXES

IGNORE_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "virtualenv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".eggs",
    "build",
    "dist",
    "site-packages",
    ".codex",
    ".idea",
    ".vscode",
}

# clang diagnostic line: "<path>:<line>:<col>: error: <message>" (plus the
# "fatal error: ..." variant). note:/warning: lines never match.
_CLANG_LINE_RE = re.compile(
    r"^(?P<loc>.*?):(?P<line>\d+):(?P<col>\d+):\s+(?:fatal\s+)?error:\s+(?P<msg>.+)$"
)
_MISSING_HEADER_RE = re.compile(r"fatal error: '(.+?)' file not found")
# Parse-class errors only. Semantic diagnostics (undeclared identifier, no
# matching function, redefinition, ...) are excluded on purpose: without the
# project's real include graph they are noise, and noise makes the model
# rewrite working code.
_PARSE_ERROR_RE = re.compile(
    r"expected|extraneous|unbalanced|unclosed|unterminated|stray"
    r"|missing|invalid suffix|character too large|unimplemented",
    re.I,
)


# ── Logging / state ──────────────────────────────────────────────────────────
def _log_dir() -> Path:
    override = os.environ.get("CPP_EDIT_GATE_LOG_DIR", "").strip()
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
    """Append one JSONL record. Never raises - the log is diagnostic only."""
    try:
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "cpp_edit_gate.jsonl"
        _rotate(log)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _state_path() -> Path:
    return _log_dir() / "cpp_edit_gate.state.json"


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        cutoff = time.time() - DEDUPE_TTL_SECONDS
        pruned = {
            k: v
            for k, v in state.items()
            if isinstance(v, (int, float)) and v > cutoff
        }
        kept = sorted(pruned.items(), key=lambda kv: kv[1])[-500:]
        tools = state.get("tools")
        if isinstance(tools, dict):
            kept.append(("tools", tools))
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = _state_path()
        _rotate(path, 400_000)
        path.write_text(json.dumps(dict(kept)), encoding="utf-8")
    except Exception:
        pass


def _sha1(text) -> str:
    if isinstance(text, bytes):
        data = text
    else:
        data = str(text).encode("utf-8", "replace")
    return hashlib.sha1(data).hexdigest()[:16]


def _now() -> float:
    return time.monotonic()


def _is_ignored(path: Path) -> bool:
    return bool({p.lower() for p in path.parts} & IGNORE_DIR_NAMES)


def syntax_disabled_here(path: Path) -> bool:
    """A project opts out of heavyweight runs with a .edit-gate-disable marker."""
    try:
        for parent in [path.parent, *list(path.parents)[:6]]:
            if (parent / ".edit-gate-disable").exists():
                return True
            if (parent / ".git").exists():
                break
    except OSError:
        pass
    return False


# ── Payload parsing ─────────────────────────────────────────────────────────
_PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$",
    re.MULTILINE,
)


def parse_patch_paths(text: str) -> list[str]:
    """Paths from an apply_patch body ('*** Update File: <path>')."""
    return [m.group(1) for m in _PATCH_FILE_RE.finditer(text) if m.group(1).strip()]


def extract_targets(payload: dict, cwd: Path) -> list[Path]:
    """Existing C/C++ files the payload refers to (patch headers + path keys)."""
    raw: list[str] = []

    def scan(value) -> None:
        if isinstance(value, str):
            raw.extend(parse_patch_paths(value))
        elif isinstance(value, dict):
            for key, sub in value.items():
                if key in (
                    "file_path",
                    "path",
                    "filePath",
                    "target_file",
                    "notebook_path",
                ):
                    if isinstance(sub, str):
                        raw.append(sub)
                else:
                    scan(sub)
        elif isinstance(value, list):
            for item in value:
                scan(item)

    scan(payload.get("tool_input"))

    out: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        cleaned = item.strip().strip('"').strip("'")
        if Path(cleaned).suffix.lower() not in WATCHED_SUFFIXES:
            continue
        candidate = Path(cleaned)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def skip_reason(path: Path) -> str:
    """'' when the file should be checked, else why it is skipped."""
    try:
        if not path.is_file():
            return "not a file"
        if _is_ignored(path):
            return "ignored dir"
        size = path.stat().st_size
        if size == 0:
            return "empty"
        if size > MAX_FILE_BYTES:
            return f"too large ({size} B)"
    except OSError as exc:
        return f"oserror: {exc}"
    return ""


# ── Pure checks (no subprocess, unit-testable) ──────────────────────────────
def encoding_findings(blob: bytes, path: Path) -> list[str]:
    """Encoding traps for C/C++ on this box (MSVC + CP936 + clang)."""
    name = path.name
    findings: list[str] = []
    if blob.startswith(b"\xff\xfe") or blob.startswith(b"\xfe\xff"):
        findings.append(
            f"{name}: UTF-16 file - no compiler reads it, save as UTF-8"
        )
        return findings
    # A UTF-8 BOM is fine for C++ (MSVC uses it to detect UTF-8) - no finding.
    try:
        blob.decode("utf-8")
        utf8_ok = True
    except UnicodeDecodeError:
        utf8_ok = False
    try:
        blob.decode("gbk")
        gbk_ok = True
    except UnicodeDecodeError:
        gbk_ok = False
    if not utf8_ok and not gbk_ok:
        findings.append(
            f"{name}: bytes valid in neither UTF-8 nor GBK - the file mojibakes"
            " under every reader"
        )
    elif not utf8_ok and gbk_ok:
        findings.append(
            f"{name}: GBK/CP936 only - MSVC reads it via the system codepage"
            " here; it breaks with /utf-8 or on any non-CP936 machine, save as"
            " UTF-8"
        )
    else:
        has_non_ascii = any(byte > 127 for byte in blob)
        has_bom = blob.startswith(b"\xef\xbb\xbf")
        if has_non_ascii and not has_bom:
            findings.append(
                f"{name}: non-ASCII in BOM-less UTF-8 - MSVC reads it as the"
                " ANSI codepage and garbles string literals; add a BOM or"
                " compile with /utf-8"
            )
    return findings


def parse_clang_output(stdout: str) -> tuple[list[str], list[str]]:
    """clang diagnostics -> (parse errors 'L..C.. msg', missing headers)."""
    errors: list[str] = []
    missing: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        header = _MISSING_HEADER_RE.search(line)
        if header:
            name = header.group(1)
            if name not in missing:
                missing.append(name)
            continue
        match = _CLANG_LINE_RE.match(line)
        if not match:
            continue
        if not _PARSE_ERROR_RE.search(match.group("msg")):
            continue  # semantic-class noise without project context
        errors.append(
            f"L{match.group('line')}C{match.group('col')} {match.group('msg')}"
        )
    return errors, missing


# ── Tool discovery ──────────────────────────────────────────────────────────
CLANG_GLOBS = [
    r"C:\Program Files\LLVM\bin\clang++.exe",
]


def _run(cmd: list, timeout: float):
    try:
        return subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except Exception:
        return None


class Tools:
    """Checker argv prefixes (None = unavailable)."""

    def __init__(self, state: dict | None = None) -> None:
        self.clang: list | None = None
        self.tier = "encoding-only"
        self._discover({} if state is None else state)

    def _from_cache(self, state: dict) -> bool:
        cache = state.get("tools")
        if not isinstance(cache, dict):
            return False
        try:
            if time.time() - float(cache.get("ts", 0)) >= DISCOVERY_TTL_SECONDS:
                return False
        except (TypeError, ValueError):
            return False
        argv = (cache.get("argvs") or {}).get("clang")
        if argv is None:
            self.tier = "encoding-only"
            return True
        if not isinstance(argv, list) or not argv or not Path(str(argv[0])).is_file():
            return False
        self.clang = [str(part) for part in argv]
        self.tier = "clang"
        return True

    def _discover(self, state: dict) -> None:
        if self._from_cache(state):
            return
        found = shutil.which("clang++")
        if found and Path(found).is_file():
            self.clang = [found]
        else:
            for candidate in CLANG_GLOBS:
                if Path(candidate).is_file():
                    self.clang = [candidate]
                    break
        if self.clang:
            proc = _run([*self.clang, "--version"], 5.0)
            if not proc or proc.returncode != 0:
                self.clang = None
        self.tier = "clang" if self.clang else "encoding-only"
        state["tools"] = {
            "ts": time.time(),
            "argvs": {"clang": self.clang},
        }


# ── Tier 2: clang++ -fsyntax-only ───────────────────────────────────────────
def clang_findings(
    tools: Tools, path: Path
) -> tuple[list[str], str, int]:
    """(parse errors, missing-header note, returncode) for one file.

    The file's own directory goes on the include path so sibling headers
    resolve; anything still missing voids the run (later diagnostics would be
    unreliable) and is reported as one deterministic note.
    """
    if not tools.clang:
        return [], "", -1
    lang = "c" if path.suffix.lower() in C_SUFFIXES else "c++"
    proc = _run(
        [
            *tools.clang,
            "-fsyntax-only",
            "-x",
            lang,
            "-I",
            str(path.parent),
            str(path),
        ],
        CLANG_TIMEOUT,
    )
    if proc is None:
        return [], "", -2
    output = (proc.stdout or "") + (proc.stderr or "")
    errors, missing = parse_clang_output(output)
    if missing:
        note = (
            f"{path.name}: header '{missing[0]}' not found"
            + (f" (+{len(missing) - 1} more)" if len(missing) > 1 else "")
            + ", syntax check skipped"
        )
        return [], note, proc.returncode
    return errors, "", proc.returncode


# ── Advisory ────────────────────────────────────────────────────────────────
def compose_advisory(
    syntax: list[str],
    encoding: list[str],
    notes: list[str],
) -> str:
    """Short, action-ordered text for hookSpecificOutput.additionalContext."""
    blocks: list[str] = []

    def add(label: str, items: list[str], limit: int = MAX_ITEMS_PER_SECTION) -> None:
        if not items:
            return
        shown = items[:limit]
        tail = f" (+{len(items) - len(shown)} more)" if len(items) > len(shown) else ""
        blocks.append(f"[cpp-edit-gate] {label}: " + " | ".join(shown) + tail)

    add("DOES NOT PARSE", syntax, 4)
    add("encoding", encoding)
    add("note", notes, 4)
    if not blocks:
        return ""
    text = "\n".join(blocks)
    return (
        text
        if len(text) <= ADVISORY_MAX_CHARS
        else text[: ADVISORY_MAX_CHARS - 3] + "..."
    )


def _emit(advisory: str) -> None:
    if not advisory:
        return
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


# ── Per-file pipeline ───────────────────────────────────────────────────────
def check_file(
    path: Path,
    tools: Tools,
    deadline: float,
    note_sink: list[str],
) -> dict:
    result = {"syntax": [], "encoding": [], "notes": []}
    try:
        blob = path.read_bytes()
    except OSError as exc:
        result["notes"].append(f"{path.name}: unreadable ({exc})")
        return result

    result["encoding"] = encoding_findings(blob, path)

    if tools.clang is None:
        note_sink.append(f"{path.name}: clang++ unavailable, syntax tier skipped")
    elif syntax_disabled_here(path):
        result["notes"].append(f"{path.name}: syntax check skipped (.edit-gate-disable)")
    elif _now() < deadline:
        errors, note, rc = clang_findings(tools, path)
        if rc == -2:
            note_sink.append(f"{path.name}: clang++ timed out")
        result["syntax"].extend(errors)
        if note:
            result["notes"].append(note)
    else:
        note_sink.append(f"{path.name}: syntax check skipped (time cap)")
    return result


# ── Main ────────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    started = _now()
    if os.environ.get("CPP_EDIT_GATE", "1").strip().lower() in ("0", "off", "false"):
        return 0

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
    targets = [
        p
        for p in extract_targets(payload, cwd)
        if p.suffix.lower() in WATCHED_SUFFIXES and not skip_reason(p)
    ]
    if not targets:
        return 0

    state = _load_state()
    tools = Tools(state)
    deadline = started + HARD_CAP_SECONDS

    sections = {k: [] for k in ("syntax", "encoding", "notes")}
    checked: list[str] = []
    note_sink: list[str] = []  # env/time-dependent: audit only, never advisory
    for path in targets:
        checked.append(str(path))
        result = check_file(path, tools, deadline, note_sink)
        digest = _sha1(str(path) + json.dumps(result, sort_keys=True, default=str))
        repeated = state.get(digest, 0) > time.time() - DEDUPE_TTL_SECONDS
        state[digest] = time.time()
        if repeated:
            for key in ("syntax", "encoding", "notes"):
                result[key] = []
        for key, bucket in sections.items():
            bucket.extend(result[key])

    advisory = compose_advisory(
        sections["syntax"],
        sections["encoding"],
        sections["notes"],
    )
    audit(
        {
            "ts": round(time.time(), 2),
            "event": payload.get("hook_event_name"),
            "tool": payload.get("tool_name"),
            "files": checked,
            "tier": tools.tier,
            "advisory_chars": len(advisory),
            "skipped": note_sink,
            "ms": int((_now() - started) * 1000),
        },
    )
    _save_state(state)
    _emit(advisory)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # fail open, always
        audit({"error": f"{type(exc).__name__}: {exc}"})
        sys.exit(0)

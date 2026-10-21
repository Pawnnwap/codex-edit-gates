"""js_edit_gate.py
Codex PostToolUse gate for JavaScript/TypeScript edits (global, standalone).

Registered as a lifecycle hook in ~/.codex/config.toml, so every Codex session
on this machine gets it. Reads the hook payload on stdin, finds the JS/TS files
the agent just touched, then:

  tier 1  encoding   byte-level classification: UTF-16 (node cannot read it),
                     GBK-only (mojibake under every UTF-8 toolchain), bytes
                     valid in no encoding at all. A UTF-8 BOM and non-ASCII
                     UTF-8 are deliberately NOT flagged: UTF-8 is the JS
                     source encoding and node strips the BOM.
  tier 2  syntax     node --check for .js/.mjs/.cjs (~50 ms). For .ts/.mts/.cts
                     that is NOT enough - node --check swallows type-strip
                     failures and reports success - so TypeScript goes through
                     node:module's stripTypeScriptTypes, whose SyntaxError
                     carries the line number. .jsx/.tsx have no parser on this
                     box (no esbuild/tsc): encoding only, honestly silent.

The advisory contains only file-derived, deterministic findings, so an
identical file always yields an identical message - which keeps the injected
context prefix stable and friendly to prefill caching. Tool availability,
timeouts, and wall-clock notes go to the audit log instead. The gate never
blocks a tool call and fails open: any internal error means exit 0 with no
output.

Deliberately standalone: stdlib only, imports nothing from this repo.

Kill switches:
  JS_EDIT_GATE=0         env var, disables the gate entirely
  .edit-gate-disable     marker file in a project: advisory-only there
                         (encoding still reports, no node run)

Audit log: ~/.codex/logs/js_edit_gate.jsonl
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Budgets. node starts in ~50 ms; the caps only matter for pathological files.
HARD_CAP_SECONDS = 8.0
NODE_TIMEOUT = 5.0
MAX_FILE_BYTES = 400_000
ADVISORY_MAX_CHARS = 1200
MAX_ITEMS_PER_SECTION = 8
DEDUPE_TTL_SECONDS = 6 * 3600
DISCOVERY_TTL_SECONDS = 3600

CHECK_SUFFIXES = {".js", ".mjs", ".cjs"}
TS_SUFFIXES = {".ts", ".mts", ".cts"}
JSX_SUFFIXES = {".jsx", ".tsx"}
WATCHED_SUFFIXES = CHECK_SUFFIXES | TS_SUFFIXES | JSX_SUFFIXES

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

# node --check prints "<path>:<line>" first, the source line, a caret line,
# then "SyntaxError: <message>" and a stack. The regexes pull those apart.
_NODE_LOC_RE = re.compile(r"^(?P<loc>.*?):(?P<line>\d+)\s*$")
_NODE_ERR_RE = re.compile(r"^(?P<name>\w*Error)(?:\s*\[[^\]]*\])?:\s*(?P<msg>.+)$")
_TSERR_PREFIX = "TSERR|"


# ── Logging / state ──────────────────────────────────────────────────────────
def _log_dir() -> Path:
    override = os.environ.get("JS_EDIT_GATE_LOG_DIR", "").strip()
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
        log = folder / "js_edit_gate.jsonl"
        _rotate(log)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _state_path() -> Path:
    return _log_dir() / "js_edit_gate.state.json"


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
    """Existing JS/TS files the payload refers to (patch headers + path keys)."""
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
    """Encoding traps for JS/TS on this box (node + UTF-8 toolchains)."""
    name = path.name
    findings: list[str] = []
    if blob.startswith(b"\xff\xfe") or blob.startswith(b"\xfe\xff"):
        findings.append(
            f"{name}: UTF-16 file - node reads it as mojibake, save as UTF-8"
        )
        return findings
    # UTF-8 (with or without BOM) is the JS source encoding; node strips the
    # BOM and non-ASCII strings are correct. No finding for either.
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
            f"{name}: GBK/CP936 only - string literals are mojibake under every"
            " UTF-8 toolchain (node, browsers, bundlers); save as UTF-8"
        )
    return findings


def parse_node_check_output(stdout: str) -> list[str]:
    """node --check stderr/stdout -> ['L..C.. Error: message'] (0 or 1 item)."""
    lines = (stdout or "").splitlines()
    number = 0
    col = 0
    message = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not message:
            match = _NODE_ERR_RE.match(stripped)
            if match:
                message = f"{match.group('name')}: {match.group('msg')}"
                continue
        if not number:
            match = _NODE_LOC_RE.match(stripped)
            # The location line is the first "path:line" line and is followed
            # by the source line; a caret line below it holds the column.
            if match and match.group("loc") and index + 1 < len(lines):
                try:
                    number = int(match.group("line"))
                except ValueError:
                    continue
                for follow in lines[index + 1 : index + 6]:
                    caret = follow.find("^")
                    if caret >= 0 and not follow.strip().startswith(("at ", "npm ")):
                        col = caret
                        break
    if not message:
        return []
    return [f"L{number}C{col} {message}"]


def parse_ts_output(stdout: str) -> list[str]:
    """tsprobe.js output -> ['L..C.. message'] (0 or 1 item)."""
    for line in (stdout or "").splitlines():
        if not line.startswith(_TSERR_PREFIX):
            continue
        parts = line[len(_TSERR_PREFIX) :].split("|", 2)
        if len(parts) != 3:
            continue
        return [f"L{parts[0]}C{parts[1]} {parts[2]}"]
    return []


# ── Tool discovery ──────────────────────────────────────────────────────────
NODE_CANDIDATES = [
    r"C:\Program Files\nodejs\node.exe",
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
        self.node: list | None = None
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
        argv = (cache.get("argvs") or {}).get("node")
        if argv is None:
            self.tier = "encoding-only"
            return True
        if not isinstance(argv, list) or not argv or not Path(str(argv[0])).is_file():
            return False
        self.node = [str(part) for part in argv]
        self.tier = "node"
        return True

    def _discover(self, state: dict) -> None:
        if self._from_cache(state):
            return
        found = shutil.which("node")
        if found and Path(found).is_file():
            self.node = [found]
        else:
            for candidate in NODE_CANDIDATES:
                if Path(candidate).is_file():
                    self.node = [candidate]
                    break
        if self.node:
            proc = _run([*self.node, "--version"], 5.0)
            if not proc or proc.returncode != 0:
                self.node = None
        self.tier = "node" if self.node else "encoding-only"
        state["tools"] = {
            "ts": time.time(),
            "argvs": {"node": self.node},
        }


# ── Tier 2: node syntax ─────────────────────────────────────────────────────
_PROBE_DIR = Path(tempfile.gettempdir()) / "js_edit_gate_probe"
# stripTypeScriptTypes is the TS parser node itself uses: it throws SyntaxError
# with a ":LINE" stack and a caret column. mode 'transform' accepts enums and
# other non-erasable syntax so valid TS is never misreported.
_TSPROBE_SOURCE = """const { stripTypeScriptTypes } = require("node:module");
const fs = require("fs");
try {
  stripTypeScriptTypes(fs.readFileSync(process.argv[2], "utf8"), { mode: "transform" });
} catch (e) {
  let line = 0, col = 0;
  const stack = String((e && e.stack) || "");
  const head = /^:(\\d+)/.exec(stack);
  if (head) line = parseInt(head[1], 10) || 0;
  for (const raw of stack.split("\\n")) {
    const t = raw.trim();
    if (t === "^") { col = raw.indexOf("^"); break; }
  }
  const msg = String((e && e.message) || "syntax error")
    .replace(/\\|/g, "/").replace(/[\\r\\n]+/g, " ");
  process.stdout.write("TSERR|" + line + "|" + col + "|" + msg);
  process.exit(1);
}
"""


def node_check_findings(tools: Tools, path: Path) -> tuple[list[str], int]:
    """node --check findings for .js/.mjs/.cjs. rc -2 = timed out."""
    if not tools.node:
        return [], -1
    proc = _run([*tools.node, "--check", str(path)], NODE_TIMEOUT)
    if proc is None:
        return [], -2
    if proc.returncode == 0:
        return [], proc.returncode
    return parse_node_check_output((proc.stdout or "") + (proc.stderr or "")), proc.returncode


def ts_findings(tools: Tools, path: Path) -> tuple[list[str], int]:
    """stripTypeScriptTypes findings for .ts/.mts/.cts. rc -2 = timed out."""
    if not tools.node:
        return [], -1
    try:
        _PROBE_DIR.mkdir(parents=True, exist_ok=True)
        probe = _PROBE_DIR / "tsprobe.js"
        if not probe.is_file() or probe.read_text(encoding="utf-8") != _TSPROBE_SOURCE:
            probe.write_bytes(_TSPROBE_SOURCE.encode("utf-8"))
    except OSError:
        return [], -3
    proc = _run([*tools.node, str(probe), str(path)], NODE_TIMEOUT)
    if proc is None:
        return [], -2
    return parse_ts_output(proc.stdout or ""), proc.returncode


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
        blocks.append(f"[js-edit-gate] {label}: " + " | ".join(shown) + tail)

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

    suffix = path.suffix.lower()
    if suffix in JSX_SUFFIXES:
        return result  # no JSX parser on this box: encoding only
    if tools.node is None:
        note_sink.append(f"{path.name}: node unavailable, syntax tier skipped")
    elif syntax_disabled_here(path):
        result["notes"].append(f"{path.name}: syntax check skipped (.edit-gate-disable)")
    elif _now() < deadline:
        if suffix in TS_SUFFIXES:
            errors, rc = ts_findings(tools, path)
        else:
            errors, rc = node_check_findings(tools, path)
        if rc == -2:
            note_sink.append(f"{path.name}: node syntax check timed out")
        elif rc == -3:
            note_sink.append(f"{path.name}: probe not writable")
        result["syntax"].extend(errors)
    else:
        note_sink.append(f"{path.name}: syntax check skipped (time cap)")
    return result


# ── Main ────────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    started = _now()
    if os.environ.get("JS_EDIT_GATE", "1").strip().lower() in ("0", "off", "false"):
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

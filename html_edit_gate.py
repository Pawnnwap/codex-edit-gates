"""html_edit_gate.py
Codex PostToolUse gate for HTML edits (global, standalone).

Registered as a lifecycle hook in ~/.codex/config.toml, so every Codex session
on this machine gets it. Reads the hook payload on stdin, finds the HTML files
the agent just touched, then:

  tier 1  encoding   byte-level classification plus the HTML-specific trap: a
                     <meta charset> declaration that contradicts the actual
                     bytes (declared GBK but saved UTF-8, declared UTF-8 but
                     saved GBK) mojibakes in every browser, and non-ASCII
                     content with no charset declared leaves the encoding to
                     guesswork. UTF-16 and never-decodable bytes are broken
                     outright. A UTF-8 BOM is not flagged: browsers skip it.
  tier 2  structure  stdlib html.parser tag-stack check: stray closing tags,
                     mismatched nesting, unclosed elements, duplicate
                     attributes. Elements whose end tag may be omitted in
                     HTML5 (li, td, p, tr, ...) auto-close silently, so valid
                     markup like <li>a<li>b or <table><tr><td>x</table> is
                     never nagged. Inside inline <svg>/<math> the checker goes
                     permissive (foreign content, tool-generated). .xhtml gets
                     a strict stdlib XML parse instead.

This gate is pure stdlib with zero subprocesses: it is the fastest tier in the
family and its advisory is a deterministic function of the file bytes - an
identical file always yields an identical message, which keeps the injected
context prefix stable and friendly to prefill caching. The gate never blocks a
tool call and fails open: any internal error means exit 0 with no output.

Deliberately standalone: stdlib only, imports nothing from this repo.

Kill switches:
  HTML_EDIT_GATE=0       env var, disables the gate entirely
  .edit-gate-disable     marker file in a project: advisory-only there
                         (encoding still reports, no structure check)

Audit log: ~/.codex/logs/html_edit_gate.jsonl
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from html.parser import HTMLParser
import sys
import time
from pathlib import Path
from xml.etree import ElementTree

MAX_FILE_BYTES = 400_000
ADVISORY_MAX_CHARS = 1200
MAX_ITEMS_PER_SECTION = 8
DEDUPE_TTL_SECONDS = 6 * 3600

HTML_SUFFIXES = {".html", ".htm"}
XHTML_SUFFIXES = {".xhtml"}
WATCHED_SUFFIXES = HTML_SUFFIXES | XHTML_SUFFIXES

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

# Elements that never take an end tag.
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
# HTML5 lets these drop their end tag; a stack checker that demanded them
# would nag perfectly valid markup (<li>a<li>b, <table><tr><td>x</table>).
OPTIONAL_END = {
    "html", "head", "body", "p", "li", "dt", "dd", "thead", "tbody", "tfoot",
    "tr", "td", "th", "option", "optgroup", "colgroup", "rt", "rp",
}
# A start tag from this set implicitly closes an open <p>.
_P_CLOSERS = {
    "address", "article", "aside", "blockquote", "details", "div", "dl",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol",
    "p", "pre", "section", "table", "ul",
}
# Foreign-content containers: inside these the checker goes permissive.
_FOREIGN = {"svg", "math"}

_CHARSET_RE = re.compile(
    r"""<meta[^>]*charset\s*=\s*["']?\s*([\w:.-]+)""", re.IGNORECASE
)
_UTF8_NAMES = {"utf8"}
_GB_NAMES = {"gbk", "gb2312", "gb18030", "cp936", "csgb2312", "xgbk"}


def _normalize_charset(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# ── Logging / state ──────────────────────────────────────────────────────────
def _log_dir() -> Path:
    override = os.environ.get("HTML_EDIT_GATE_LOG_DIR", "").strip()
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
        log = folder / "html_edit_gate.jsonl"
        _rotate(log)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _state_path() -> Path:
    return _log_dir() / "html_edit_gate.state.json"


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


def structure_disabled_here(path: Path) -> bool:
    """A project opts out of structure checks with a .edit-gate-disable marker."""
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
    """Existing HTML files the payload refers to (patch headers + path keys)."""
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
    """Encoding traps for HTML: byte validity plus declaration mismatches."""
    name = path.name
    findings: list[str] = []
    if blob.startswith(b"\xff\xfe") or blob.startswith(b"\xfe\xff"):
        findings.append(
            f"{name}: UTF-16 - browsers mishandle it; save as UTF-8"
        )
        return findings

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
    has_non_ascii = any(byte > 127 for byte in blob)

    text = blob.decode("utf-8", "replace")
    declared = None
    match = _CHARSET_RE.search(text)
    if match:
        declared = _normalize_charset(match.group(1))

    if declared in _UTF8_NAMES:
        if not utf8_ok:
            findings.append(
                f"{name}: declares utf-8 but the bytes are not valid UTF-8;"
                " fix the declaration or the file"
            )
    elif declared in _GB_NAMES:
        if utf8_ok and has_non_ascii:
            findings.append(
                f"{name}: declares {match.group(1)} but the bytes are UTF-8;"
                " fix the declaration or the file"
            )

    if not utf8_ok and not gbk_ok:
        findings.append(
            f"{name}: valid in neither UTF-8 nor GBK - mojibake everywhere"
        )
    elif not utf8_ok and gbk_ok:
        if declared is None:
            findings.append(
                f"{name}: GBK/CP936 only with no declared charset - save as"
                " UTF-8 and declare it"
            )
    elif has_non_ascii and declared is None:
        findings.append(
            f"{name}: non-ASCII with no declared charset - add"
            ' <meta charset="utf-8">'
        )
    return findings


class _TagStack(HTMLParser):
    """Tag-stack structure check with HTML5 optional-end-tag tolerance."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.errors: list[str] = []
        self._stack: list[tuple[str, int]] = []

    def _at(self) -> tuple[int, int]:
        return self.getpos()

    def _error(self, message: str) -> None:
        line, col = self._at()
        self.errors.append(f"L{line}C{col} {message}")

    def _foreign_open(self) -> bool:
        return any(tag in _FOREIGN for tag, _ in self._stack)

    def handle_starttag(self, tag: str, attrs) -> None:
        # Document order, not set order: report order must not depend on
        # PYTHONHASHSEED or the advisory stops being byte-reproducible.
        seen: set[str] = set()
        reported: set[str] = set()
        for name, _ in attrs:
            if name in seen and name not in reported:
                self._error(f"duplicate attribute '{name}' in <{tag}>")
                reported.add(name)
            seen.add(name)
        if tag in VOID_ELEMENTS:
            return
        if self._foreign_open():
            self._stack.append((tag, self._at()[0]))
            return
        # Implicit closes: a repeated optional-end-tag element, and a block
        # element ending an open <p>. Both are HTML5-valid and never errors.
        while self._stack and self._stack[-1][0] == tag and tag in OPTIONAL_END:
            self._stack.pop()
        if tag in _P_CLOSERS:
            while self._stack and self._stack[-1][0] == "p":
                self._stack.pop()
        self._stack.append((tag, self._at()[0]))

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_ELEMENTS and self._stack and self._stack[-1][0] == tag:
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        names = [t for t, _ in self._stack]
        if tag not in names:
            if tag not in OPTIONAL_END and not self._foreign_open():
                self._error(f"stray closing tag </{tag}>")
            return
        if self._foreign_open():
            while self._stack:
                t, _ = self._stack.pop()
                if t == tag:
                    return
            return
        while self._stack:
            t, _ = self._stack.pop()
            if t == tag:
                return
            if t not in OPTIONAL_END:
                self._error(f"expected </{t}> before </{tag}>")

    def finish(self) -> None:
        if self._foreign_open():
            return  # unclosed foreign subtrees are tool-generated, not defects
        for tag, line in self._stack:
            if tag not in OPTIONAL_END:
                self.errors.append(f"L{line}C0 unclosed <{tag}> (opened here)")
        self._stack = []


def structure_findings(text: str) -> list[str]:
    """Tag-stack findings for HTML; capped and deterministic in file order."""
    parser = _TagStack()
    try:
        parser.feed(text)
        parser.close()
        parser.finish()
    except Exception:
        return []  # html.parser is lenient; a crash here is not the file's fault
    return parser.errors


def xml_findings(blob: bytes) -> list[str]:
    """Strict well-formedness for .xhtml (XML rules apply in full)."""
    try:
        ElementTree.fromstring(blob)
        return []
    except ElementTree.ParseError as exc:
        line = getattr(exc, "position", (0, 0))[0]
        return [f"L{line}C0 {exc.msg}"]


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
        blocks.append(f"{label}: " + " | ".join(shown) + tail)

    add("DOES NOT PARSE", syntax, 4)
    add("encoding", encoding)
    add("note", notes, 4)
    if not blocks:
        return ""
    # Gate tag on the first line only - one advisory per tool call, a tag per
    # block is pure repetition in the model's context.
    text = "\n".join([f"[html-edit-gate] {blocks[0]}"] + blocks[1:])
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
def check_file(path: Path) -> dict:
    result = {"syntax": [], "encoding": [], "notes": []}
    try:
        blob = path.read_bytes()
    except OSError as exc:
        result["notes"].append(f"{path.name}: unreadable ({exc})")
        return result

    result["encoding"] = encoding_findings(blob, path)

    if structure_disabled_here(path):
        result["notes"].append(f"{path.name}: structure check skipped (.edit-gate-disable)")
        return result

    if path.suffix.lower() in XHTML_SUFFIXES:
        result["syntax"].extend(xml_findings(blob))
    else:
        text = blob.decode("utf-8", "replace")
        result["syntax"].extend(structure_findings(text))
    return result


# ── Main ────────────────────────────────────────────────────────────────────
def _emit_error(exc: Exception) -> None:
    """Fail open, but never silently: one brief error line to the advisory."""
    try:
        message = f"[html-edit-gate]: internal error: {type(exc).__name__}: {exc}"[:300]
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": message,
                },
                "systemMessage": message[:200],
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    except Exception:
        sys.stderr.write(f"[html-edit-gate]: internal error: {type(exc).__name__}: {exc}\n")


def main(argv: list[str]) -> int:
    started = _now()
    if os.environ.get("HTML_EDIT_GATE", "1").strip().lower() in ("0", "off", "false"):
        return 0

    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        audit({"error": f"payload unreadable: {type(exc).__name__}: {exc}"})
        _emit_error(exc)
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

    sections = {k: [] for k in ("syntax", "encoding", "notes")}
    checked: list[str] = []
    for path in targets:
        checked.append(str(path))
        result = check_file(path)
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
            "tier": "stdlib",
            "advisory_chars": len(advisory),
            "skipped": [],
            "ms": int((_now() - started) * 1000),
        },
    )
    _save_state(state)
    _emit(advisory)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # fail open, but visibly
        audit({"error": f"{type(exc).__name__}: {exc}"})
        _emit_error(exc)
        sys.exit(0)

"""cpp_edit_gate.py
Codex PostToolUse gate for C/C++ edits (global, standalone).

Registered as a lifecycle hook in ~/.codex/config.toml, so every Codex session
on this machine gets it. Reads the hook payload on stdin, finds the C/C++ files
the agent just touched, then:

  tier 1  safe autofix  narrow, deterministic fixes verified by re-read/re-check
                        before and after (any mismatch reverts the file):
                          - UTF-16 (LE/BE BOM) decoded and saved as UTF-8+BOM
                          - BOM-less UTF-8 with non-ASCII gets a BOM prepended
                          - GBK/CP936-only text decoded and saved as UTF-8+BOM
                          - "expected ';' after class/struct/union/enum" at a
                            brace that closes the type: one semicolon inserted
                          - clang-format -i when the leftover drift is small
                            (replacement count within the churn cap), proven
                            idempotent by a re-measure; a larger drift is a
                            reformat and is held back instead
                        Everything else stays report-only. The advisory says
                        what was fixed in the same tone as the ruff-based
                        py_edit_gate ("AUTO-FIXED: ...").
  tier 2  encoding      byte-level classification after any fix: UTF-16 (no
                        compiler reads it), non-ASCII in BOM-less text (MSVC
                        falls back to the ANSI codepage and garbles string
                        literals unless /utf-8 or a BOM is used), GBK-only
                        (breaks off this CP936 box), bytes valid in no
                        encoding at all. A UTF-8 BOM is deliberately NOT
                        flagged: MSVC needs it to detect UTF-8.
  tier 3  syntax        clang++ -fsyntax-only with the file's own directory on
                        the include path. Only parse-class errors are reported
                        (expected/extraneous/unbalanced/...); semantic errors
                        are suppressed on purpose because they false-positive
                        without project context ("use of undeclared
                        identifier" for a sibling header's function is not a
                        defect). A missing project header voids the run: one
                        deterministic note replaces the findings.
  tier 4  tidy          clang-tidy with a curated allow-list (bugprone-*,
                        performance-*, portability-*, modernize-use-nullptr,
                        modernize-use-override), run only on parse-clean
                        files, main-file diagnostics only. Warnings only:
                        tidy --fix needs a project compile database and
                        rewrites more than the churn budget allows, so it is
                        deliberately not on.

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
                         (autofix off, encoding still reports, no clang run)
  --check-only           CLI flag in the hook command string: detection only,
                         never writes

Audit log: ~/.codex/logs/cpp_edit_gate.jsonl
"""

from __future__ import annotations

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
CHECK_ONLY = "--check-only" in sys.argv

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
# The only parse class the gate dares to fix: a type definition whose closing
# brace lost its semicolon ("expected ';' after struct ..."). Everything else
# has an unbounded number of plausible fixes, so it stays advisory.
_AUTO_SEMI_RE = re.compile(
    r"expected ';' after (?:class|struct|union|enum)", re.I
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


def _shared_tools() -> dict:
    """Tool argvs from tool_discovery's session-start scan (fast path).
    Returns {"__error__": msg} when the cache is unreadable - never silent."""
    try:
        data = json.loads(
            (_log_dir() / "tool_discovery.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {}  # not warmed yet - normal before the first session start
    except Exception as exc:
        return {"__error__": f"tool cache unreadable: {type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"__error__": "tool cache unreadable: not an object"}
    try:
        if time.time() - float(data.get("ts", 0)) > 12 * 3600:
            return {}
    except (TypeError, ValueError):
        return {}
    tools = data.get("tools")
    return tools if isinstance(tools, dict) else {}


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
            f"{name}: UTF-16 - no compiler reads it; save as UTF-8"
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
            f"{name}: valid in neither UTF-8 nor GBK - mojibake everywhere"
        )
    elif not utf8_ok and gbk_ok:
        findings.append(
            f"{name}: GBK/CP936 only - breaks with /utf-8 or on any non-CP936"
            " machine; save as UTF-8"
        )
    else:
        has_non_ascii = any(byte > 127 for byte in blob)
        has_bom = blob.startswith(b"\xef\xbb\xbf")
        if has_non_ascii and not has_bom:
            findings.append(
                f"{name}: non-ASCII in BOM-less UTF-8 - MSVC garbles string"
                " literals; add a BOM or compile with /utf-8"
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


# ── Tier 1: safe autofix (verify-before/after, revert on any mismatch) ─────
def autofix_encoding(blob: bytes) -> tuple[bytes | None, str]:
    """Deterministic encoding rescue: UTF-16 / BOM-less text / GBK-only.

    Returns (fixed_bytes or None, description). Never raises; any internal
    doubt means the file is left untouched (tier 2 keeps reporting it).
    """
    try:
        if blob[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = blob.decode("utf-16", "strict")
            if not text.strip():
                return None, ""
            return (
                b"\xef\xbb\xbf" + text.encode("utf-8"),
                "UTF-16 decoded, saved as UTF-8+BOM",
            )
        has_bom = blob.startswith(b"\xef\xbb\xbf")
        try:
            blob.decode("utf-8", "strict")
            utf8_ok = True
        except UnicodeDecodeError:
            utf8_ok = False
        try:
            blob.decode("gbk", "strict")
            gbk_ok = True
        except UnicodeDecodeError:
            gbk_ok = False
        if utf8_ok and any(byte > 127 for byte in blob) and not has_bom:
            # Pure 3-byte prepend: zero bytes of the file are rewritten, so a
            # mistake here cannot garble existing characters.
            return (
                b"\xef\xbb\xbf" + blob,
                "BOM added (BOM-less UTF-8 with non-ASCII)",
            )
        if not utf8_ok and gbk_ok and not has_bom:
            text = blob.decode("gbk", "strict")
            return (
                b"\xef\xbb\xbf" + text.encode("utf-8"),
                "GBK/CP936 decoded, saved as UTF-8+BOM",
            )
    except Exception:
        pass
    return None, ""


def _semicolon_candidates(lines: list[str], errors: list[str]) -> list[int]:
    """0-based lines where a type-closing brace lost its ';' (clang's view).

    clang normally reports the brace line itself; some variants point one
    line past it, so that line gets the first pass.
    """
    cands: list[int] = []
    for err in errors:
        if not _AUTO_SEMI_RE.search(err):
            continue
        match = re.match(r"L(\d+)C(\d+)", err)
        if not match:
            continue
        for line_no in (int(match.group(1)) - 1, int(match.group(1))):
            if (
                0 <= line_no < len(lines)
                and line_no not in cands
                and _line_ends_with_type_brace(lines[line_no])
            ):
                cands.append(line_no)
    return cands


_TYPE_BRACE_END_RE = re.compile(r"\}\s*(?://[^\n]*)?$")


def _line_ends_with_type_brace(line: str) -> bool:
    """The line visibly ends a type body: '}' then only whitespace/comment."""
    return bool(_TYPE_BRACE_END_RE.search(line))


def autofix_semicolon(
    tools: Tools, path: Path, before_errors: list[str] | None = None
) -> tuple[list[str], str, int]:
    """Insert one ';' after a type-closing brace, then prove it with clang.

    Exactly one insertion per file/run. Kept only when clang's parse-error
    count stays equal or improves; anything else reverts the original bytes.
    Returns (remaining_errors, fix_desc_or_empty, rc).
    """
    if not tools.clang:
        return [], "", -1
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8", "strict")
    except Exception:
        return [], "", -1
    lines = text.split("\n")
    if before_errors is None:
        before_errors, _, rc0 = clang_findings(tools, path)
    else:
        before_errors = list(before_errors)
        rc0 = -1
    cands = _semicolon_candidates(lines, before_errors)
    if not cands:
        return before_errors, "", rc0

    idx = cands[0]
    line = lines[idx]
    pos = line.rstrip().rfind("}")
    if pos < 0 or line[:pos + 1].endswith(";"):
        return before_errors, "", rc0
    new_line = line[:pos + 1] + ";" + line[pos + 1:]
    candidate_text = "\n".join(list(lines[:idx]) + [new_line] + lines[idx + 1:])
    if candidate_text == text:
        return before_errors, "", rc0
    try:
        path.write_bytes(candidate_text.encode("utf-8"))
    except OSError:
        return before_errors, "", rc0
    after_errors, _, rc1 = clang_findings(tools, path)
    keep = (
        not err_worse(before_errors, after_errors)
        and (
            len(after_errors) < len(before_errors)
            or not any(_AUTO_SEMI_RE.search(err) for err in after_errors)
        )
    )
    if not keep:
        try:
            path.write_bytes(raw)
        except OSError:
            pass
        return before_errors, "", rc0
    return after_errors, "inserted ';' after struct/class closing brace", rc1


def err_worse(before: list[str], after: list[str]) -> bool:
    """True when the patched file surfaced a parse error it did not have."""
    before_keys = set(before)
    return any(err not in before_keys for err in after)


# ── Tier 2b: clang-format (popular formatter, churn-capped) ─────────────────
FORMAT_CHURN_CAP = 40  # replacement cells; beyond this it is a reformat, not a fix
_REPLACEMENT_RE = re.compile(r"<replacement ")


def fmt_replacements(tools: Tools, path: Path, deadline: float) -> int:
    """How many replace-cells clang-format would apply (0 = already clean).

    Negative means 'unknown' (missing tool, timeout, cap-budget) - callers
    treat it as no-op, and the audit log carries a note.
    """
    if not tools.clang_format or _now() > deadline:
        return -1
    proc = _run(
        [*tools.clang_format, "--output-replacements-xml", str(path)],
        CLANG_TIMEOUT,
    )
    if proc is None or proc.returncode != 0 or not proc.stdout:
        return -1
    # "replacements" (plural, the root tag) cannot match: the literal here is
    # '<replacement ' with one trailing space, which only the cells carry.
    return proc.stdout.count("<replacement ")


def autofix_format(tools: Tools, path: Path, deadline: float, note_sink: list[str]) -> int:
    """Apply clang-format when the churn stays small, then prove idempotence.

    Returns the number of applied replacement cells (0 when nothing was done).
    The fixed bytes survive only when a re-measure says the file is now clean;
    anything else restores the original bytes.
    """
    raw = path.read_bytes()
    spots = fmt_replacements(tools, path, deadline)
    if spots <= 0:
        if spots < 0:
            note_sink.append(f"{path.name}: clang-format probe skipped (tool/time)")
        return 0
    if spots > FORMAT_CHURN_CAP:
        note_sink.append(
            f"{path.name}: clang-format held back ({spots} spots > churn cap"
            f" {FORMAT_CHURN_CAP})"
        )
        return 0
    proc = _run([*tools.clang_format, "-i", str(path)], CLANG_TIMEOUT)
    if proc is None or proc.returncode != 0:
        path.write_bytes(raw)
        return 0
    leftover = fmt_replacements(tools, path, deadline)
    if leftover != 0:
        path.write_bytes(raw)
        note_sink.append(f"{path.name}: clang-format not idempotent, reverted")
        return 0
    return spots


# ── Tier 2c: clang-tidy (curated warning set, no --fix) ─────────────────────
TIDY_CHECKS = "-*,bugprone-*,performance-*,portability-*,modernize-use-nullptr,modernize-use-override"
TIDY_TIMEOUT = 10.0
_TIDY_LINE_RE = re.compile(
    r"^(?P<loc>.*?):(?P<line>\d+):(?P<col>\d+):\s+warning:\s+(?P<msg>.+?)\s+\[(?P<check>.+)\]$"
)


def tidy_findings(tools: Tools, path: Path, deadline: float) -> list[str]:
    """clang-tidy warnings for one file, main-file diagnostics only.

    Tidy needs a valid parse, so callers run it only after the syntax tier is
    clean. Findings are deterministic per file and capped like the rest.
    """
    if not tools.clang_tidy or _now() > deadline:
        return []
    lang = "c" if path.suffix.lower() in C_SUFFIXES else "c++"
    proc = _run(
        [
            *tools.clang_tidy,
            "--quiet",
            f"--checks={TIDY_CHECKS}",
            str(path),
            "--",
            "-x",
            lang,
            "-I",
            str(path.parent),
        ],
        TIDY_TIMEOUT,
    )
    if proc is None:
        return []
    out: list[str] = []
    for line in (proc.stdout or "").splitlines():
        match = _TIDY_LINE_RE.match(line.strip())
        if match:
            out.append(
                f"L{match.group('line')}C{match.group('col')} "
                f"{match.group('msg')} [{match.group('check')}]"
            )
    return out


# ── Tool discovery ──────────────────────────────────────────────────────────
def _program_bases() -> list[Path]:
    """Standard install bases from the environment, never hardcoded."""
    bases: list[Path] = []
    if os.name == "nt":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            value = os.environ.get(var)
            if value:
                bases.append(Path(value))
    else:
        for entry in (os.environ.get("PATH") or "").split(os.pathsep):
            if entry:
                bases.append(Path(entry))
    return bases


def _llvm_tool_candidates(name: str) -> list[str]:
    """<name> on PATH first, else vendor layouts beneath detected bases."""
    out: list[str] = []
    found = shutil.which(name)
    if found:
        out.append(found)
    for base in _program_bases():
        candidate = base / "LLVM" / "bin" / f"{name}.exe"
        if candidate.is_file() and str(candidate) not in out:
            out.append(str(candidate))
    return out


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


_TOOL_NAMES = ("clang", "clang-format", "clang-tidy")


class Tools:
    """Checker argv prefixes (None = unavailable)."""

    def __init__(self, state: dict | None = None) -> None:
        for name in _TOOL_NAMES:
            key = name.replace("-", "_")
            setattr(self, key, None)
        self.tier = "encoding-only"
        self.shared_error = ""
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
        argvs = cache.get("argvs") or {}
        missing = [name for name in _TOOL_NAMES if name not in argvs]
        if missing:
            return False  # cache predates this tool set: rediscover once
        for name in _TOOL_NAMES:
            key = name.replace("-", "_")
            argv = argvs.get(name)
            if not isinstance(argv, list) or not argv or not Path(str(argv[0])).is_file():
                setattr(self, key, None)
            else:
                setattr(self, key, [str(part) for part in argv])
        self.tier = "clang" if self.clang else "encoding-only"
        return True

    def _discover(self, state: dict) -> None:
        if self._from_cache(state):
            return
        for name in _TOOL_NAMES:
            key = name.replace("-", "_")
            candidates = _llvm_tool_candidates(name)
            if not candidates:
                continue
            proc = _run([candidates[0], "--version"], 5.0)
            if proc and proc.returncode == 0:
                setattr(self, key, [candidates[0]])
        # The session-start scan is authoritative: it swept the machine once,
        # so its verdict wins over whatever this run probed - including an
        # explicit "missing", which must stay missing until the next sweep.
        shared = _shared_tools()
        if isinstance(shared, dict):
            self.shared_error = str(shared.get("__error__") or "")
            for name in _TOOL_NAMES:
                key = name.replace("-", "_")
                info = shared.get(name)
                if not isinstance(info, dict):
                    continue
                if info.get("argv"):
                    setattr(self, key, [str(part) for part in info["argv"]])
                else:
                    setattr(self, key, None)
        self.tier = "clang" if self.clang else "encoding-only"
        state["tools"] = {
            "ts": time.time(),
            "argvs": {name: getattr(self, name.replace("-", "_")) for name in _TOOL_NAMES},
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
    fixed: list[str],
    syntax: list[str],
    encoding: list[str],
    tidy: list[str],
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

    add("AUTO-FIXED", fixed, 4)
    add("DOES NOT PARSE", syntax, 4)
    add("encoding", encoding)
    add("tidy", tidy)
    add("note", notes, 4)
    if not blocks:
        return ""
    # Gate tag on the first line only - one advisory per tool call, a tag per
    # block is pure repetition in the model's context.
    text = "\n".join([f"[cpp-edit-gate] {blocks[0]}"] + blocks[1:])
    if fixed:
        text += "\nFile changed on disk - re-read before the next patch to it."
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
    result = {"syntax": [], "encoding": [], "notes": [], "fixed": [], "tidy": []}
    try:
        blob = path.read_bytes()
    except OSError as exc:
        result["notes"].append(f"{path.name}: unreadable ({exc})")
        return result

    may_write = not CHECK_ONLY and not syntax_disabled_here(path)
    if may_write:
        new_blob, fix_msg = autofix_encoding(blob)
        if new_blob is not None:
            try:
                path.write_bytes(new_blob)
                blob = new_blob
                result["fixed"].append(fix_msg)
            except OSError:
                note_sink.append(f"{path.name}: encoding autofix failed to write")

    result["encoding"] = encoding_findings(blob, path)
    if getattr(tools, "shared_error", ""):
        result["notes"].append(str(tools.shared_error))

    if tools.clang is None:
        result["notes"].append(f"{path.name}: clang++ unavailable - syntax check degraded")
    elif syntax_disabled_here(path):
        result["notes"].append(f"{path.name}: syntax check skipped (.edit-gate-disable)")
    elif _now() < deadline:
        parse_errors, note, rc = clang_findings(tools, path)
        if rc == -2:
            note_sink.append(f"{path.name}: clang++ timed out")
        if note:
            result["notes"].append(note)

        wrote = False
        if may_write and parse_errors:
            parse_errors, fix_msg, rc = autofix_semicolon(tools, path, parse_errors)
            if rc == -2:
                note_sink.append(f"{path.name}: clang++ timed out (post-fix)")
            if fix_msg:
                result["fixed"].append(fix_msg)
                wrote = True
        if may_write and not parse_errors and tools.clang_format:
            spots = autofix_format(tools, path, deadline, note_sink)
            if spots:
                result["fixed"].append(f"clang-format normalized {spots} spots")
                wrote = True

        if wrote:  # one re-parse so every reported coordinate is disk-true
            parse_errors, note, rc = clang_findings(tools, path)
            if rc == -2:
                note_sink.append(f"{path.name}: clang++ timed out (post-format)")
            if note:
                result["notes"].append(note)

        if parse_errors:
            result["syntax"].extend(parse_errors)
        elif tools.clang_tidy:
            result["tidy"].extend(tidy_findings(tools, path, deadline))
    else:
        note_sink.append(f"{path.name}: syntax check skipped (time cap)")
    return result


# ── Main ────────────────────────────────────────────────────────────────────
def _emit_error(exc: Exception) -> None:
    """Fail open, but never silently: one brief error line to the advisory."""
    try:
        message = f"[cpp-edit-gate]: internal error: {type(exc).__name__}: {exc}"[:300]
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
        sys.stderr.write(f"[cpp-edit-gate]: internal error: {type(exc).__name__}: {exc}\n")


def main(argv: list[str]) -> int:
    started = _now()
    if os.environ.get("CPP_EDIT_GATE", "1").strip().lower() in ("0", "off", "false"):
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
    tools = Tools(state)
    deadline = started + HARD_CAP_SECONDS

    sections = {k: [] for k in ("syntax", "encoding", "notes", "fixed", "tidy")}
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
        sections["fixed"],
        sections["syntax"],
        sections["encoding"],
        sections["tidy"],
        sections["notes"],
    )
    audit(
        {
            "ts": round(time.time(), 2),
            "event": payload.get("hook_event_name"),
            "tool": payload.get("tool_name"),
            "files": checked,
            "tier": tools.tier,
            "fixed": sections["fixed"],
            "tidy_count": len(sections["tidy"]),
            "check_only": CHECK_ONLY,
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
    except Exception as exc:  # fail open, but visibly
        audit({"error": f"{type(exc).__name__}: {exc}"})
        _emit_error(exc)
        sys.exit(0)

"""ml_edit_gate.py
Codex PostToolUse gate for MATLAB .m edits (global, standalone).

Registered as a lifecycle hook in ~/.codex/config.toml, so every Codex session
on this machine gets it. Reads the hook payload on stdin, finds the .m files
the agent just touched, then:

  tier 1  encoding   byte-level classification: UTF-16 (unreadable), UTF-8 BOM
                     (mh_lint's lexer dies on it, and fileread keeps U+FEFF in
                     the char stream), GBK-only (reads fine under this box's
                     CP936 + R2021a, breaks under R2021b+ / non-CP936 readers),
                     bytes valid in no encoding at all
  tier 2  fast lint  mh_lint --ignore-config --brief (~0.3 s): parse errors and
                     structural checks, isolated from any repo miss_hit config
  tier 3  authoritative   matlab -batch checkcode (R2021a): the real parser's
                     findings with line numbers. MATLAB startup costs 5-16 s,
                     so this tier is cadence-throttled (once per file digest
                     per interval, one spawn per hook run) and its findings
                     ride a UTF-8 output file, bypassing the CP936 console.

Everything found comes back as a short advisory in
hookSpecificOutput.additionalContext. The gate never blocks a tool call and
fails open: any internal error means exit 0 with no output, so a broken gate
can never break an agent session.

Deliberately standalone: stdlib only, imports nothing from this repo, so the
installed copy under ~/.codex/scripts works from any workspace.

Kill switches:
  ML_EDIT_GATE=0            env var, disables the gate entirely
  ML_EDIT_GATE_CHECKCODE=0  env var, keeps the matlab tier from spawning
  .edit-gate-disable        marker file in a project: advisory-only there
                            (fast tiers report, no heavyweight matlab run)

Audit log: ~/.codex/logs/ml_edit_gate.jsonl
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Budgets. MATLAB -batch startup measures 5-16 s here, so one spawn per run
# and a generous per-call timeout keep the whole hook under its 25 s cap.
HARD_CAP_SECONDS = 22.0
MH_TIMEOUT = 8.0
MATLAB_TIMEOUT = 21.0
# Only launch matlab while enough budget is left for a full startup + run.
MATLAB_LAUNCH_WINDOW = 4.0
MAX_CHECKCODE_PER_RUN = 1
# The authoritative tier is cadence-throttled: at most one checkcode per file
# per interval, so a rapid edit loop cannot spawn MATLAB on every keystroke.
CHECKCODE_INTERVAL = 120.0
MAX_FILE_BYTES = 400_000
ADVISORY_MAX_CHARS = 1200
MAX_ITEMS_PER_SECTION = 8
DEDUPE_TTL_SECONDS = 6 * 3600
# Probing mh_lint/matlab costs subprocesses per edit; cache it.
DISCOVERY_TTL_SECONDS = 3600

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

# mh_lint emits "path:line:col: <severity>: message" in --brief mode.
_MH_LINE_RE = re.compile(r"^(?P<loc>.*?):(?P<line>\d+):(?P<col>\d+):\s+(?P<msg>.+)$")
# A message is a parse failure when it starts with "error" / "lex error".
_MH_ERROR_RE = re.compile(r"^(?:lex\s+)?error\b")
# checkcode messages that mean the file does not parse.
_CHECKCODE_SYNTAX_RE = re.compile(r"syntax|not\s+(?:be\s+)?closed|unbalanced", re.I)


# ── Logging / state ──────────────────────────────────────────────────────────
def _log_dir() -> Path:
    override = os.environ.get("ML_EDIT_GATE_LOG_DIR", "").strip()
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
        log = folder / "ml_edit_gate.jsonl"
        _rotate(log)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _state_path() -> Path:
    return _log_dir() / "ml_edit_gate.state.json"


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
        for key in ("tools",):
            if key in state:
                kept.append((key, state[key]))
        for key, value in state.items():
            if key.startswith("checkcode:") and isinstance(value, dict):
                if float(value.get("ts", 0)) > cutoff:
                    kept.append((key, value))
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


def checkcode_disabled_here(path: Path) -> bool:
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
    """Existing .m files the payload refers to (patch headers + path keys)."""
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
        if not cleaned.lower().endswith(".m"):
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
    """Encoding traps for .m files on this box (R2021a + CP936 + mh_lint)."""
    name = path.name
    findings: list[str] = []
    if blob.startswith(b"\xff\xfe") or blob.startswith(b"\xfe\xff"):
        findings.append(
            f"{name}: UTF-16 - MATLAB and mh_lint cannot read it; save as"
            " UTF-8 (no BOM)"
        )
        return findings
    if blob.startswith(b"\xef\xbb\xbf"):
        findings.append(
            f"{name}: UTF-8 BOM - mh_lint lex-errors on it; save without BOM"
        )
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
            f"{name}: GBK/CP936 only - breaks under R2021b+ or any non-CP936"
            " machine; save as UTF-8"
        )
    return findings


def parse_mh_lint_output(stdout: str) -> tuple[list[str], list[str]]:
    """mh_lint --brief output -> (parse errors, lint findings)."""
    syntax: list[str] = []
    lint: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("MISS_HIT"):
            continue
        match = _MH_LINE_RE.match(line)
        if not match:
            continue
        entry = f"L{match.group('line')}C{match.group('col')} {match.group('msg')}"
        if _MH_ERROR_RE.match(match.group("msg")):
            syntax.append(entry)
        else:
            lint.append(entry)
    return syntax, lint


def parse_checkcode_output(text: str) -> list[tuple[int, int, str]]:
    """UTF-8 probe file lines 'LINE|COL|MESSAGE' -> [(line, col, message)]."""
    out: list[tuple[int, int, str]] = []
    for line in (text or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        try:
            number, column = int(parts[0] or 0), int(parts[1] or 0)
        except ValueError:
            continue
        out.append((number, column, parts[2]))
    return out


# ── Tool discovery ──────────────────────────────────────────────────────────
def interpreter_candidates() -> list[str]:
    """Python interpreters to probe: the running one, PATH, active envs."""
    out: list[str] = [sys.executable]
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found and found not in out:
            out.append(found)
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        base = os.environ.get(var)
        if not base:
            continue
        for rel in ("Scripts/python.exe", "bin/python", "bin/python3"):
            candidate = Path(base) / rel
            if candidate.is_file() and str(candidate) not in out:
                out.append(str(candidate))
    return out


def _program_bases() -> list[Path]:
    """Standard install bases from the environment, never hardcoded: the
    Windows program-file env vars, or PATH entries (plus their prefix parents,
    which cover prefix-style installs) on POSIX."""
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
                bases.append(Path(entry).parent)
        bases.append(Path.home() / ".local" / "bin")
    return bases


def _matlab_candidates() -> list[str]:
    """matlab on PATH first, else vendor install layouts beneath the detected
    bases (release directories like R2024a are globbed)."""
    out: list[str] = []
    found = shutil.which("matlab")
    if found:
        out.append(found)
    if os.name == "nt":
        layouts = ("MATLAB/R*/bin/matlab.exe", "Polyspace/R*/bin/matlab.exe")
    else:
        layouts = ("MATLAB/R*/bin/matlab", "matlab/R*/bin/matlab")
    for base in _program_bases():
        for rel in layouts:
            for candidate in sorted(glob.glob(str(base / rel))):
                if Path(candidate).is_file() and candidate not in out:
                    out.append(candidate)
    return out


def _run(cmd: list, timeout: float, env: dict | None = None):
    try:
        return subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except Exception:
        return None


class Tools:
    """Checker argv prefixes (None = unavailable)."""

    def __init__(self, state: dict | None = None) -> None:
        self.mh_lint: list | None = None
        self.matlab: list | None = None
        self.tier = "encoding-only"
        self.shared_error = ""
        self._discover({} if state is None else state)

    def _from_cache(self, slots: dict, state: dict) -> bool:
        cache = state.get("tools")
        if not isinstance(cache, dict):
            return False
        try:
            if time.time() - float(cache.get("ts", 0)) >= DISCOVERY_TTL_SECONDS:
                return False
        except (TypeError, ValueError):
            return False
        argvs = cache.get("argvs")
        if not isinstance(argvs, dict) or set(argvs) != set(slots):
            return False
        for name in slots:
            argv = argvs.get(name)
            if argv is None:
                continue
            # An interpreter or install can move or vanish between edits.
            if (
                not isinstance(argv, list)
                or not argv
                or not Path(str(argv[0])).is_file()
            ):
                return False
            setattr(self, name, [str(part) for part in argv])
        self.tier = "+".join(n for n in slots if getattr(self, n)) or "encoding-only"
        return True

    def _discover(self, state: dict) -> None:
        slots = {"mh_lint": "mh_lint", "matlab": "matlab"}
        if self._from_cache(slots, state):
            return
        # mh_lint: PATH first, then known interpreters' install dirs (Windows
        # conda puts env exes at the root and tools in Scripts; venvs put both
        # in Scripts; POSIX pip puts them in bin - try every layout).
        found = shutil.which("mh_lint")
        if found:
            self.mh_lint = [found]
        else:
            for interp in interpreter_candidates():
                base = Path(interp).parent
                for candidate in (
                    base / "mh_lint.exe",
                    base / "Scripts" / "mh_lint.exe",
                    base / "bin" / "mh_lint",
                    base / "bin" / "mh_lint.exe",
                ):
                    if candidate.is_file():
                        self.mh_lint = [str(candidate)]
                        break
                if self.mh_lint:
                    break
        if self.mh_lint:
            proc = _run([*self.mh_lint, "--version"], MH_TIMEOUT)
            if not proc or proc.returncode != 0:
                self.mh_lint = None
        # matlab: PATH first, else vendor layouts beneath detected bases.
        candidates = _matlab_candidates()
        if candidates:
            self.matlab = [candidates[0]]
        # The session-start scan is authoritative: it swept the machine once,
        # so its verdict wins over whatever this run probed - including an
        # explicit "missing", which must stay missing until the next sweep.
        shared = _shared_tools()
        if isinstance(shared, dict):
            self.shared_error = str(shared.get("__error__") or "")
            for name in ("mh_lint", "matlab"):
                info = shared.get(name)
                if not isinstance(info, dict):
                    continue
                if info.get("argv"):
                    setattr(self, name, [str(part) for part in info["argv"]])
                else:
                    setattr(self, name, None)
        self.tier = (
            "+".join(n for n in ("mh_lint", "matlab") if getattr(self, n))
            or "encoding-only"
        )
        state["tools"] = {
            "ts": time.time(),
            "argvs": {"mh_lint": self.mh_lint, "matlab": self.matlab},
        }


# ── Tier 2: mh_lint ─────────────────────────────────────────────────────────
def mh_lint_findings(tools: Tools, path: Path) -> tuple[list[str], list[str]]:
    """(parse errors, lint findings); both empty when mh_lint produced nothing."""
    if not tools.mh_lint:
        return [], []
    # PYTHONIOENCODING keeps any non-ASCII in the output valid UTF-8 on capture.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = _run(
        [*tools.mh_lint, "--ignore-config", "--brief", str(path)],
        MH_TIMEOUT,
        env=env,
    )
    if proc is None:
        return [], []
    return parse_mh_lint_output(proc.stdout or "")


# ── Tier 3: matlab -batch checkcode ─────────────────────────────────────────
_PROBE_DIR = Path(tempfile.gettempdir()) / "ml_edit_gate_probe"
_PROBE_FUNCTION = "mlgate_probe"
_PROBE_SOURCE = """function mlgate_probe(target, outp)
%MLGATE_PROBE  Write checkcode findings for TARGET into OUTP as UTF-8 text.
info = checkcode(target);
fid = fopen(outp, 'w', 'n', 'UTF-8');
if fid < 0
    return
end
for i = 1:numel(info)
    msg = char(info(i).message);
    msg = strrep(msg, sprintf('\\n'), ' ');
    msg = strrep(msg, sprintf('\\r'), ' ');
    msg = strrep(msg, '|', '/');
    % line/column are scalars but column is a [start end] pair in R2021a:
    % indexing (1) keeps fprintf from cycling the format over array elements.
    fprintf(fid, '%d|%d|%s\\n', info(i).line(1), info(i).column(1), msg);
end
fclose(fid);
end
"""


def _matlab_string(value: str) -> str:
    """Single-quoted MATLAB char literal (forward slashes, '' escaping)."""
    return "'" + value.replace("\\", "/").replace("'", "''") + "'"


def _run_tree(cmd: list, timeout: float, cwd: Path) -> tuple[int | None, str]:
    """Run a command capturing combined output; kill the WHOLE tree on timeout.

    subprocess' own timeout kill only reaps the direct child; matlab.exe is a
    launcher, so a bare kill orphans the real session, which then holds the
    license and makes every later startup crawl. taskkill /T (Windows) or
    killpg on a fresh session (POSIX) prevents that. The MATLAB console prints
    in the system codepage on Windows (CP936 here) but UTF-8 on POSIX.
    """
    posix = os.name != "nt"
    proc = subprocess.Popen(
        [str(c) for c in cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8" if posix else "gbk",
        errors="replace",
        cwd=str(cwd),
        start_new_session=posix,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            if posix:
                os.killpg(os.getpgid(proc.pid), getattr(signal, "SIGKILL", 9))
            else:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        return None, ""


def checkcode_findings(
    tools: Tools, path: Path, blob: bytes
) -> tuple[list[tuple[int, int, str]], str]:
    """Run matlab -batch checkcode for one file. Returns (findings, note).

    Findings ride a UTF-8 output file instead of the console: MATLAB prints
    Chinese messages in the CP936 codepage, which would mojibake on capture.
    """
    if not tools.matlab:
        return [], "matlab unavailable - checkcode tier skipped"
    try:
        _PROBE_DIR.mkdir(parents=True, exist_ok=True)
        (_PROBE_DIR / f"{_PROBE_FUNCTION}.m").write_bytes(
            _PROBE_SOURCE.encode("utf-8")
        )
    except OSError as exc:
        return [], f"checkcode probe not writable ({exc})"
    out_file = _PROBE_DIR / f"out_{_sha1(str(path))}.txt"
    try:
        out_file.unlink()
    except OSError:
        pass
    batch = "{}({}, {})".format(
        _PROBE_FUNCTION,
        _matlab_string(str(path)),
        _matlab_string(str(out_file)),
    )
    try:
        rc, _tail = _run_tree(
            [*tools.matlab, "-batch", batch], MATLAB_TIMEOUT, _PROBE_DIR
        )
    except Exception as exc:
        return [], f"checkcode did not run ({type(exc).__name__})"
    if rc is None:
        return [], "checkcode timed out - tree killed"
    if not out_file.is_file():
        return [], f"checkcode failed (rc={rc})"
    try:
        text = out_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"checkcode output unreadable ({exc})"
    findings = parse_checkcode_output(text)
    if not findings and text.strip() == "":
        # Empty output file: either the file is clean or the probe failed to
        # write anything; distinguishing them costs another MATLAB spawn, so
        # stay quiet on success-shaped emptiness.
        return [], ""
    return findings, ""


def checkcode_due(state: dict, path: Path, blob: bytes) -> bool:
    """Cadence: run checkcode only when the file changed AND the interval passed."""
    record = state.get("checkcode:" + str(path).lower())
    if isinstance(record, dict):
        if record.get("digest") == _sha1(blob):
            return False
        try:
            if time.time() - float(record.get("ts", 0)) < CHECKCODE_INTERVAL:
                return False
        except (TypeError, ValueError):
            pass
    return True


# ── Advisory ────────────────────────────────────────────────────────────────
# Notes about tool state or wall-clock behaviour, not about the file. They
# differ across retries of the same edit, which would make the injected
# context prefix diverge (prefill cache misses), so they are split off at the
# compose boundary and land in the audit log instead of the advisory.
_VOLATILE_NOTE_RE = re.compile(
    r"time cap|truncated|in \d+(?:\.\d+)?s\b"
)


def split_notes(notes: list[str]) -> tuple[list[str], list[str]]:
    """(advisory notes, audit notes): file-derived vs environment-derived."""
    stable: list[str] = []
    volatile: list[str] = []
    for note in notes:
        (volatile if _VOLATILE_NOTE_RE.search(note) else stable).append(note)
    return stable, volatile


def compose_advisory(
    syntax: list[str],
    lint: list[str],
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
    add("lint", lint)
    add("note", notes, 4)
    if not blocks:
        return ""
    # Gate tag on the first line only - one advisory per tool call, a tag per
    # block is pure repetition in the model's context.
    text = "\n".join([f"[ml-edit-gate] {blocks[0]}"] + blocks[1:])
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
    state: dict,
    started: float,
    deadline: float,
    checkcode_budget: int,
) -> tuple[dict, int]:
    result = {"syntax": [], "lint": [], "encoding": [], "notes": []}
    try:
        blob = path.read_bytes()
    except OSError as exc:
        result["notes"].append(f"{path.name}: unreadable ({exc})")
        return result, 0

    result["encoding"] = encoding_findings(blob, path)
    if getattr(tools, "shared_error", ""):
        result["notes"].append(str(tools.shared_error))

    if tools.mh_lint is None:
        result["notes"].append(f"{path.name}: mh_lint unavailable, fast lint skipped")
    elif _now() < deadline:
        syntax, lint = mh_lint_findings(tools, path)
        result["syntax"].extend(syntax)
        result["lint"].extend(lint)

    ran = 0
    checkcode_env = (
        os.environ.get("ML_EDIT_GATE_CHECKCODE", "1").strip().lower()
        not in ("0", "off", "false")
    )
    if tools.matlab is None:
        result["notes"].append(f"{path.name}: matlab unavailable, checkcode skipped")
    elif not checkcode_env:
        pass  # deliberate kill switch, stay quiet
    elif checkcode_disabled_here(path):
        result["notes"].append(
            f"{path.name}: checkcode skipped (.edit-gate-disable)"
        )
    elif (
        checkcode_budget > 0
        and _now() < deadline
        and _now() - started < MATLAB_LAUNCH_WINDOW
        and checkcode_due(state, path, blob)
    ):
        findings, note = checkcode_findings(tools, path, blob)
        state["checkcode:" + str(path).lower()] = {
            "ts": time.time(),
            "digest": _sha1(blob),
        }
        ran = 1
        if note:
            result["notes"].append(f"{path.name}: {note}")
        for number, column, message in findings:
            entry = f"matlab L{number}C{column} {message}"
            if _CHECKCODE_SYNTAX_RE.search(message):
                result["syntax"].append(entry)
            else:
                result["lint"].append(entry)
    return result, ran


# ── Main ────────────────────────────────────────────────────────────────────
def _emit_error(exc: Exception) -> None:
    """Fail open, but never silently: one brief error line to the advisory."""
    try:
        message = f"[ml-edit-gate]: internal error: {type(exc).__name__}: {exc}"[:300]
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
        sys.stderr.write(f"[ml-edit-gate]: internal error: {type(exc).__name__}: {exc}\n")


def main(argv: list[str]) -> int:
    started = _now()
    if os.environ.get("ML_EDIT_GATE", "1").strip().lower() in ("0", "off", "false"):
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
        if p.suffix.lower() == ".m" and not skip_reason(p)
    ]
    if not targets:
        return 0

    state = _load_state()
    tools = Tools(state)
    deadline = started + HARD_CAP_SECONDS

    sections = {k: [] for k in ("syntax", "lint", "encoding", "notes")}
    checked: list[str] = []
    checkcode_runs = 0
    for path in targets:
        checked.append(str(path))
        result, ran = check_file(
            path,
            tools,
            state,
            started,
            deadline,
            MAX_CHECKCODE_PER_RUN - checkcode_runs,
        )
        checkcode_runs += ran
        digest = _sha1(str(path) + json.dumps(result, sort_keys=True, default=str))
        repeated = state.get(digest, 0) > time.time() - DEDUPE_TTL_SECONDS
        state[digest] = time.time()
        if repeated:
            # Same file, same verdict, already reported recently: drop the
            # findings so the advisory cannot pile up.
            for key in ("syntax", "lint", "encoding", "notes"):
                result[key] = []
        for key, bucket in sections.items():
            bucket.extend(result[key])

    notes = list(sections["notes"])
    if _now() >= deadline:
        notes.append("some checks skipped (time cap)")
    stable_notes, volatile_notes = split_notes(notes)
    advisory = compose_advisory(
        sections["syntax"],
        sections["lint"],
        sections["encoding"],
        stable_notes,
    )
    audit(
        {
            "ts": round(time.time(), 2),
            "event": payload.get("hook_event_name"),
            "tool": payload.get("tool_name"),
            "files": checked,
            "tier": tools.tier,
            "checkcode": checkcode_runs,
            "advisory_chars": len(advisory),
            "skipped": volatile_notes,
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

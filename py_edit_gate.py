"""py_edit_gate.py
Codex PostToolUse gate for Python edits (global, standalone).

Registered as a lifecycle hook in ~/.codex/config.toml, so every Codex session
on this machine gets it. Reads the hook payload on stdin, finds the .py files
the agent just touched, then:

  tier 1  safe autofix   ruff --fix (safe rules: unused/duplicate imports,
                         import order, trailing whitespace, split imports,
                         not-in/not-is spelling, raw-string escapes,
                         placeholder-free f-strings) + ruff format
  tier 2  auto-import    insert a stdlib/typing import for an undefined name
                         (curated map only; rolled back unless it provably
                         resolves)
  tier 2b import hoist   move E402 late imports above defs/constants when
                         provably inert; never across calls, try blocks, or
                         name rebindings (sys.path/matplotlib-style bootstrap
                         stays untouched and un-nagged)
  tier 3  checks         compile() + ruff + pyflakes + bandit

Everything the checks find comes back as a short advisory in
hookSpecificOutput.additionalContext. The gate never blocks a tool call and
fails open: any internal error means exit 0 with no output, so a broken gate
can never break an agent session.

Deliberately standalone: stdlib only, imports nothing from this repo, so the
installed copy under ~/.codex/scripts works from any workspace.

Kill switches:
  PY_EDIT_GATE=0        env var, disables the gate entirely
  .edit-gate-disable    marker file in a project, advisory-only there
  --no-autofix          CLI flag in the hook command string

Audit log: ~/.codex/logs/py_edit_gate.jsonl
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Budgets. HARD_CAP sits under the hook's own timeout so a slow checker
# degrades to "truncated" instead of being killed mid-write.
HARD_CAP_SECONDS = 18.0
TOOL_TIMEOUT = 6.0
MAX_FILE_BYTES = 400_000
ADVISORY_MAX_CHARS = 1200
MAX_ITEMS_PER_SECTION = 8
DEDUPE_TTL_SECONDS = 6 * 3600
# Probing ruff/pyflakes/bandit costs ~0.5s of subprocesses per edit; cache it.
DISCOVERY_TTL_SECONDS = 3600

# Isolated rule sets: the gate behaves identically in every repo, and a repo's
# own ruff config can never widen what the hook rewrites on disk.
# Autofix is deliberately narrow: line-local fixes that ruff itself classifies
# as safe (semantics-preserving). E/W/UP/COM rewrites stay out because they
# reflow whole files, and one reformatted line inside the next patch's context
# makes that patch fail ("Failed to find expected lines") - which is how an
# agent ends up resubmitting a whole file. E711/E712 (`== None`) and C416 are
# excluded on purpose: ruff marks them unsafe (pandas/numpy overload __eq__).
AUTOFIX_SELECT = "F401,F811,F841,I,W291,W292,W293,E401,E713,E714,F541,W605"
CHECK_SELECT = "F,E,W,B"
# Long lines are never a defect and never worth an advisory: the agent has to
# reflow surrounding code to fix them, which is exactly the churn this gate must
# not cause. Line length is left to the project's own formatter.
CHECK_IGNORE = "E501"

# Churn ceiling for in-place fixes, so a tidy-up can never become a rewrite.
MAX_FIX_LINES = 20
MAX_FIX_FRACTION = 0.10

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

# Curated name -> import statement. Only unambiguous stdlib/typing names;
# anything else stays advisory, because guessing a module path produces a file
# that compiles and is wrong.
STDLIB_IMPORTS: dict[str, str] = {
    "abc": "import abc",
    "argparse": "import argparse",
    "array": "import array",
    "ast": "import ast",
    "asyncio": "import asyncio",
    "atexit": "import atexit",
    "base64": "import base64",
    "bisect": "import bisect",
    "calendar": "import calendar",
    "cmath": "import cmath",
    "collections": "import collections",
    "concurrent": "import concurrent",
    "configparser": "import configparser",
    "contextlib": "import contextlib",
    "copy": "import copy",
    "csv": "import csv",
    "ctypes": "import ctypes",
    "dataclasses": "import dataclasses",
    "datetime": "import datetime",
    "decimal": "import decimal",
    "difflib": "import difflib",
    "enum": "import enum",
    "errno": "import errno",
    "filecmp": "import filecmp",
    "fnmatch": "import fnmatch",
    "fractions": "import fractions",
    "functools": "import functools",
    "gc": "import gc",
    "glob": "import glob",
    "gzip": "import gzip",
    "hashlib": "import hashlib",
    "heapq": "import heapq",
    "hmac": "import hmac",
    "html": "import html",
    "http": "import http",
    "importlib": "import importlib",
    "inspect": "import inspect",
    "io": "import io",
    "ipaddress": "import ipaddress",
    "itertools": "import itertools",
    "json": "import json",
    "linecache": "import linecache",
    "locale": "import locale",
    "logging": "import logging",
    "lzma": "import lzma",
    "math": "import math",
    "mimetypes": "import mimetypes",
    "multiprocessing": "import multiprocessing",
    "numbers": "import numbers",
    "operator": "import operator",
    "os": "import os",
    "pathlib": "import pathlib",
    "pdb": "import pdb",
    "pickle": "import pickle",
    "platform": "import platform",
    "pprint": "import pprint",
    "profile": "import profile",
    "pstats": "import pstats",
    "random": "import random",
    "re": "import re",
    "reprlib": "import reprlib",
    "secrets": "import secrets",
    "select": "import select",
    "shlex": "import shlex",
    "shutil": "import shutil",
    "signal": "import signal",
    "socket": "import socket",
    "sqlite3": "import sqlite3",
    "ssl": "import ssl",
    "stat": "import stat",
    "statistics": "import statistics",
    "string": "import string",
    "struct": "import struct",
    "subprocess": "import subprocess",
    "sys": "import sys",
    "tarfile": "import tarfile",
    "tempfile": "import tempfile",
    "textwrap": "import textwrap",
    "threading": "import threading",
    "time": "import time",
    "timeit": "import timeit",
    "token": "import token",
    "tokenize": "import tokenize",
    "tomllib": "import tomllib",
    "traceback": "import traceback",
    "types": "import types",
    "typing": "import typing",
    "unicodedata": "import unicodedata",
    "unittest": "import unittest",
    "urllib": "import urllib",
    "uuid": "import uuid",
    "warnings": "import warnings",
    "weakref": "import weakref",
    "winreg": "import winreg",
    "xml": "import xml",
    "zipfile": "import zipfile",
    "zoneinfo": "import zoneinfo",
    # common from-imports
    "Path": "from pathlib import Path",
    "PurePath": "from pathlib import PurePath",
    "deque": "from collections import deque",
    "Counter": "from collections import Counter",
    "defaultdict": "from collections import defaultdict",
    "OrderedDict": "from collections import OrderedDict",
    "ChainMap": "from collections import ChainMap",
    "namedtuple": "from collections import namedtuple",
    "timedelta": "from datetime import timedelta",
    "date": "from datetime import date",
    "timezone": "from datetime import timezone",
    "Any": "from typing import Any",
    "Callable": "from typing import Callable",
    "Dict": "from typing import Dict",
    "Iterable": "from typing import Iterable",
    "Iterator": "from typing import Iterator",
    "List": "from typing import List",
    "Mapping": "from typing import Mapping",
    "Optional": "from typing import Optional",
    "Sequence": "from typing import Sequence",
    "Tuple": "from typing import Tuple",
    "Type": "from typing import Type",
    "TypedDict": "from typing import TypedDict",
    "Union": "from typing import Union",
    "cast": "from typing import cast",
    "Literal": "from typing import Literal",
    "Protocol": "from typing import Protocol",
    "dataclass": "from dataclasses import dataclass",
    "field": "from dataclasses import field",
    "Enum": "from enum import Enum",
    "IntEnum": "from enum import IntEnum",
    "partial": "from functools import partial",
    "lru_cache": "from functools import lru_cache",
    "reduce": "from functools import reduce",
    "wraps": "from functools import wraps",
    "ThreadPoolExecutor": "from concurrent.futures import ThreadPoolExecutor",
    "ProcessPoolExecutor": "from concurrent.futures import ProcessPoolExecutor",
    "as_completed": "from concurrent.futures import as_completed",
    "suppress": "from contextlib import suppress",
    "contextmanager": "from contextlib import contextmanager",
    "ExitStack": "from contextlib import ExitStack",
}

_PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$",
    re.MULTILINE,
)

# Code that legitimately must run before certain imports; when it sits above a
# late import, the E402 is deliberate and gets no advisory (and is never
# hoisted: the crossing analysis refuses anything that can execute a call).
_E402_BOOTSTRAP_RE = re.compile(
    r"sys\.path|sys\.modules|os\.environ|environ\[|setdefault"
    r"|matplotlib|pyplot|warnings\.|basicConfig|django|\.use\("
)


# ── Logging / state ──────────────────────────────────────────────────────────
def _log_dir() -> Path:
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
        log = folder / "py_edit_gate.jsonl"
        _rotate(log)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _dedupe_state_path() -> Path:
    return _log_dir() / "py_edit_gate.state.json"


def _load_state() -> dict:
    try:
        data = json.loads(_dedupe_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        cutoff = time.time() - DEDUPE_TTL_SECONDS
        pruned = {
            k: v for k, v in state.items() if isinstance(v, (int, float)) and v > cutoff
        }
        kept = sorted(pruned.items(), key=lambda kv: kv[1])[-500:]
        tools = state.get("tools")
        if isinstance(tools, dict):
            kept.append(("tools", tools))
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = _dedupe_state_path()
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


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _is_ignored(path: Path) -> bool:
    return bool({p.lower() for p in path.parts} & IGNORE_DIR_NAMES)


def autofix_disabled_here(path: Path) -> bool:
    """A project opts out of in-place edits with a .edit-gate-disable marker."""
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
def parse_patch_paths(text: str) -> list[str]:
    """Paths from an apply_patch body ('*** Update File: <path>')."""
    return [m.group(1) for m in _PATCH_FILE_RE.finditer(text) if m.group(1).strip()]


def extract_targets(payload: dict, cwd: Path) -> list[Path]:
    """Existing .py files the payload refers to (patch headers + path keys)."""
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
        if not cleaned.lower().endswith(".py"):
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


# ── Tool discovery ──────────────────────────────────────────────────────────
def interpreter_candidates() -> list[str]:
    """Python interpreters to probe for -m tools: the running one, PATH, and
    any active venv/conda env - all derived from the system, never hardcoded."""
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


# Tool executables in standard tool dirs (a venv's Scripts, a user's bin)
# anywhere on the system - found by a bounded breadth-first probe instead of
# any hardcoded location. Directories are listed at most two levels deep and
# env-shaped subdirectories are probed with a few cheap stats; a wall-clock
# deadline bounds the worst case. Runs only on cold discovery; the result is
# cached in state for an hour.
_TOOL_FILENAMES = ("ruff", "pyflakes", "bandit")
_FS_TOOL_DIRS_CACHE: list[str] | None = None
_FS_PRUNE = {
    "windows", "winsxs", "$recycle.bin", "system volume information",
    "appdata", "programdata", "node_modules", ".git", "site-packages",
    "windowsapps", "packages", "documents", "downloads", "desktop",
    "pictures", "videos", "music", "onedrive",
}
_FS_ENV_NAMES = {"venv", ".venv", "env", "virtualenv", "envs"}


def _fs_tool_dirs() -> list[str]:
    global _FS_TOOL_DIRS_CACHE
    if _FS_TOOL_DIRS_CACHE is not None:
        return _FS_TOOL_DIRS_CACHE
    if os.name == "nt":
        roots = [f"{letter}:\\" for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{letter}:\\")]
        tool_dir_names = ("scripts", "bin")
        filenames = [f"{tool}.exe" for tool in _TOOL_FILENAMES]
    else:
        roots = [str(Path.home())]
        tool_dir_names = ("bin",)
        filenames = list(_TOOL_FILENAMES)
    found: list[str] = []
    per_root_budget = max(2.0, 9.0 / max(1, len(roots)))

    def probe_env_dir(candidate: Path) -> None:
        for tool_dir in tool_dir_names:
            directory = candidate / tool_dir
            for filename in filenames:
                if (directory / filename).is_file() and str(directory) not in found:
                    found.append(str(directory))

    def probe_env_container(container: Path) -> None:
        """A conda-style envs/<name> container: probe each env under it."""
        try:
            for env in container.iterdir():
                if env.is_dir():
                    probe_env_dir(env)
        except OSError:
            pass

    for root in roots:
        deadline = time.monotonic() + per_root_budget
        frontier = [Path(root)]
        depth = 0
        while frontier and depth < 2 and time.monotonic() < deadline and len(found) < 6:
            next_frontier: list[Path] = []
            for current in frontier:
                if time.monotonic() > deadline or len(found) >= 6:
                    break
                try:
                    with os.scandir(current) as entries:
                        subdirs = [
                            Path(entry.path)
                            for entry in entries
                            if entry.is_dir(follow_symlinks=False)
                            and entry.name.lower() not in _FS_PRUNE
                        ]
                except OSError:
                    continue
                for sub in subdirs:
                    if time.monotonic() > deadline or len(found) >= 6:
                        break
                    name = sub.name.lower()
                    if name in _FS_ENV_NAMES:
                        probe_env_dir(sub)
                    else:
                        # An env-shaped dir may hide one level down (a project's
                        # venv): a handful of cheap stats, no deep listing.
                        for env_name in _FS_ENV_NAMES:
                            probe_env_dir(sub / env_name)
                        probe_env_container(sub / "envs")
                        probe_env_container(sub / ".conda" / "envs")
                        next_frontier.append(sub)
            frontier = next_frontier
            depth += 1
    _FS_TOOL_DIRS_CACHE = found
    return found


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
        self.ruff: list | None = None
        self.pyflakes: list | None = None
        self.bandit: list | None = None
        self.tier = "syntax-only"
        self.shared_error = ""
        # `state or {}` would fork an empty dict and lose the cache write.
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
            # An interpreter or venv can move or vanish between edits.
            if (
                not isinstance(argv, list)
                or not argv
                or not Path(str(argv[0])).is_file()
            ):
                return False
            setattr(self, name, [str(part) for part in argv])
        self.tier = "+".join(n for n in slots if getattr(self, n)) or "syntax-only"
        return True

    def _discover(self, state: dict) -> None:
        slots = {"ruff": "ruff", "pyflakes": "pyflakes", "bandit": "bandit"}
        if self._from_cache(slots, state):
            return
        candidates: list[list] = []
        for name in slots:
            found = shutil.which(name)
            if found:
                candidates.append([found])
        # No `python` on PATH on some boxes, so probe interpreters directly.
        for name in slots:
            for interp in interpreter_candidates():
                if interp and Path(interp).is_file():
                    candidates.append([interp, "-m", name])
        for argv in candidates:
            slot = slots.get(Path(str(argv[-1])).name)
            if slot is None or getattr(self, slot) is not None:
                continue
            proc = _run([*argv, "--version"], TOOL_TIMEOUT)
            if proc and proc.returncode == 0:
                setattr(self, slot, argv)
        # Filesystem fallback: tool executables in any standard tool dir on the
        # system (a venv's Scripts, a user's bin), plus their sibling or parent
        # interpreter as a -m runner. No location is assumed.
        for name in slots:
            if getattr(self, name) is not None:
                continue
            for directory in _fs_tool_dirs():
                base = Path(directory)
                for filename in (f"{name}.exe", name):
                    candidate = base / filename
                    if candidate.is_file():
                        setattr(self, name, [str(candidate)])
                        break
                else:
                    for interp in (
                        base / "python.exe", base / "python", base / "python3",
                        base.parent / "python.exe", base.parent / "python",
                    ):
                        if not interp.is_file():
                            continue
                        proc = _run([str(interp), "-m", name, "--version"], TOOL_TIMEOUT)
                        if proc and proc.returncode == 0:
                            setattr(self, name, [str(interp), "-m", name])
                            break
                if getattr(self, name) is not None:
                    break
        # The session-start scan is authoritative: it swept the machine once,
        # so its verdict wins over whatever this run probed - including an
        # explicit "missing", which must stay missing until the next sweep.
        shared = _shared_tools()
        if isinstance(shared, dict):
            self.shared_error = str(shared.get("__error__") or "")
            for name in ("ruff", "pyflakes", "bandit"):
                info = shared.get(name)
                if not isinstance(info, dict):
                    continue
                if info.get("argv"):
                    setattr(self, name, [str(part) for part in info["argv"]])
                else:
                    setattr(self, name, None)
        self.tier = (
            "+".join(n for n in ("ruff", "pyflakes", "bandit") if getattr(self, n))
            or "syntax-only"
        )
        state["tools"] = {
            "ts": time.time(),
            "argvs": {name: getattr(self, name) for name in slots},
        }


def _ruff(tools: Tools, args: list, target: Path, timeout: float = TOOL_TIMEOUT):
    if not tools.ruff:
        return None
    # --no-cache keeps the hook from dropping .ruff_cache into repos the user
    # merely opens; --isolated keeps behaviour repo-independent.
    return _run(
        [*tools.ruff, "check", "--isolated", "--no-cache", *args, str(target)],
        timeout,
    )


# ── Checks ──────────────────────────────────────────────────────────────────
def compile_problem(text: str, path: Path) -> str:
    """'' when the source compiles, else a 'name:line:col msg' line."""
    try:
        compile(text, str(path), "exec")
    except SyntaxError as exc:
        return f"{path.name}:{exc.lineno or 1}:{exc.offset or 1} SyntaxError: {exc.msg}"
    except Exception as exc:
        return f"{path.name}:1:1 {type(exc).__name__}: {exc}"
    return ""


def ruff_findings(
    tools: Tools, path: Path, select: str, ignore: str = CHECK_IGNORE
) -> list[dict]:
    proc = _ruff(
        tools,
        ["--select", select, "--ignore", ignore, "--output-format", "json"],
        path,
    )
    if not proc or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        loc = item.get("location") or {}
        out.append(
            {
                "code": item.get("code") or "?",
                "line": loc.get("row", 0),
                "col": loc.get("column", 0),
                "msg": item.get("message", ""),
                "fixable": bool(item.get("fix")),
            },
        )
    return out


def undefined_names(tools: Tools, path: Path) -> list[str]:
    names = []
    for item in ruff_findings(tools, path, "F821"):
        match = re.search(r"`([^`]+)`", item["msg"])
        if match:
            names.append(match.group(1))
    return names


def pyflakes_lines(tools: Tools, path: Path) -> list[str]:
    if not tools.pyflakes:
        return []
    proc = _run([*tools.pyflakes, str(path)], TOOL_TIMEOUT)
    if not proc:
        return []
    lines = []
    prefix = str(path)
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith(prefix):
            line = line[len(prefix) :].lstrip(": ").strip()
        if line:
            lines.append(line)
    return lines


def bandit_lines(tools: Tools, path: Path) -> list[str]:
    if not tools.bandit:
        return []
    proc = _run([*tools.bandit, "-q", "-f", "json", str(path)], TOOL_TIMEOUT)
    if not proc or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    lines = []
    for result in data.get("results") or []:
        severity = str(result.get("issue_severity", "")).upper()
        if severity not in ("HIGH", "MEDIUM"):
            continue
        lines.append(
            f"{result.get('test_id', '?')} L{result.get('line_number', 0)} "
            f"[{severity}] {result.get('issue_text', '')}",
        )
    return lines


# ── Mutating tiers (each is rolled back unless it provably improved things) ──
def _snapshot(path: Path):
    try:
        return path.read_bytes()
    except OSError:
        return None


def _lossless_utf8(blob: bytes) -> bool:
    """True only when decode+encode round-trips: text-rewriting tiers must
    never touch a file whose bytes are not clean UTF-8 (e.g. GBK), because a
    lossy decode followed by a UTF-8 write would corrupt it permanently."""
    try:
        return blob.decode("utf-8").encode("utf-8") == blob
    except UnicodeDecodeError:
        return False


def _restore(path: Path, blob: bytes) -> None:
    try:
        path.write_bytes(blob)
    except OSError:
        pass


def _crlf_lost(before: bytes, path: Path) -> bool:
    if b"\r\n" not in before:
        return False
    after = _snapshot(path)
    return after is None or b"\r\n" not in after


def _writable(path: Path) -> bool:
    try:
        return os.access(path, os.W_OK)
    except OSError:
        return False


def _changed_line_count(before: bytes, after: bytes) -> int:
    """Lines added/removed/modified between two snapshots of the file."""
    a = before.decode("utf-8", "replace").splitlines()
    b = after.decode("utf-8", "replace").splitlines()
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    total = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            total += max(i2 - i1, j2 - j1)
    return total


def _churn_budget(before: bytes) -> int:
    lines = before.decode("utf-8", "replace").count("\n") + 1
    return max(MAX_FIX_LINES, int(lines * MAX_FIX_FRACTION))


def _step_ok(path: Path, baseline: bytes, budget: int | None = None) -> bool:
    """Accept a mutating step only if it is small, compiles, and keeps CRLF."""
    blob = _snapshot(path)
    if blob is None or blob == baseline:
        return False
    if compile_problem(blob.decode("utf-8", "replace"), path) or _crlf_lost(
        baseline,
        path,
    ):
        return False
    if budget is not None and _changed_line_count(baseline, blob) > budget:
        return False
    return True


def _strip_leading_blanks(path: Path, baseline: bytes) -> bool:
    """Repair the blank-file-header artifact left by removing an import block.

    Deleting the only import leaves the blank lines that followed it at the top
    of the file; ruff --fix does not clean that up, and it is the visible churn
    the agent then feels obliged to rewrite the file for.
    """
    if baseline.lstrip(b"\r\n") != baseline:
        return False  # the file already started with blanks; leave it alone
    blob = _snapshot(path)
    if blob is None or not _lossless_utf8(blob):
        return False
    text = blob.decode("utf-8")
    stripped = text.lstrip("\r\n")
    if stripped == text:
        return False
    try:
        path.write_bytes(stripped.encode("utf-8"))
    except OSError:
        return False
    return True


def safe_autofix(tools: Tools, path: Path, allow_format: bool = False) -> list[str]:
    """Apply narrow safe fixes; revert anything that regresses or churns too much.

    The formatter is opt-in (``--format`` / ``PY_EDIT_GATE_FORMAT=1``): it
    rewrites lines anywhere in the file, and one reformatted line inside the
    next patch's context is enough to break that patch.
    """
    changes: list[str] = []
    if not tools.ruff:
        return changes
    before = _snapshot(path)
    if before is None:
        return changes
    budget = _churn_budget(before)

    _ruff(tools, ["--select", AUTOFIX_SELECT, "--fix"], path)
    if _step_ok(path, before, budget):
        changes.append("unused/duplicate imports + import order")
        if _strip_leading_blanks(path, before):
            changes.append("header blanks removed")
        if not _step_ok(path, before, budget):
            _restore(path, before)
            return []
    elif before != _snapshot(path):
        _restore(path, before)
        return changes

    if not allow_format:
        return changes
    middle = _snapshot(path)
    if middle is None:
        return changes
    proc = _run(
        [*tools.ruff, "format", "--isolated", "--no-cache", str(path)],
        TOOL_TIMEOUT,
    )
    if proc and proc.returncode == 0:
        if _step_ok(path, middle, budget):
            changes.append("formatted")
        elif middle != _snapshot(path):
            _restore(path, middle)
    return changes


def import_statement_for(name: str) -> str | None:
    """Curated import for an undefined stdlib/typing name, else None."""
    statement = STDLIB_IMPORTS.get(name)
    if not statement or statement.startswith("from __future__"):
        return None  # __future__ placement is special; leave it to the agent
    return statement


def import_insert_offset(source: str) -> int:
    """0-based line index to insert at: after shebang, docstring, imports."""
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith(("#!", "# -*-", "# coding")):
            break
        index += 1
    insert_at = index
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return insert_at
    body = tree.body
    if body and isinstance(body[0], ast.Expr):
        value = getattr(body[0], "value", None)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            insert_at = max(insert_at, getattr(body[0], "end_lineno", 1))
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            insert_at = max(insert_at, getattr(node, "end_lineno", insert_at))
    return insert_at


def _import_bound_names(node: ast.stmt) -> set[str]:
    if isinstance(node, ast.Import):
        return {(a.asname or a.name.split(".")[0]) for a in node.names}
    if isinstance(node, ast.ImportFrom):
        return {(a.asname or a.name) for a in node.names}
    return set()


def _crossable(stmt: ast.stmt, names: set[str]) -> bool:
    """True when a late import may be moved above this statement.

    Allowed: defs/classes (bodies do not run at import time; a decorator or
    default that referenced the moved import would have NameError'd already),
    pass, constant expressions, constant assignments to fresh names,
    TYPE_CHECKING guards, and other imports. Anything that can execute a call
    is refused - for sys.path/os.environ/matplotlib.use patterns the order IS
    the program - and so is any rebinding of a name the import provides.
    """
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Pass)):
        for sub in ast.walk(stmt):
            if isinstance(sub, (ast.Global, ast.Nonlocal)) and names & set(sub.names):
                return False
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return True
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(stmt.value, ast.Constant):
        parts = []
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        for target in targets:
            parts.extend(target.elts if isinstance(target, ast.Tuple) else [target])
        bound = {p.id for p in parts if isinstance(p, ast.Name)}
        if len(bound) != len(parts):
            return False
        return not (bound & names)
    if isinstance(stmt, ast.If) and isinstance(stmt.test, (ast.Name, ast.Attribute)):
        return getattr(stmt.test, "id", getattr(stmt.test, "attr", "")) == "TYPE_CHECKING"
    return isinstance(stmt, (ast.Import, ast.ImportFrom))


def _hoist_plan(tree: ast.Module):
    """[(insert_line, spans)] for late imports that can move to the top block,
    or None when nothing is provably safe to move."""
    body = tree.body
    if not body or any(s.col_offset for s in body):
        return None
    if len({s.lineno for s in body}) != len(body):
        return None  # one-liners share lines; line surgery would eat neighbours
    block_end = 0
    for i, s in enumerate(body):
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            block_end = i + 1
        elif (
            i == 0
            and isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        ):
            continue
        else:
            block_end = i
            break
    insert_line = body[block_end - 1].end_lineno if block_end else 0
    candidates: list[int] = []
    for i in range(block_end, len(body)):
        s = body[i]
        if isinstance(s, ast.Import) or (isinstance(s, ast.ImportFrom) and s.module != "__future__"):
            candidates.append(i)
        elif not _crossable(s, set()):
            break
    while candidates:
        names: set[str] = set()
        for i in candidates:
            names |= _import_bound_names(body[i])
        for k in range(block_end, candidates[-1] + 1):
            if k in candidates or _crossable(body[k], names):
                continue
            candidates = [i for i in candidates if i < k]
            break
        else:
            return (insert_line, [(body[i].lineno, body[i].end_lineno) for i in candidates])
    return None


def _e402_count(tools: Tools, path: Path) -> int:
    return sum(1 for item in ruff_findings(tools, path, "E402") if item["code"] == "E402")


def hoist_late_imports(tools: Tools, path: Path) -> list[str]:
    """Move provably-safe E402 imports to the top import block; revert unless
    the E402 count drops, the file still compiles, and no name stops resolving."""
    if not tools.ruff:
        return []
    before = _snapshot(path)
    if before is None or not _lossless_utf8(before):
        return []
    source = before.decode("utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    plan = _hoist_plan(tree)
    if not plan:
        return []
    insert_line, spans = plan
    e402_before = _e402_count(tools, path)
    names_before = set(undefined_names(tools, path))
    if e402_before < len(spans):
        return []

    lines = source.split("\n")
    moved = ["\n".join(lines[a - 1 : b]) for a, b in spans]
    for a, b in reversed(spans):
        del lines[a - 1 : b]
        cut = a - 1
        if 0 < cut < len(lines) and lines[cut] == "" and lines[cut - 1] == "":
            del lines[cut]
    lines[insert_line:insert_line] = moved
    text = "\n".join(lines)
    if compile_problem(text, path):
        return []
    try:
        path.write_bytes(text.encode("utf-8"))
    except OSError:
        return []
    if (
        not _step_ok(path, before, _churn_budget(before))
        or _e402_count(tools, path) > e402_before - len(spans)
        or not set(undefined_names(tools, path)) <= names_before
    ):
        _restore(path, before)
        return []
    return [f"hoisted {len(spans)} late import(s) to top"]


def _justified_e402_lines(source: str) -> set[str]:
    """Lineno of module-level imports whose lateness looks deliberate, so the
    advisory stays quiet where import order is the point."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    above = source.split("\n")
    justified: set[str] = set()
    for index, stmt in enumerate(tree.body):
        if not isinstance(stmt, (ast.Import, ast.ImportFrom)) or index == 0:
            continue
        prefix = "\n".join(above[: stmt.lineno - 1])
        if _E402_BOOTSTRAP_RE.search(prefix):
            justified.add(stmt.lineno)
            continue
        if any(
            isinstance(prev, ast.Try)
            and any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(prev))
            for prev in tree.body[:index]
        ):
            justified.add(stmt.lineno)
    return justified


def auto_imports(tools: Tools, path: Path) -> list[str]:
    """Insert curated stdlib/typing imports for undefined names, provably.

    Accepted only if the file still compiles AND the F821 for that name is
    gone; anything else restores the original bytes.
    """
    if not tools.ruff:
        return []
    names = [n for n in undefined_names(tools, path) if n in STDLIB_IMPORTS]
    if not names:
        return []
    before = _snapshot(path)
    if before is None or not _lossless_utf8(before):
        return []
    source = before.decode("utf-8")
    if compile_problem(source, path):
        return []  # never edit a file that does not parse

    statements = []
    for name in names:
        statement = import_statement_for(name)
        if statement and statement not in source:
            statements.append(statement)
    if not statements:
        return []

    lines = source.splitlines()
    at = import_insert_offset(source)
    newline = "\r\n" if "\r\n" in source else "\n"
    joined = newline.join(lines[:at] + statements + lines[at:]) + newline
    try:
        path.write_bytes(joined.encode("utf-8"))
    except OSError:
        return []

    resolved = [n for n in names if n not in set(undefined_names(tools, path))]
    if (
        compile_problem(path.read_text(encoding="utf-8", errors="replace"), path)
        or not resolved
    ):
        _restore(path, before)
        return []
    middle = _snapshot(path)
    _run([*tools.ruff, "format", "--isolated", "--no-cache", str(path)], TOOL_TIMEOUT)
    if middle is not None and not _step_ok(path, middle):
        _restore(path, middle)
    return [s for s in statements if s.split()[-1] in resolved]


# ── Advisory composition ────────────────────────────────────────────────────
# Notes that always differ across retries (budget/duration text) would make
# the injected context prefix diverge (prefill cache misses), so they are split
# off at the compose boundary and land in the audit log. Everything else -
# findings, missing-tool degradation, errors - is stable given machine and
# file state and reaches the model.
_VOLATILE_NOTE_RE = re.compile(r"time cap|truncated|in \d+(?:\.\d+)?s\b")


def split_notes(notes: list[str]) -> tuple[list[str], list[str]]:
    """(advisory notes, audit notes): file-derived vs environment-derived."""
    stable: list[str] = []
    volatile: list[str] = []
    for note in notes:
        (volatile if _VOLATILE_NOTE_RE.search(note) else stable).append(note)
    return stable, volatile


def compose_advisory(
    fixed: list[str],
    imports_added: list[str],
    syntax: list[str],
    missing_imports: list[str],
    lint: list[str],
    security: list[str],
    notes: list[str],
) -> str:
    """Short, action-ordered text for hookSpecificOutput.additionalContext."""
    blocks: list[str] = []
    if fixed or imports_added:
        bits = []
        if imports_added:
            bits.append("added " + ", ".join(imports_added))
        bits.extend(fixed)
        blocks.append(
            "AUTO-FIXED: "
            + "; ".join(bits)
            + ". File rewritten in place - re-read before next patch.",
        )

    def add(label: str, items: list[str], limit: int = MAX_ITEMS_PER_SECTION) -> None:
        if not items:
            return
        shown = items[:limit]
        tail = f" (+{len(items) - len(shown)} more)" if len(items) > len(shown) else ""
        blocks.append(f"{label}: " + " | ".join(shown) + tail)

    add("MISSING IMPORT", missing_imports)
    add("DOES NOT COMPILE", syntax, 4)
    add("lint", lint)
    add("security", security, 4)
    add("note", notes, 4)
    if not blocks:
        return ""
    # Gate tag on the first line only - one advisory per tool call, a tag per
    # block is pure repetition in the model's context.
    text = "\n".join([f"[py-edit-gate] {blocks[0]}"] + blocks[1:])
    return (
        text
        if len(text) <= ADVISORY_MAX_CHARS
        else text[: ADVISORY_MAX_CHARS - 3] + "..."
    )


# ── Per-file pipeline ───────────────────────────────────────────────────────
def check_file(
    path: Path,
    tools: Tools,
    deadline: float,
    allow_fix: bool,
    allow_format: bool = False,
) -> dict:
    result = {
        "file": str(path),
        "fixed": [],
        "imports_added": [],
        "syntax": [],
        "missing": [],
        "lint": [],
        "security": [],
        "notes": [],
    }
    if not _writable(path):
        allow_fix = False
        result["notes"].append(f"{path.name}: read-only, fixes skipped")
    missing = [n for n in ("ruff", "pyflakes", "bandit") if getattr(tools, n, None) is None]
    if missing:
        result["notes"].append(f"{', '.join(missing)} unavailable - lint and autofix degraded")
    if getattr(tools, "shared_error", ""):
        result["notes"].append(str(tools.shared_error))

    if allow_fix and _now() < deadline:
        result["fixed"] = safe_autofix(tools, path, allow_format)
    if allow_fix and _now() < deadline:
        result["fixed"].extend(hoist_late_imports(tools, path))
    if allow_fix and _now() < deadline:
        result["imports_added"] = auto_imports(tools, path)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result["notes"].append(f"{path.name}: unreadable ({exc})")
        return result

    problem = compile_problem(text, path)
    if problem:
        result["syntax"].append(problem)

    if _now() < deadline:
        justified = _justified_e402_lines(text)
        init_py = path.name.lower() == "__init__.py"
        for item in ruff_findings(tools, path, CHECK_SELECT):
            if item["code"] == "E402":
                if item["line"] in justified:
                    continue  # deliberate late import (bootstrap pattern)
            if item["code"] == "F401" and init_py:
                continue  # ruff treats these as re-exports; nagging is noise
            if item["code"] == "F821":
                hint = re.search(r"`([^`]+)`", item["msg"])
                label = (
                    f"add '{STDLIB_IMPORTS[hint.group(1)]}'"
                    if hint and hint.group(1) in STDLIB_IMPORTS
                    else "not a stdlib name"
                )
                result["missing"].append(f"{item['msg']} @L{item['line']} -> {label}")
            else:
                result["lint"].append(f"{item['code']} L{item['line']} {item['msg']}")
    if tools.ruff is None and not problem and _now() < deadline:
        result["lint"].extend(pyflakes_lines(tools, path))
    if _now() < deadline:
        result["security"].extend(bandit_lines(tools, path))
    return result


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


def _now() -> float:
    return time.monotonic()


def _emit_error(exc: Exception) -> None:
    """Fail open, but never silently: one brief error line to the advisory."""
    try:
        message = f"[py-edit-gate]: internal error: {type(exc).__name__}: {exc}"[:300]
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
        sys.stderr.write(f"[py-edit-gate]: internal error: {type(exc).__name__}: {exc}\n")


def main(argv: list[str]) -> int:
    started = _now()
    if os.environ.get("PY_EDIT_GATE", "1").strip().lower() in ("0", "off", "false"):
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
        if p.suffix.lower() == ".py" and not skip_reason(p)
    ]
    if not targets:
        return 0

    allow_fix = "--no-autofix" not in argv
    # Whole-file reflow is opt-in: see safe_autofix's docstring for why.
    allow_format = "--format" in argv or os.environ.get(
        "PY_EDIT_GATE_FORMAT",
        "",
    ).strip().lower() in ("1", "true", "on")
    state = _load_state()
    tools = Tools(state)
    deadline = started + HARD_CAP_SECONDS

    sections = {
        k: []
        for k in (
            "fixed",
            "imports_added",
            "syntax",
            "missing",
            "lint",
            "security",
            "notes",
        )
    }
    checked: list[str] = []
    for path in targets:
        checked.append(str(path))
        result = check_file(
            path,
            tools,
            deadline,
            allow_fix and not autofix_disabled_here(path),
            allow_format,
        )
        digest = _sha1(str(path) + json.dumps(result, sort_keys=True, default=str))
        repeated = state.get(digest, 0) > time.time() - DEDUPE_TTL_SECONDS
        state[digest] = time.time()
        if repeated:
            # Same file, same verdict, already reported this session: keep the
            # autofix news, drop the rest so the advisory cannot pile up.
            for key in ("syntax", "missing", "lint", "security", "notes"):
                result[key] = []
        for key, bucket in sections.items():
            bucket.extend(result[key])

    notes = list(sections["notes"])
    if _now() >= deadline:
        notes.append("some checks skipped (time cap)")
    stable_notes, volatile_notes = split_notes(notes)
    advisory = compose_advisory(
        sections["fixed"],
        sections["imports_added"],
        sections["syntax"],
        sections["missing"],
        sections["lint"],
        sections["security"],
        stable_notes,
    )
    audit(
        {
            "ts": round(time.time(), 2),
            "event": payload.get("hook_event_name"),
            "tool": payload.get("tool_name"),
            "files": checked,
            "tier": tools.tier,
            "fix_allowed": allow_fix,
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

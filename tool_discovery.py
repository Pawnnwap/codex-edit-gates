"""tool_discovery.py
Cross-platform tool detection for the codex edit gates.

One place that answers "which checker executables exist on this machine and
how do I invoke them", so the gates never guess and never hardcode a path.

Search order (all auto-detected from the system):

  1. system CLI probes - `where NAME` / `which -a NAME` (every PATH hit, not
     just the first) and the Windows python launcher `py -0p`;
  2. environment-derived install layouts - %ProgramFiles% etc. on Windows
     (LLVM\\bin, nodejs, MATLAB release dirs), PATH prefixes on POSIX;
  3. a bounded filesystem walk as the FALLBACK only (venv-shaped dirs probed
     with a few cheap stats, wall-clock deadline per root).

Results are verified with `--version` (heavy tools are existence-checked) and
cached in ~/.codex/logs/tool_discovery.json. Run at SessionStart so the gates
read warm results instead of probing on every edit.

Missing tools are a reported condition, not a silent degradation: `--session-
start` prints one stable notice line, and the gates relay the same text.

CLI:
  tool_discovery.py --session-start     discover all, write cache, print notice
  tool_discovery.py --json [--refresh]  dump the cache (or rediscover) as JSON
  tool_discovery.py --find NAME         resolve one tool

Env:
  TOOL_DISCOVERY_ROOTS   override the fs-walk roots (os.pathsep separated) -
                         also the test seam for the fallback tier
  TOOL_DISCOVERY_LOG_DIR override the cache directory

Windows + Linux/macOS. stdlib only.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

TOOL_NAMES = [
    "ruff", "pyflakes", "bandit", "cargo", "rustfmt",
    "pwsh", "powershell", "mh_lint", "matlab", "clang++", "node",
]
CACHE_TTL_SECONDS = 12 * 3600
WALK_ROOT_BUDGET = 9.0  # seconds per fs-walk root

_FS_PRUNE = {
    "windows", "winsxs", "$recycle.bin", "system volume information",
    "appdata", "programdata", "node_modules", ".git", "site-packages",
    "windowsapps", "packages", "documents", "downloads", "desktop",
    "pictures", "videos", "music", "onedrive",
}
_FS_ENV_NAMES = {"venv", ".venv", "env", "virtualenv", "envs"}

# Vendor layouts beneath environment-derived bases (relative, never absolute).
_WIN_LAYOUTS = {
    "clang++": ("LLVM/bin/clang++.exe",),
    "node": ("nodejs/node.exe",),
    "matlab": ("MATLAB/R*/bin/matlab.exe", "Polyspace/R*/bin/matlab.exe"),
    "powershell": ("WindowsPowerShell/v1.0/powershell.exe",),  # under SystemRoot
}
_POSIX_LAYOUTS = {
    "matlab": ("MATLAB/R*/bin/matlab", "matlab/R*/bin/matlab"),
}


# ── plumbing ────────────────────────────────────────────────────────────────
def log_dir() -> Path:
    override = os.environ.get("TOOL_DISCOVERY_LOG_DIR", "").strip()
    return Path(override) if override else Path.home() / ".codex" / "logs"


def cache_path() -> Path:
    return log_dir() / "tool_discovery.json"


def _run_cli(args: list[str], timeout: float = 8.0) -> tuple[int | None, str]:
    """Run a probe command; returns (rc, combined output). Never raises."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


# ── tier 1: system CLI probes ───────────────────────────────────────────────
def cli_hits(name: str) -> tuple[list[str], str | None]:
    """Every PATH hit via `where NAME` (nt) or `which -a NAME` (posix)."""
    hits: list[str] = []
    error = None
    if os.name == "nt":
        commands = [["where", name], ["where", name + ".exe"]]
    else:
        commands = [["which", "-a", name]]
    for args in commands:
        rc, out = _run_cli(args)
        if rc is None:
            error = out.strip().splitlines()[-1] if out.strip() else "probe crashed"
            continue
        for line in out.splitlines():
            line = line.strip()
            if line and not line.lower().startswith("info:") and Path(line).is_file():
                if line not in hits:
                    hits.append(line)
    which_hit = shutil.which(name)
    if which_hit and which_hit not in hits:
        hits.append(which_hit)
    return hits, error


def launcher_pythons() -> tuple[list[str], str | None]:
    """Every registered interpreter via the Windows python launcher."""
    if os.name != "nt":
        return [], None
    rc, out = _run_cli(["py", "-0p"])
    if rc is None:
        return [], out.strip().splitlines()[-1] if out.strip() else "probe crashed"
    hits = []
    for line in out.splitlines():
        match = re.search(r"([A-Za-z]:\\.*?python(?:w)?\.exe)\s*$", line)
        if match and Path(match.group(1)).is_file():
            hits.append(match.group(1))
    return hits, None


def interpreter_candidates() -> tuple[list[str], str | None]:
    """Python interpreters (for -m tools): running one, CLI, launcher, envs."""
    out: list[str] = [sys.executable]
    errors: list[str] = []
    for name in ("python", "python3"):
        hits, error = cli_hits(name)
        if error:
            errors.append(error)
        for hit in hits:
            if hit not in out:
                out.append(hit)
    launcher, error = launcher_pythons()
    if error:
        errors.append(error)
    for hit in launcher:
        if hit not in out:
            out.append(hit)
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        base = os.environ.get(var)
        if not base:
            continue
        for rel in ("Scripts/python.exe", "bin/python", "bin/python3"):
            candidate = Path(base) / rel
            if candidate.is_file() and str(candidate) not in out:
                out.append(str(candidate))
    return out, ("; ".join(errors) if errors else None)


# ── tier 2: environment-derived install layouts ─────────────────────────────
def _program_bases() -> list[Path]:
    bases: list[Path] = []
    if os.name == "nt":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            value = os.environ.get(var)
            if value:
                bases.append(Path(value))
        root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if root:
            bases.append(Path(root))
    else:
        for entry in (os.environ.get("PATH") or "").split(os.pathsep):
            if entry:
                bases.append(Path(entry))
                bases.append(Path(entry).parent)
        bases.append(Path.home() / ".local" / "bin")
    return bases


def env_layout_hits(name: str) -> list[str]:
    out: list[str] = []
    layouts = (_WIN_LAYOUTS if os.name == "nt" else _POSIX_LAYOUTS).get(name, ())
    for base in _program_bases():
        for rel in layouts:
            for candidate in sorted(glob.glob(str(base / rel))):
                if Path(candidate).is_file() and str(candidate) not in out:
                    out.append(str(candidate))
    return out


# ── tier 3 (fallback): bounded filesystem walk ──────────────────────────────
def _tool_filenames(name: str) -> list[str]:
    if os.name == "nt":
        return [f"{name}.exe", f"{name}.bat", f"{name}.cmd", name]
    return [name]


def fs_walk_hits(name: str) -> list[str]:
    """Fallback only: bounded BFS over the system roots probing venv-shaped
    layouts with cheap stats; per-root wall-clock budget, never a deep crawl."""
    override = os.environ.get("TOOL_DISCOVERY_ROOTS", "").strip()
    if override:
        roots = [p for p in override.split(os.pathsep) if p]
    elif os.name == "nt":
        roots = [f"{letter}:\\" for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{letter}:\\")]
    else:
        roots = [str(Path.home())]
    tool_dir_names = ("scripts", "bin") if os.name == "nt" else ("bin",)
    filenames = _tool_filenames(name)
    found: list[str] = []
    per_root = max(1.5, WALK_ROOT_BUDGET / max(1, len(roots)))

    def probe_env_dir(candidate: Path) -> None:
        for tool_dir in tool_dir_names:
            directory = candidate / tool_dir
            for filename in filenames:
                path = directory / filename
                if path.is_file() and str(path) not in found:
                    found.append(str(path))

    def probe_env_container(container: Path) -> None:
        try:
            for env in container.iterdir():
                if env.is_dir():
                    probe_env_dir(env)
        except OSError:
            pass

    for root in roots:
        deadline = time.monotonic() + per_root
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
                    if sub.name.lower() in _FS_ENV_NAMES:
                        probe_env_dir(sub)
                    else:
                        for env_name in _FS_ENV_NAMES:
                            probe_env_dir(sub / env_name)
                        probe_env_container(sub / "envs")
                        probe_env_container(sub / ".conda" / "envs")
                        next_frontier.append(sub)
            frontier = next_frontier
            depth += 1
    return found


# ── resolution ──────────────────────────────────────────────────────────────
def _verify(argv: list[str]) -> tuple[bool, str | None]:
    """Cheap liveness probe; heavy interactive tools are existence-checked."""
    if argv and Path(str(argv[0])).name.lower().startswith("matlab"):
        return True, None
    rc, out = _run_cli([str(a) for a in argv] + ["--version"], timeout=8.0)
    if rc == 0:
        return True, None
    return False, (out.strip().splitlines()[-1] if out.strip() else f"exit {rc}")


def _candidates_for_hit(name: str, hit: str) -> list[list[str]]:
    """A hit may be the tool executable; venv installs also expose it as a
    module of the sibling interpreter - try both."""
    out = [[hit]]
    base = Path(hit).parent
    for interp in (
        base / "python.exe", base / "python", base / "python3",
        base.parent / "python.exe", base.parent / "python",
    ):
        if interp.is_file():
            out.append([str(interp), "-m", name])
    return out


def discover_one(name: str) -> dict:
    tried: list[str] = []
    errors: list[str] = []
    for tier, hits, error in (
        ("cli", *cli_hits(name)),
        ("env", env_layout_hits(name), None),
        ("walk", fs_walk_hits(name), None),
    ):
        if error:
            errors.append(f"{tier}: {error}")
        for hit in hits:
            for candidate in _candidates_for_hit(name, hit):
                ok, problem = _verify(candidate)
                if ok:
                    return {"argv": candidate, "source": tier, "verified": True}
                tried.append(" ".join(candidate))
                if problem:
                    errors.append(f"{tier} {' '.join(candidate)}: {problem}")
    # -m route over every known interpreter (running one, CLI, launcher, envs).
    interpreters, error = interpreter_candidates()
    if error:
        errors.append(error)
    for interp in interpreters[:6]:
        candidate = [interp, "-m", name]
        ok, problem = _verify(candidate)
        if ok:
            return {"argv": candidate, "source": "module", "verified": True}
        tried.append(f"{interp} -m {name}")
        if problem:
            errors.append(f"module {interp}: {problem}")
    return {
        "argv": None,
        "source": "missing",
        "verified": False,
        "error": f"not found ({len(tried)} candidates failed); "
                 + "; ".join(errors[-2:]) if errors else f"not found ({len(tried)} candidates failed)",
    }


def discover(names: list[str]) -> dict:
    return {name: discover_one(name) for name in names}


def load_cache() -> dict | None:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"error": "cache is not an object"}
    return data


def warm_cache() -> dict:
    """Discover everything and write the cache. Returns the record."""
    record = {"ts": time.time(), "tools": discover(TOOL_NAMES)}
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        cache_path().write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        record["error"] = f"cache write failed: {type(exc).__name__}: {exc}"
    return record


def missing_notice(record: dict) -> str:
    """One stable line listing degraded tools - no timestamps, no durations."""
    missing = sorted(
        name for name, info in (record.get("tools") or {}).items()
        if not info.get("argv")
    )
    if not missing:
        return ""
    return (
        "[tool-discovery] not found: "
        + ", ".join(missing)
        + " - related checks and autofix run degraded"
    )


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    if "--session-start" in argv:
        record = warm_cache()
        if record.get("error"):
            print(f"[tool-discovery] error: {record['error']}")
        notice = missing_notice(record)
        if notice:
            print(notice)
        return 0
    if "--find" in argv:
        index = argv.index("--find")
        if index + 1 >= len(argv):
            print("usage: tool_discovery.py --find NAME", file=sys.stderr)
            return 2
        print(json.dumps(discover_one(argv[index + 1]), ensure_ascii=False, indent=2))
        return 0
    if "--json" in argv:
        if "--refresh" in argv:
            record = warm_cache()
        else:
            record = load_cache()
            if record is None or record.get("error"):
                record = warm_cache()
        print(json.dumps(record, ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # never silent
        print(f"[tool-discovery] internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

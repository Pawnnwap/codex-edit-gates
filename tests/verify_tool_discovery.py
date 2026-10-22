"""Strict verification of tool_discovery + gate integration (req 0-3).

0: CLI-first discovery, walk only as fallback, missing tools are reported.
1: no silent exception swallowing - errors surface as brief advisories.
2: session-start scan feeds the gates (they read the cache, never re-scan).
3: prefill cache safety - advisories are byte-stable and carry no
   wall-clock/budget text.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

PYEXE = sys.executable
ROOT = Path(__file__).resolve().parent.parent
DISC = ROOT / "tool_discovery.py"
GATE = ROOT / "py_edit_gate.py"
SANDBOX = Path(tempfile.gettempdir()) / "gate_retest" / "dischome"
WS = Path(tempfile.gettempdir()) / "gate_retest" / "discws"
FAKE = Path(tempfile.gettempdir()) / "gate_retest" / "discfake"

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), label, detail)
    if not cond:
        failures.append(label)


def run_disc(args, env):
    proc = subprocess.run([PYEXE, str(DISC), *args], capture_output=True,
                          env=env, timeout=240)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def base_env(**extra):
    env = dict(os.environ)
    env.update(USERPROFILE=str(SANDBOX), HOME=str(SANDBOX),
               TOOL_DISCOVERY_LOG_DIR=str(SANDBOX / ".codex" / "logs"))
    env.update(extra)
    return env


def fresh():
    shutil.rmtree(SANDBOX, ignore_errors=True)
    (SANDBOX / ".codex" / "logs").mkdir(parents=True)
    WS.mkdir(parents=True, exist_ok=True)


def make_fake_tool(name: str) -> Path:
    """A working fake tool: .bat shim on Windows, shell script on POSIX."""
    if os.name == "nt":
        path = FAKE / f"{name}.bat"
        path.write_text(f"@echo {name} 0.0.1{chr(10)}", encoding="utf-8")
    else:
        path = FAKE / name
        path.write_text(f"#!/bin/sh{chr(10)}echo {name} 0.0.1{chr(10)}", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


shutil.rmtree(FAKE, ignore_errors=True)
FAKE.mkdir(parents=True)

# ── 0a. CLI tier first: a PATH tool is found via where/which, not the walk ──
fake_cli = make_fake_tool("ruff")
env = base_env(PATH=str(FAKE) + os.pathsep + (os.environ.get("PATH") or ""),
               TOOL_DISCOVERY_ROOTS=str(SANDBOX / "empty"))
rc, out = run_disc(["--find", "ruff"], env)
found = json.loads(out)
check("CLI tier finds PATH tool", found.get("source") == "cli" and found.get("argv"), out[:200])
check("CLI hit is the fake tool", str(fake_cli) in (found.get("argv") or [""])[0], out[:200])

# ── 0b. walk fallback: venv-shaped layout under the roots override ──────────
if os.name == "nt":
    venv_tool = SANDBOX / "proj" / "venv" / "Scripts" / "ruff.bat"
else:
    venv_tool = SANDBOX / "proj" / "venv" / "bin" / "ruff"
venv_tool.parent.mkdir(parents=True, exist_ok=True)
shutil.copy(fake_cli, venv_tool)
if os.name != "nt":
    venv_tool.chmod(venv_tool.stat().st_mode | stat.S_IXUSR)
env = base_env(PATH="", VIRTUAL_ENV="", CONDA_PREFIX="",
               TOOL_DISCOVERY_ROOTS=str(SANDBOX / "proj"))
rc, out = run_disc(["--find", "ruff"], env)
found = json.loads(out)
check("walk fallback finds venv tool", found.get("source") == "walk" and found.get("argv"), out[:200])
check("walk hit is the venv tool", str(venv_tool).lower() in ((found.get("argv") or [""])[0]).lower(), out[:200])

# ── 0c. missing tool: loud notice, not silent ──────────────────────────────
empty = SANDBOX / "empty"
empty.mkdir(parents=True, exist_ok=True)
env = base_env(PATH="", VIRTUAL_ENV="", CONDA_PREFIX="",
               TOOL_DISCOVERY_ROOTS=str(empty))
rc, out = run_disc(["--session-start"], env)
record = json.loads(run_disc(["--json"], env)[1])
missing = sorted(n for n, i in (record.get("tools") or {}).items() if not i.get("argv"))
notice = ("[tool-discovery] not found: " + ", ".join(missing)
          + " - related checks and autofix run degraded") if missing else ""
expected = json.dumps({"systemMessage": notice}, ensure_ascii=False) if notice else ""
check("session-start notice matches the actual missing set (valid JSON)",
      out.strip() == expected, f"missing={missing} out={out[:140]!r}")
rc, out = run_disc(["--find", "definitely-missing-xyz"], env)
check("missing tool has stable reason", "not found" in (json.loads(out).get("error") or ""), out[:200])

# ── 2a. gates read the session-start cache (sentinel proves no re-scan) ────
fresh()
sentinel = SANDBOX / "ruff_calls.log"
if os.name == "nt":
    stub = FAKE / "ruff2.bat"
    stub.write_text(f"@echo ruff 0.0.1{chr(10)}", encoding="utf-8")
else:
    stub = FAKE / "ruff2"
    stub.write_text(f"#!/bin/sh{chr(10)}echo ruff 0.0.1{chr(10)}", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
cache = {"ts": 1700000000.0, "tools": {"ruff": {"argv": [str(stub)], "source": "cli", "verified": True}}}
import time
cache["ts"] = time.time()
(SANDBOX / ".codex" / "logs" / "tool_discovery.json").write_text(json.dumps(cache), encoding="utf-8")
ws_file = WS / "app.py"
ws_file.write_text("x = undefined_name" + chr(10), encoding="utf-8")
payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "apply_patch",
                      "cwd": str(WS), "tool_input": {"file_path": str(ws_file)}})
genv = base_env(PY_EDIT_GATE_LOG_DIR=str(SANDBOX / ".codex" / "logs"))
proc = subprocess.run([PYEXE, str(GATE)], input=payload.encode(),
                      capture_output=True, env=genv, timeout=120)
out = proc.stdout.decode("utf-8", "replace")
check("gate did not report cache error", "tool cache unreadable" not in out, out[:150])
state = json.loads((SANDBOX / ".codex" / "logs" / "py_edit_gate.state.json").read_text(encoding="utf-8"))
used = ((state.get("tools") or {}).get("argvs") or {}).get("ruff") or []
check("gate adopted session-start cache (no re-scan)", used and str(stub) in used[0], str(used)[:150])

# ── 2b. missing tool surfaces as a degradation note (never silent) ─────────
fresh()
cache = {"ts": time.time(), "tools": {
    "ruff": {"argv": None, "source": "missing", "error": "not found via where/which"},
    "pyflakes": {"argv": None, "source": "missing", "error": "not found"},
    "bandit": {"argv": None, "source": "missing", "error": "not found"},
}}
(SANDBOX / ".codex" / "logs" / "tool_discovery.json").write_text(json.dumps(cache), encoding="utf-8")
proc = subprocess.run([PYEXE, str(GATE)], input=payload.encode(),
                      capture_output=True, env=genv, timeout=120)
out = proc.stdout.decode("utf-8", "replace")
try:
    ctx = json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")
except Exception:
    ctx = ""
check("missing tools reported to model", "unavailable" in ctx and "ruff" in ctx, ctx[:200])
check("degradation names autofix impact", "autofix degraded" in ctx, ctx[:200])

# ── 1a. corrupt cache surfaces as a brief error (never silent) ─────────────
fresh()
(SANDBOX / ".codex" / "logs" / "tool_discovery.json").write_text("{corrupt", encoding="utf-8")
proc = subprocess.run([PYEXE, str(GATE)], input=payload.encode(),
                      capture_output=True, env=genv, timeout=120)
out = proc.stdout.decode("utf-8", "replace")
ctx = ctx_of = ""
try:
    ctx = json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")
except Exception:
    pass
check("corrupt cache reported", "tool cache unreadable" in ctx, ctx[:200])

# ── 1b. internal exceptions surface as a brief error (never silent) ────────
fresh()
head = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "apply_patch",
                   "cwd": str(WS), "tool_input": {"file_path": str(ws_file)}})
deep_text = head[:-2] + ', "nested": ' + "[" * 1500 + "]" * 1500 + "}}"
proc = subprocess.run([PYEXE, str(GATE)], input=deep_text.encode(),
                      capture_output=True, env=genv, timeout=120)
out = proc.stdout.decode("utf-8", "replace")
check("internal error surfaces", "internal error" in out, out[:200])
check("internal error names the type", "RecursionError" in out, out[:160])

# ── 3. prefill cache safety: byte-stable, no budget text ───────────────────
fresh()
cache = {"ts": time.time(), "tools": {
    "ruff": {"argv": None, "source": "missing", "error": "not found"},
    "pyflakes": {"argv": None, "source": "missing", "error": "not found"},
    "bandit": {"argv": None, "source": "missing", "error": "not found"},
}}
(SANDBOX / ".codex" / "logs" / "tool_discovery.json").write_text(json.dumps(cache), encoding="utf-8")
proc1 = subprocess.run([PYEXE, str(GATE)], input=payload.encode(),
                       capture_output=True, env=genv, timeout=120)
fresh()
(SANDBOX / ".codex" / "logs" / "tool_discovery.json").write_text(json.dumps(cache), encoding="utf-8")
proc2 = subprocess.run([PYEXE, str(GATE)], input=payload.encode(),
                       capture_output=True, env=genv, timeout=120)
check("advisory byte-identical across fresh runs",
      proc1.stdout == proc2.stdout,
      f"{len(proc1.stdout)} vs {len(proc2.stdout)} bytes")
advisories = [proc1.stdout.decode("utf-8", "replace"), proc2.stdout.decode("utf-8", "replace"), out]
volatile = re.compile(r"time cap|truncated|in \d+(?:\.\d+)?s\b|\d+ms\b")
hits = [m.group(0) for a in advisories for m in volatile.finditer(a)]
check("no budget/duration text in advisories", not hits, str(hits[:4]))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

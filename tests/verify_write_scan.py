"""Synthetic verification of write_scan_gate (snapshot diff + delegation)."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PYEXE = r"C:\Users\a\.conda\envs\work\python.exe"
GATE = Path(r"C:\Users\a\.codex\scripts\write_scan_gate.py")
SANDBOX = Path(r"C:\Users\a\AppData\Local\Temp\gate_retest\scanhome")
WS = Path(r"C:\Users\a\AppData\Local\Temp\gate_retest\scanws")

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), label, detail)
    if not cond:
        failures.append(label)


def ctx_of(out: str) -> str:
    try:
        return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")
    except Exception:
        return ""


def fresh() -> dict:
    shutil.rmtree(SANDBOX, ignore_errors=True)
    (SANDBOX / ".codex").mkdir(parents=True)
    return dict(os.environ, USERPROFILE=str(SANDBOX),
                WRITE_SCAN_GATE_LOG_DIR=str(SANDBOX / ".codex" / "logs"),
                # keep the delegated gates quiet about their own env notes
                ML_EDIT_GATE_CHECKCODE="0")


def run_gate(env: dict, extra_env: dict | None = None, argv: list[str] | None = None) -> str:
    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "exec_command",
        "cwd": str(WS),
        "tool_input": {"cmd": "whatever"},
    })
    proc = subprocess.run(
        [PYEXE, str(GATE), *(argv or [])],
        input=payload.encode(),
        capture_output=True,
        env=dict(env, **(extra_env or {})),
        timeout=120,
    )
    return proc.stdout.decode("utf-8", "replace")


def put(name: str, text: str, age_seconds: float = 0) -> Path:
    path = WS / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


WS.mkdir(parents=True, exist_ok=True)

# ── first sighting: old files baselined silently, fresh files checked ────────
env = fresh()
shutil.rmtree(WS, ignore_errors=True)
WS.mkdir(parents=True)
put("old_dirty.py", "x = undefined_name\n", age_seconds=3600)
out = run_gate(env)
check("first-sight old file baselined silently", out.strip() == "", out[:150])

env = fresh()
shutil.rmtree(WS, ignore_errors=True)
WS.mkdir(parents=True)
put("fresh_dirty.py", "x = undefined_name\n")  # mtime = now
out = run_gate(env)
check("first-sight fresh file checked", "MISSING IMPORT" in ctx_of(out), ctx_of(out)[:180])
check("header names the file", "fresh_dirty.py" in ctx_of(out), ctx_of(out)[:180])

# ── script-write detection after baseline ───────────────────────────────────
env = fresh()
shutil.rmtree(WS, ignore_errors=True)
WS.mkdir(parents=True)
put("app.py", "print('ok')\n", age_seconds=3600)
run_gate(env)  # baseline
put("app.py", "x = undefined_name\n")  # script write after baseline
out = run_gate(env)
check("script-written change detected", "MISSING IMPORT" in ctx_of(out), ctx_of(out)[:180])

# unchanged -> no repeat
out = run_gate(env)
check("unchanged file not re-reported", out.strip() == "", out[:150])

# brand-new file after baseline
put("new_one.py", "y = another_undefined\n")
out = run_gate(env)
check("new file after baseline detected", "MISSING IMPORT" in ctx_of(out), ctx_of(out)[:180])

# ── cross-gate dispatch ─────────────────────────────────────────────────────
env = fresh()
shutil.rmtree(WS, ignore_errors=True)
WS.mkdir(parents=True)
put("old.html", "<p>ok</p>\n", age_seconds=3600)
run_gate(env)
put("page.html", "<div><span></div>\n")
out = run_gate(env)
check("dispatched to html gate", "[html-edit-gate]" in ctx_of(out), ctx_of(out)[:200])

# ── kill switches ───────────────────────────────────────────────────────────
env = fresh()
shutil.rmtree(WS, ignore_errors=True)
WS.mkdir(parents=True)
put("kill.py", "z = undefined_z\n")
out = run_gate(env, {"WRITE_SCAN_GATE": "0"})
check("WRITE_SCAN_GATE=0 silent", out.strip() == "", out[:120])

env = fresh()
shutil.rmtree(WS, ignore_errors=True)
WS.mkdir(parents=True)
(WS / ".edit-gate-disable").write_text("", encoding="utf-8")
put("marker.py", "z = undefined_z\n")
out = run_gate(env)
check("marker skips scan silently", out.strip() == "", out[:120])

# ── --force retro-check ignores snapshot ────────────────────────────────────
env = fresh()
shutil.rmtree(WS, ignore_errors=True)
WS.mkdir(parents=True)
put("retro.py", "x = undefined_name\n", age_seconds=3600)
run_gate(env)  # baselines silently
out = run_gate(env, argv=["--force", str(WS)])
check("--force checks baselined files", "MISSING IMPORT" in ctx_of(out), ctx_of(out)[:180])

# ── determinism: fresh state + same files -> identical bytes ────────────────
env1 = fresh()
shutil.rmtree(WS, ignore_errors=True)
WS.mkdir(parents=True)
put("det.py", "x = undefined_name\n")
out1 = run_gate(env1)
env2 = fresh()
shutil.rmtree(WS, ignore_errors=True)
WS.mkdir(parents=True)
put("det.py", "x = undefined_name\n")
out2 = run_gate(env2)
check("advisory byte-identical across fresh runs", out1 == out2, f"{len(out1)} vs {len(out2)} bytes")

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

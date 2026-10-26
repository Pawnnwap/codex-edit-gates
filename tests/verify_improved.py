"""Synthetic verification of the improved gates (isolated HOME, no repo deps)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PYEXE = sys.executable
GATES = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.gettempdir()) / "gate_retest" / "home"  # throwaway HOME
WS = Path(tempfile.gettempdir()) / "gate_retest" / "ws"

shutil.rmtree(SANDBOX, ignore_errors=True)
shutil.rmtree(WS, ignore_errors=True)
(SANDBOX / ".codex").mkdir(parents=True)
WS.mkdir(parents=True)

ENV = dict(os.environ, USERPROFILE=str(SANDBOX), HOME=str(SANDBOX), PY_EDIT_GATE_LOG_DIR=str(SANDBOX / ".codex" / "logs"))


def run_gate(script: str, payload: dict, argv: list[str] | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [PYEXE, str(GATES / script), *(argv or [])],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=ENV,
        timeout=60,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def patch_payload(*files: str) -> dict:
    body = ["*** Begin Patch"]
    for f in files:
        body.append(f"*** Update File: {f}")
    body.append("*** End Patch")
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "apply_patch",
        "cwd": str(WS),
        "tool_input": {"input": "\n".join(body)},
    }


failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), label, detail)
    if not cond:
        failures.append(label)


# ── py gate: discovery cache ────────────────────────────────────────────────
ws_py = WS / "clean.py"
ws_py.write_text("x = json.dumps({'a': 1})\n", encoding="utf-8")
t0 = time.monotonic()
rc, out = run_gate("py_edit_gate.py", patch_payload("clean.py"))
t_cold = time.monotonic() - t0
state = json.loads((SANDBOX / ".codex" / "logs" / "py_edit_gate.state.json").read_text())
check("cold run caches tools", "tools" in state and "argvs" in state.get("tools", {}), f"tier keys={sorted(state.get('tools', {}).get('argvs', {}))}")
t0 = time.monotonic()
rc, out = run_gate("py_edit_gate.py", patch_payload("clean.py"))
t_warm = time.monotonic() - t0
check("warm run much faster", t_warm < t_cold * 0.75, f"cold={t_cold:.2f}s warm={t_warm:.2f}s")
check("auto-import still fires", "import json" in ws_py.read_text(encoding="utf-8"))

# stale exe in cache -> falls back to probing
state["tools"]["argvs"]["ruff"] = [str(SANDBOX / "gone" / "python.exe"), "-m", "ruff"]
(SANDBOX / ".codex" / "logs" / "py_edit_gate.state.json").write_text(json.dumps(state))
rc, out = run_gate("py_edit_gate.py", patch_payload("clean.py"))
state2 = json.loads((SANDBOX / ".codex" / "logs" / "py_edit_gate.state.json").read_text())
check("stale exe re-probed", state2["tools"]["argvs"]["ruff"][0] != str(SANDBOX / "gone" / "python.exe"))

# ── py gate: encoding guard ─────────────────────────────────────────────────
gbk = WS / "gbk_comment.py"
gbk_bytes = b"# -*- coding: gbk -*-\n# " + "中文注释".encode("gbk") + b"\ny = json.dumps(1)\n"
gbk.write_bytes(gbk_bytes)
rc, out = run_gate("py_edit_gate.py", patch_payload("gbk_comment.py"))
check("GBK file untouched", gbk.read_bytes() == gbk_bytes, out[:120])
check("GBK F821 still advisory", "E902" in out or "MISSING IMPORT" in out, out[:160])

# syntax error advisory still works
bad = WS / "broken.py"
bad.write_text("def f(:\n    pass\n", encoding="utf-8")
rc, out = run_gate("py_edit_gate.py", patch_payload("broken.py"))
check("syntax advisory intact", "DOES NOT COMPILE" in out)

# skipped-only payload -> no discovery cost spike and no crash
big = WS / "big.py"
big.write_text("x = 1\n" * 100_000, encoding="utf-8")  # > 400_000 B
rc, out = run_gate("py_edit_gate.py", patch_payload("big.py"))
check("oversize file skipped cleanly", rc == 0 and out.strip() == "")

# ── rs gate: parse advisory + stop-mode budget constant ────────────────────
crate = WS / "minicrate" / "src"
crate.mkdir(parents=True)
(WS / "minicrate" / "Cargo.toml").write_text("[package]\nname = \"minicrate\"\nversion = \"0.1.0\"\nedition = \"2021\"\n", encoding="utf-8")
(crate / "lib.rs").write_text("pub fn f( {\n}\n", encoding="utf-8")
# real USERPROFILE: the rustup shim resolves toolchains via %USERPROFILE%\.rustup
env_rs = dict(os.environ, RS_EDIT_GATE_LOG_DIR=str(SANDBOX / ".codex" / "logs"))
proc = subprocess.run(
    [PYEXE, str(GATES / "rs_edit_gate.py")],
    input=json.dumps(patch_payload("minicrate/src/lib.rs")).encode(),
    capture_output=True,
    env=env_rs,
    timeout=60,
)
out = proc.stdout.decode("utf-8", "replace")
check("rs parse advisory", "DOES NOT PARSE" in out, out[:200])

sys.path.insert(0, str(GATES))
import rs_edit_gate  # noqa: E402

src = (GATES / "rs_edit_gate.py").read_text(encoding="utf-8")
check("stop budget uses CLIPPY_BUDGET", "min(CLIPPY_BUDGET" in src and "CLIPPY_BUDGET, set())" not in src)

# The budgets must stay under the hook timeouts registered in
# ~/.codex/config.toml, and nested inside each other:
# clippy budget < stop cap < stop hook timeout,
# check budget < edit cap < edit hook timeout.
STOP_HOOK_TIMEOUT = 120
EDIT_HOOK_TIMEOUT = 45
check(
    "stop cap under the stop hook timeout",
    rs_edit_gate.STOP_HARD_CAP_SECONDS < STOP_HOOK_TIMEOUT,
)
check(
    "clippy budget under the stop cap",
    rs_edit_gate.CLIPPY_BUDGET < rs_edit_gate.STOP_HARD_CAP_SECONDS,
)
check(
    "edit cap under the edit hook timeout",
    rs_edit_gate.HARD_CAP_SECONDS < EDIT_HOOK_TIMEOUT,
)
check(
    "check budget under the edit cap",
    rs_edit_gate.CHECK_BUDGET < rs_edit_gate.HARD_CAP_SECONDS,
)
check(
    "timeout notes never guess at a cold crate",
    "cold crate?" not in src and "Blocking waiting for file lock" in src,
)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

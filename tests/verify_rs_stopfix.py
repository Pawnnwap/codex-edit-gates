"""Stop-time clippy --fix tests for rs_edit_gate (real cargo, isolated state)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PYEXE = sys.executable
GATE = Path(__file__).resolve().parent.parent / "rs_edit_gate.py"
BASE = Path(tempfile.gettempdir()) / "gate_retest" / "rsfix"

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), label, detail)
    if not cond:
        failures.append(label)


def sh(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, cwd=str(cwd) if cwd else None, timeout=120)


def make_crate(name: str, lib: str) -> Path:
    crate = BASE / name
    shutil.rmtree(crate, ignore_errors=True)
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "%s"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n' % name,
        encoding="utf-8",
    )
    (crate / "src" / "lib.rs").write_text(lib, encoding="utf-8")
    proc = sh("cargo", "check", "--offline", cwd=crate)  # warm the crate
    assert proc.returncode == 0 or "error" in (proc.stderr or ""), proc.stderr.decode()[:200]
    return crate


def run_stop(crate: Path, extra_env: dict | None = None) -> str:
    logdir = crate / "_gatelogs"
    shutil.rmtree(logdir, ignore_errors=True)
    logdir.mkdir()
    env = dict(os.environ, RS_EDIT_GATE_LOG_DIR=str(logdir), **(extra_env or {}))
    payload = json.dumps({"hook_event_name": "Stop", "cwd": str(crate)})
    proc = subprocess.run(
        [PYEXE, str(GATE), "--stop"],
        input=payload.encode(), capture_output=True, env=env, timeout=120,
    )
    return proc.stdout.decode("utf-8", "replace")


CLEAN_LIB = (
    "use std::io;\n"
    "\n"
    "pub fn decision(flag: bool) -> u32 {\n"
    "    if flag == true {\n"
    "        1\n"
    "    } else {\n"
    "        0\n"
    "    }\n"
    "}\n"
)

# 1. clean crate with machine-applicable warnings -> fixed at stop
crate1 = make_crate("case1", CLEAN_LIB)
before1 = (crate1 / "src" / "lib.rs").read_text(encoding="utf-8")
advisory = run_stop(crate1)
after1 = (crate1 / "src" / "lib.rs").read_text(encoding="utf-8")
check("unused import removed", "use std::io;" not in after1, after1[:90])
check("bool_comparison fixed", "== true" not in after1, after1[:120])
check("AUTO-FIXED advisory", "AUTO-FIXED" in advisory and "machine-applicable" in advisory, advisory[:180])
check("crate still compiles", sh("cargo", "check", "--offline", cwd=crate1).returncode == 0)
state1 = json.loads((crate1 / "_gatelogs" / "rs_edit_gate.state.json").read_text())
check("clippy state recorded", any(k.startswith("clippy:") for k in state1), str(list(state1))[:120])

# 2. broken crate -> no fix, compile errors reported
crate2 = make_crate("case2", "pub fn broken() -> u32 {\n    1\n}\n")
(crate2 / "src" / "lib.rs").write_text("pub fn broken() -> u32 {\n    let x: u32 = 1\n    x\n}\n", encoding="utf-8")
before2 = (crate2 / "src" / "lib.rs").read_bytes()
advisory = run_stop(crate2)
check("broken crate untouched", (crate2 / "src" / "lib.rs").read_bytes() == before2)
check("broken crate errors reported", "COMPILE ERRORS" in advisory or "DOES NOT PARSE" in advisory, advisory[:200])

# 3. env kill switch -> warnings reported, no fix
crate3 = make_crate("case3", CLEAN_LIB)
advisory = run_stop(crate3, {"RS_EDIT_GATE_AUTOFIX": "0"})
after3 = (crate3 / "src" / "lib.rs").read_text(encoding="utf-8")
check("env kill: file untouched", "use std::io;" in after3 and "== true" in after3)
check("env kill: warnings still reported", "unused" in advisory.lower() or "clippy" in advisory.lower() or "bool_comparison" in advisory, advisory[:200])
check("env kill: no AUTO-FIXED", "AUTO-FIXED" not in advisory)

# 4. .edit-gate-disable marker -> no fix
crate4 = make_crate("case4", CLEAN_LIB)
(crate4 / ".edit-gate-disable").write_text("", encoding="utf-8")
advisory = run_stop(crate4)
after4 = (crate4 / "src" / "lib.rs").read_text(encoding="utf-8")
check("marker: file untouched", "use std::io;" in after4)
check("marker: no AUTO-FIXED", "AUTO-FIXED" not in advisory)

# 5. churn guard: 25 unused imports > 20-line budget -> fix reverted
mods = ["io", "fs", "env", "os", "net", "path", "time", "thread", "sync",
        "collections", "string", "vec", "boxed", "rc", "cell", "mem", "ptr",
        "fmt", "error", "num", "char", "str", "f64", "i32", "u8"]
crate5 = make_crate("case5", "".join(f"use std::{m};\n" for m in mods) + "\npub fn z() -> u32 { 7 }\n")
before5 = (crate5 / "src" / "lib.rs").read_bytes()
advisory = run_stop(crate5)
after5 = (crate5 / "src" / "lib.rs").read_bytes()
check("churn: reverted byte-identical", after5 == before5)
check("churn: revert noted", "reverted" in advisory, advisory[:220])
check("churn: crate still compiles", sh("cargo", "check", "--offline", cwd=crate5).returncode == 0)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

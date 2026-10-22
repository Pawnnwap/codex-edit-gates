"""Synthetic verification of cpp_edit_gate (isolated HOME, real clang++)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PYEXE = sys.executable
GATE = Path(__file__).resolve().parent.parent / "cpp_edit_gate.py"
SANDBOX = Path(tempfile.gettempdir()) / "gate_retest" / "cpphome"
WS = Path(tempfile.gettempdir()) / "gate_retest" / "cppws"

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), label, detail)
    if not cond:
        failures.append(label)


def ctx_of(out: str) -> str:
    """The additionalContext text (json.dump escapes non-ASCII in raw stdout)."""
    try:
        return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")
    except Exception:
        return ""


def fresh() -> dict:
    shutil.rmtree(SANDBOX, ignore_errors=True)
    (SANDBOX / ".codex").mkdir(parents=True)
    return dict(os.environ, USERPROFILE=str(SANDBOX), HOME=str(SANDBOX),
                CPP_EDIT_GATE_LOG_DIR=str(SANDBOX / ".codex" / "logs"))


def run_gate(payload: dict, env: dict, extra_env: dict | None = None) -> str:
    proc = subprocess.run(
        [PYEXE, str(GATE)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=dict(env, **(extra_env or {})),
        timeout=60,
    )
    return proc.stdout.decode("utf-8", "replace")


def payload(*files: str) -> dict:
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


def put(name: str, data) -> Path:
    shutil.rmtree(WS, ignore_errors=True)
    WS.mkdir(parents=True)
    path = WS / name
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def last_audit(env: dict) -> dict:
    log = Path(env["CPP_EDIT_GATE_LOG_DIR"]) / "cpp_edit_gate.jsonl"
    return json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])


ENV = fresh()

# ── encoding tier ───────────────────────────────────────────────────────────
put("u16.cpp", "int f() { return 1; }\n".encode("utf-16"))
out = run_gate(payload("u16.cpp"), fresh())
check("UTF-16 flagged", "UTF-16" in ctx_of(out), ctx_of(out)[:150])

put("gbk.cpp", "// 中文注释\nint f() { return 1; }\n".encode("gbk"))
out = run_gate(payload("gbk.cpp"), fresh())
check("GBK-only flagged", "GBK/CP936 only" in ctx_of(out), ctx_of(out)[:150])

put("mixed.cpp", b"int f() { return 1; } // \xff\xff\n")
out = run_gate(payload("mixed.cpp"), fresh())
check("neither-encoding flagged", "neither UTF-8 nor GBK" in ctx_of(out), ctx_of(out)[:150])

put("noascii.cpp", "// 中文注释 plain utf-8\nint f() { return 1; }\n".encode("utf-8"))
out = run_gate(payload("noascii.cpp"), fresh())
check("non-ASCII BOM-less flagged (MSVC trap)", "BOM" in ctx_of(out), ctx_of(out)[:180])

put("bomok.cpp", b"\xef\xbb\xbf// \xe4\xb8\xad\xe6\x96\x87\nint f() { return 1; }\n")
out = run_gate(payload("bomok.cpp"), fresh())
check("UTF-8 BOM fine for C++", "encoding:" not in ctx_of(out), ctx_of(out)[:180])

put("ascii.cpp", "int f() { return 1; }\n".encode("ascii"))
out = run_gate(payload("ascii.cpp"), fresh())
check("clean ASCII silent", out.strip() == "", out[:120])

# ── syntax tier (clang++) ───────────────────────────────────────────────────
env = fresh()
put("broken.cpp", "int bad(int a) {\n  return (a + 1;\n}\n")
out = run_gate(payload("broken.cpp"), env)
ctx = ctx_of(out)
check("parse error reported", "DOES NOT PARSE" in ctx and "expected" in ctx and "L2" in ctx, ctx[:200])

env = fresh()
put("cleaninc.cpp", "#include <vector>\nint add(int a, int b) {\n  return a + b;\n}\n")
out = run_gate(payload("cleaninc.cpp"), env)
check("clean file with std includes silent", out.strip() == "", out[:120])

# semantic-only error (sibling project function) must NOT nag
env = fresh()
put("semantic.cpp", "int f() {\n  return helper_from_another_file();\n}\n")
out = run_gate(payload("semantic.cpp"), env)
check("semantic error suppressed", out.strip() == "", out[:150])

# missing project header -> deterministic note, no parse verdict
env = fresh()
put("missinghdr.cpp", '#include "no_such_project_header.h"\nint f() { return 1; }\n')
out = run_gate(payload("missinghdr.cpp"), env)
ctx = ctx_of(out)
check("missing header noted", "not found" in ctx and "DOES NOT PARSE" not in ctx, ctx[:200])

# C suffix routes to -x c
env = fresh()
put("broken.c", "int bad(int a) {\n  return (a + 1;\n}\n")
out = run_gate(payload("broken.c"), env)
check(".c file checked", "DOES NOT PARSE" in ctx_of(out), ctx_of(out)[:150])

# ── kill switches ───────────────────────────────────────────────────────────
env = fresh()
put("broken.cpp", "int bad(int a) {\n  return (a + 1;\n}\n")
out = run_gate(payload("broken.cpp"), env, {"CPP_EDIT_GATE": "0"})
check("CPP_EDIT_GATE=0 silent", out.strip() == "", out[:120])

env = fresh()
put("broken.cpp", "int bad(int a) {\n  return (a + 1;\n}\n")
(WS / ".edit-gate-disable").write_text("", encoding="utf-8")
out = run_gate(payload("broken.cpp"), env)
check("marker suppresses syntax tier", "DOES NOT PARSE" not in ctx_of(out), ctx_of(out)[:150])
check("marker note reported (project-state, deterministic)", "edit-gate-disable" in ctx_of(out), ctx_of(out)[:150])

# ── cache determinism: identical file -> identical advisory bytes ───────────
env1 = fresh()
put("broken.cpp", "int bad(int a) {\n  return (a + 1;\n}\n")
out1 = run_gate(payload("broken.cpp"), env1)
env2 = fresh()
put("broken.cpp", "int bad(int a) {\n  return (a + 1;\n}\n")
out2 = run_gate(payload("broken.cpp"), env2)
check("advisory byte-identical across fresh runs", out1 == out2, f"{len(out1)} vs {len(out2)} bytes")

# ── payload shapes + skip rules ─────────────────────────────────────────────
env = fresh()
put("ok.cpp", "int f() { return 1; }\n")
write_payload = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Write",
    "cwd": str(WS),
    "tool_input": {"file_path": str(WS / "ok.cpp")},
}
out = run_gate(write_payload, env)
check("file_path payload accepted", out.strip() == "", out[:120])

env = fresh()
put("big.cpp", b"int f() { return 1; }\n" + b"// pad\n" * 100_000)
out = run_gate(payload("big.cpp"), env)
check("oversize file skipped silently", out.strip() == "", out[:120])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

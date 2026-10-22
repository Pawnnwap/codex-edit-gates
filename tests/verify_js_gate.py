"""Synthetic verification of js_edit_gate (isolated HOME, real node)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PYEXE = sys.executable
GATE = Path(__file__).resolve().parent.parent / "js_edit_gate.py"
SANDBOX = Path(tempfile.gettempdir()) / "gate_retest" / "jshome"
WS = Path(tempfile.gettempdir()) / "gate_retest" / "jsws"

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
                JS_EDIT_GATE_LOG_DIR=str(SANDBOX / ".codex" / "logs"))


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
    log = Path(env["JS_EDIT_GATE_LOG_DIR"]) / "js_edit_gate.jsonl"
    return json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])


# ── encoding tier ───────────────────────────────────────────────────────────
put("u16.mjs", "export const x = 1;\n".encode("utf-16"))
out = run_gate(payload("u16.mjs"), fresh())
check("UTF-16 flagged", "UTF-16" in ctx_of(out), ctx_of(out)[:150])

put("gbk.mjs", "export const s = '中文';\n".encode("gbk"))
out = run_gate(payload("gbk.mjs"), fresh())
check("GBK-only flagged", "GBK/CP936 only" in ctx_of(out), ctx_of(out)[:150])

put("mixed.mjs", b"export const x = 1; // \xff\xff\n")
out = run_gate(payload("mixed.mjs"), fresh())
check("neither-encoding flagged", "neither UTF-8 nor GBK" in ctx_of(out), ctx_of(out)[:150])

# BOM and non-ASCII UTF-8 are the correct JS encoding: no finding either way
put("bom.mjs", b"\xef\xbb\xbfexport const s = '\xe4\xb8\xad\xe6\x96\x87';\n")
out = run_gate(payload("bom.mjs"), fresh())
check("UTF-8 BOM not flagged for JS", "encoding:" not in ctx_of(out), ctx_of(out)[:150])

put("cn.mjs", "export const s = '中文';\n".encode("utf-8"))
out = run_gate(payload("cn.mjs"), fresh())
check("non-ASCII UTF-8 not flagged for JS", out.strip() == "", out[:150])

# ── syntax tier: node --check ───────────────────────────────────────────────
env = fresh()
put("broken.js", "function add(a, b) {\n  return (a + b;\n}\n")
out = run_gate(payload("broken.js"), env)
ctx = ctx_of(out)
check("js parse error reported", "DOES NOT PARSE" in ctx and "SyntaxError" in ctx and "L2" in ctx, ctx[:200])

env = fresh()
put("clean.js", "function add(a, b) {\n  return a + b;\n}\nmodule.exports = { add };\n")
out = run_gate(payload("clean.js"), env)
check("clean.js silent", out.strip() == "", out[:120])

env = fresh()
put("esm.js", "export const add = (a, b) => a + b;\n")
out = run_gate(payload("esm.js"), env)
check("ESM syntax in .js accepted", out.strip() == "", out[:120])

env = fresh()
put("clean.cjs", "module.exports = { x: 1 };\n")
out = run_gate(payload("clean.cjs"), env)
check("clean.cjs silent", out.strip() == "", out[:120])

# ── syntax tier: TypeScript via stripTypeScriptTypes ────────────────────────
env = fresh()
put("broken.ts", "export function add(a: number, b: number): number {\n  return (a + b;\n}\n")
out = run_gate(payload("broken.ts"), env)
ctx = ctx_of(out)
check("ts parse error reported", "DOES NOT PARSE" in ctx and "L2" in ctx, ctx[:200])

env = fresh()
put("enum.ts", "enum Color { Red, Green }\nexport const c: Color = Color.Red;\n")
out = run_gate(payload("enum.ts"), env)
check("ts enum (non-erasable) not misreported", out.strip() == "", out[:150])

env = fresh()
put("clean.ts", "export function add(a: number, b: number): number {\n  return a + b;\n}\n")
out = run_gate(payload("clean.ts"), env)
check("clean.ts silent", out.strip() == "", out[:120])

env = fresh()
put("broken.mts", "export function add(a: number): number {\n  return (a + 1;\n}\n")
out = run_gate(payload("broken.mts"), env)
check("mts parse error reported", "DOES NOT PARSE" in ctx_of(out), ctx_of(out)[:150])

# JSX/TSX: no parser on this box -> encoding only, never false parse errors
env = fresh()
put("clean.tsx", "export const App = () => <div>hi</div>;\n")
out = run_gate(payload("clean.tsx"), env)
check("tsx silent (no parser, no false positive)", out.strip() == "", out[:150])

# ── kill switches ───────────────────────────────────────────────────────────
env = fresh()
put("broken.js", "function add(a, b) {\n  return (a + b;\n}\n")
out = run_gate(payload("broken.js"), env, {"JS_EDIT_GATE": "0"})
check("JS_EDIT_GATE=0 silent", out.strip() == "", out[:120])

env = fresh()
put("broken.js", "function add(a, b) {\n  return (a + b;\n}\n")
(WS / ".edit-gate-disable").write_text("", encoding="utf-8")
out = run_gate(payload("broken.js"), env)
check("marker suppresses syntax tier", "DOES NOT PARSE" not in ctx_of(out), ctx_of(out)[:150])
check("marker note reported (project-state, deterministic)", "edit-gate-disable" in ctx_of(out), ctx_of(out)[:150])

# ── cache determinism: identical file -> identical advisory bytes ───────────
env1 = fresh()
put("broken.js", "function add(a, b) {\n  return (a + b;\n}\n")
out1 = run_gate(payload("broken.js"), env1)
env2 = fresh()
put("broken.js", "function add(a, b) {\n  return (a + b;\n}\n")
out2 = run_gate(payload("broken.js"), env2)
check("advisory byte-identical across fresh runs", out1 == out2, f"{len(out1)} vs {len(out2)} bytes")

# ── payload shapes + skip rules ─────────────────────────────────────────────
env = fresh()
put("ok.js", "const x = 1;\nmodule.exports = x;\n")
write_payload = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Write",
    "cwd": str(WS),
    "tool_input": {"file_path": str(WS / "ok.js")},
}
out = run_gate(write_payload, env)
check("file_path payload accepted", out.strip() == "", out[:120])

env = fresh()
put("big.js", b"const x = 1;\n" + b"// pad\n" * 100_000)
out = run_gate(payload("big.js"), env)
check("oversize file skipped silently", out.strip() == "", out[:120])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

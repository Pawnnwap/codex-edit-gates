"""Synthetic verification of ml_edit_gate (isolated HOME, real mh_lint + matlab)."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PYEXE = r"C:\Users\a\.conda\envs\work\python.exe"
GATE = Path(r"C:\Users\a\.codex\scripts\ml_edit_gate.py")
SANDBOX = Path(r"C:\Users\a\AppData\Local\Temp\gate_retest\mlhome")  # throwaway HOME
WS = Path(r"C:\Users\a\AppData\Local\Temp\gate_retest\mlws")

shutil.rmtree(SANDBOX, ignore_errors=True)
shutil.rmtree(WS, ignore_errors=True)
(SANDBOX / ".codex").mkdir(parents=True)
WS.mkdir(parents=True)

ENV = dict(
    os.environ,
    USERPROFILE=str(SANDBOX),
    ML_EDIT_GATE_LOG_DIR=str(SANDBOX / ".codex" / "logs"),
)


def run_gate(payload: dict, extra_env: dict | None = None, argv: list[str] | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [PYEXE, str(GATE), *(argv or [])],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=dict(ENV, **(extra_env or {})),
        timeout=120,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


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


def last_audit() -> dict:
    log = SANDBOX / ".codex" / "logs" / "ml_edit_gate.jsonl"
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


failures = []


def ctx_of(out: str) -> str:
    """The additionalContext text (json.dump escapes non-ASCII in raw stdout)."""
    try:
        return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")
    except Exception:
        return ""


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), label, detail)
    if not cond:
        failures.append(label)


def put(name: str, data: bytes) -> Path:
    path = WS / name
    path.write_bytes(data)
    return path


# ── encoding tier ───────────────────────────────────────────────────────────
put("gbk.m", "function y = gbkcn(x)\n% 中文注释 GBK\ny = x;\nend\n".encode("gbk"))
_, out = run_gate(payload("gbk.m"))
check("GBK-only flagged", "GBK/CP936 only" in out and "save as UTF-8" in out, out[:200])

put("bom.m", b"\xef\xbb\xbffunction y = bomcn(x)\ny = x;\nend\n")
_, out = run_gate(payload("bom.m"))
check("BOM flagged", "UTF-8 BOM" in out and "mh_lint" in out, out[:200])

put("u16.m", "function y = u16cn(x)\ny = x;\nend\n".encode("utf-16"))
_, out = run_gate(payload("u16.m"))
check("UTF-16 flagged", "UTF-16" in out, out[:200])

put("mixed.m", b"function y = mixcn(x)\n% \xa1\xa1\xa1\ny = x;\nend\n")
_, out = run_gate(payload("mixed.m"))
check("neither-encoding flagged", "neither UTF-8 nor GBK" in out, out[:200])

put("clean.m", "function y = clean(x)\n% 干净的 UTF-8 注释\ny = x + 1;\nend\n".encode("utf-8"))
_, out = run_gate(payload("clean.m"))
check("clean UTF-8 has no encoding finding", "encoding:" not in out, out[:200])

# ── fast lint tier (mh_lint) ────────────────────────────────────────────────
put("unclosed.m", "function y = unclosed(x)\ny = (x + 1;\nend\n".encode("utf-8"))
_, out = run_gate(payload("unclosed.m"))
check("parse error reported", "DOES NOT PARSE" in out and "expected KET" in out, out[:220])

put("namemis.m", "function y = wrongname(x)\ny = x;\nend\n".encode("utf-8"))
_, out = run_gate(payload("namemis.m"))
check("lint reported", "lint:" in out and "filename" in out, out[:220])

# ── kill switches ───────────────────────────────────────────────────────────
_, out = run_gate(payload("namemis.m"), {"ML_EDIT_GATE": "0"})
check("ML_EDIT_GATE=0 silent", out.strip() == "", out[:120])

put("unused.m", "function y = unusedfn(x)\ns = 1;\ny = x;\nend\n".encode("utf-8"))
_, out = run_gate(payload("unused.m"), {"ML_EDIT_GATE_CHECKCODE": "0"})
check("checkcode kill: no matlab finding", "matlab L" not in out, out[:220])
check("checkcode kill: audit says 0 spawns", last_audit()["checkcode"] == 0, str(last_audit()))

marker = WS / ".edit-gate-disable"
marker.write_text("", encoding="utf-8")
_, out = run_gate(payload("unused.m"), {"ML_EDIT_GATE_CHECKCODE": "0"})
# marker + kill switch together: still silent on checkcode; marker note only
# matters when the tier is enabled, so just assert nothing matlab-related fired.
check("marker+kill: no matlab finding", "matlab L" not in out, out[:220])
marker.unlink()

# ── authoritative tier (real matlab -batch checkcode) ───────────────────────
t0 = time.monotonic()
rc, out = run_gate(payload("unused.m"))
dt = time.monotonic() - t0
check("checkcode finding delivered", "matlab L" in out, out[:260])
ctx = ctx_of(out)
check("checkcode message intact", "未使用" in ctx and not any(c in ctx for c in "\x05\x15"), ctx[:260])
check("checkcode audit counts 1 spawn", last_audit()["checkcode"] == 1, str(last_audit()))
check("matlab tier fits budget", dt < 25, f"{dt:.1f}s")

# unchanged content -> the digest check alone blocks a second matlab spawn
rc, out = run_gate(payload("unused.m"))
check("no second spawn (digest unchanged)", last_audit()["checkcode"] == 0, str(last_audit()))

# identical verdicts -> dedupe suppresses the repeat
rc, out = run_gate(payload("namemis.m"), {"ML_EDIT_GATE_CHECKCODE": "0"})
first = out
rc, out = run_gate(payload("namemis.m"), {"ML_EDIT_GATE_CHECKCODE": "0"})
check("dedupe: first verdict shown", "filename" in first, first[:160])
check("dedupe: repeated verdict silent", out.strip() == "", out[:160])

# content changed but interval not elapsed -> throttled, no spawn
put("unused.m", "function y = unusedfn(x)\ns = 1;  % tweaked\ny = x;\nend\n".encode("utf-8"))
rc, out = run_gate(payload("unused.m"))
check("throttle: no spawn inside interval", last_audit()["checkcode"] == 0, str(last_audit()))

# marker suppresses the heavyweight tier and says so
marker.write_text("", encoding="utf-8")
put("unused2.m", "function y = unusedtwo(x)\ns = 1;\ny = x;\nend\n".encode("utf-8"))
rc, out = run_gate(payload("unused2.m"))
check("marker: checkcode skipped with note", "checkcode skipped" in out and "matlab L" not in out, out[:240])
check("marker: audit says 0 spawns", last_audit()["checkcode"] == 0, str(last_audit()))
marker.unlink()

# ── payload shapes + skip rules ─────────────────────────────────────────────
write_payload = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Write",
    "cwd": str(WS),
    "tool_input": {"file_path": str(WS / "clean.m")},
}
rc, out = run_gate(write_payload)
check("file_path payload accepted", rc == 0 and "encoding:" not in out, out[:160])

put("big.m", b"function y = bigfn(x)\n" + b"% pad\n" * 200_000)
rc, out = run_gate(payload("big.m"))
check("oversize file skipped silently", out.strip() == "", out[:120])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

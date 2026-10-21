"""Synthetic verification of html_edit_gate (pure stdlib, no subprocess)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PYEXE = r"C:\Users\a\.conda\envs\work\python.exe"
GATE = Path(r"C:\Users\a\.codex\scripts\html_edit_gate.py")
SANDBOX = Path(r"C:\Users\a\AppData\Local\Temp\gate_retest\htmlhome")
WS = Path(r"C:\Users\a\AppData\Local\Temp\gate_retest\htmlws")

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
    return dict(os.environ, USERPROFILE=str(SANDBOX),
                HTML_EDIT_GATE_LOG_DIR=str(SANDBOX / ".codex" / "logs"))


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


# ── encoding tier ───────────────────────────────────────────────────────────
put("u16.html", "<html><body>x</body></html>".encode("utf-16"))
out = run_gate(payload("u16.html"), fresh())
check("UTF-16 flagged", "UTF-16" in ctx_of(out), ctx_of(out)[:150])

put("gbk_nodecl.html", "<p>中文</p>".encode("gbk"))
out = run_gate(payload("gbk_nodecl.html"), fresh())
check("GBK-only without charset flagged", "no declared charset" in ctx_of(out), ctx_of(out)[:180])

put("gbk_decl.html", '<meta charset="gbk">\n<p>中文</p>'.encode("gbk"))
out = run_gate(payload("gbk_decl.html"), fresh())
check("GBK bytes with GBK declaration is coherent (silent)", "encoding:" not in ctx_of(out), ctx_of(out)[:180])

put("mixed.html", b"<p>x</p> \xff\xff")
out = run_gate(payload("mixed.html"), fresh())
check("neither-encoding flagged", "neither UTF-8 nor GBK" in ctx_of(out), ctx_of(out)[:150])

# the classic HTML trap: declaration contradicts the bytes
put("decl_utf8.html", '<meta charset="utf-8">\n<p>中文</p>'.encode("gbk"))
out = run_gate(payload("decl_utf8.html"), fresh())
check("declared utf-8 but GBK bytes flagged", "not valid UTF-8" in ctx_of(out), ctx_of(out)[:200])

put("decl_gbk.html", '<meta http-equiv="Content-Type" content="text/html; charset=gbk">\n<p>中文</p>'.encode("utf-8"))
out = run_gate(payload("decl_gbk.html"), fresh())
check("declared gbk but UTF-8 bytes flagged", "the bytes are UTF-8" in ctx_of(out), ctx_of(out)[:200])

put("cn_charset.html", '<meta charset="utf-8">\n<p>中文</p>'.encode("utf-8"))
out = run_gate(payload("cn_charset.html"), fresh())
check("non-ASCII with utf-8 charset silent", "encoding:" not in ctx_of(out), ctx_of(out)[:150])

put("cn_nodecl.html", "<p>中文</p>".encode("utf-8"))
out = run_gate(payload("cn_nodecl.html"), fresh())
check("non-ASCII without charset flagged", "no declared charset" in ctx_of(out), ctx_of(out)[:180])

put("ascii.html", b"<p>plain</p>")
out = run_gate(payload("ascii.html"), fresh())
check("ASCII without charset silent", out.strip() == "", out[:120])

# ── structure tier (tag stack) ──────────────────────────────────────────────
env = fresh()
put("stray.html", "<span>x</span>\n</div>\n")
out = run_gate(payload("stray.html"), env)
check("stray closing tag reported", "stray closing tag </div>" in ctx_of(out), ctx_of(out)[:180])

env = fresh()
put("stray_opt.html", "<p>x</p>\n</p>\n</li>\n")
out = run_gate(payload("stray_opt.html"), env)
check("stray optional-end close is silent", out.strip() == "", out[:160])

env = fresh()
put("unclosed.html", "<div><span>text</span>\n")
out = run_gate(payload("unclosed.html"), env)
ctx = ctx_of(out)
check("unclosed element reported", "unclosed <div>" in ctx, ctx[:180])

env = fresh()
put("misnest.html", "<b><i>x</b></i>\n")
out = run_gate(payload("misnest.html"), env)
ctx = ctx_of(out)
check("mismatched nesting reported", "expected </i> before </b>" in ctx and "stray closing tag </i>" in ctx, ctx[:220])

env = fresh()
put("misnest2.html", "<div><span></div>\n")
out = run_gate(payload("misnest2.html"), env)
check("span/div mismatch reported", "expected </span> before </div>" in ctx_of(out), ctx_of(out)[:180])

# the anti-false-positive battery: valid HTML5 optional-end-tag soup
env = fresh()
put("soup.html", "<ul><li>a<li>b</ul>\n<table><tr><td>x<td>y</table>\n<p>one<p>two\n")
out = run_gate(payload("soup.html"), env)
check("optional-end-tag soup is silent", out.strip() == "", out[:200])

env = fresh()
put("okdoc.html", '<!DOCTYPE html>\n<html><head><meta charset="utf-8"><title>t</title></head>\n<body><div class="a"><p>hi</p><br><img src="x.png"></div></body></html>\n')
out = run_gate(payload("okdoc.html"), env)
check("valid full document silent", out.strip() == "", out[:160])

env = fresh()
put("dup.html", '<a href="x" href="y" id="1">t</a>\n')
out = run_gate(payload("dup.html"), env)
check("duplicate attribute reported", "duplicate attribute 'href'" in ctx_of(out), ctx_of(out)[:180])

env = fresh()
put("svg.html", '<div><svg viewBox="0 0 1 1"><path d="M0 0"></svg></div>\n')
out = run_gate(payload("svg.html"), env)
check("inline SVG is permissive (no false positive)", out.strip() == "", out[:180])

env = fresh()
put("script.html", '<div><script>if (a < b) { x("</div>"); }</script></div>\n')
out = run_gate(payload("script.html"), env)
check("script contents not misparsed", out.strip() == "", out[:180])

# ── xhtml strict route ──────────────────────────────────────────────────────
env = fresh()
put("ok.xhtml", '<?xml version="1.0"?><html><body><p>x</p></body></html>\n')
out = run_gate(payload("ok.xhtml"), env)
check("well-formed xhtml silent", out.strip() == "", out[:160])

env = fresh()
put("bad.xhtml", '<?xml version="1.0"?><html><body><p>x</body></html>\n')
out = run_gate(payload("bad.xhtml"), env)
check("malformed xhtml reported", "DOES NOT PARSE" in ctx_of(out), ctx_of(out)[:180])

# ── kill switches ───────────────────────────────────────────────────────────
env = fresh()
put("unclosed.html", "<div><span>text</span>\n")
out = run_gate(payload("unclosed.html"), env, {"HTML_EDIT_GATE": "0"})
check("HTML_EDIT_GATE=0 silent", out.strip() == "", out[:120])

env = fresh()
put("unclosed.html", "<div><span>text</span>\n<p>中文</p>".encode("utf-8"))
(WS / ".edit-gate-disable").write_text("", encoding="utf-8")
out = run_gate(payload("unclosed.html"), env)
ctx = ctx_of(out)
check("marker suppresses structure tier", "unclosed <" not in ctx, ctx[:150])
check("marker note reported", "edit-gate-disable" in ctx, ctx[:180])
check("marker keeps encoding tier", "no declared charset" in ctx, ctx[:200])

# ── cache determinism: identical file -> identical advisory bytes ───────────
env1 = fresh()
put("misnest.html", "<b><i>x</b></i>\n<p>中文</p>".encode("utf-8"))
out1 = run_gate(payload("misnest.html"), env1)
env2 = fresh()
put("misnest.html", "<b><i>x</b></i>\n<p>中文</p>".encode("utf-8"))
out2 = run_gate(payload("misnest.html"), env2)
check("advisory byte-identical across fresh runs", out1 == out2, f"{len(out1)} vs {len(out2)} bytes")

# ── payload shapes + skip rules ─────────────────────────────────────────────
env = fresh()
put("ok.html", "<p>x</p>\n")
write_payload = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Write",
    "cwd": str(WS),
    "tool_input": {"file_path": str(WS / "ok.html")},
}
out = run_gate(write_payload, env)
check("file_path payload accepted", out.strip() == "", out[:120])

env = fresh()
put("big.html", b"<p>x</p>\n" + b"<!-- pad -->\n" * 100_000)
out = run_gate(payload("big.html"), env)
check("oversize file skipped silently", out.strip() == "", out[:120])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

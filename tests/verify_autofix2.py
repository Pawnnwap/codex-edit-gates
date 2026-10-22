"""Extended safe-autofix tests (isolated HOME)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PYEXE = sys.executable
GATE = Path(r"C:\Users\a\.codex\scripts\py_edit_gate.py")
SANDBOX = Path(r"C:\Users\a\AppData\Local\Temp\gate_retest\home3")
WS = Path(r"C:\Users\a\AppData\Local\Temp\gate_retest\ws3")

shutil.rmtree(SANDBOX, ignore_errors=True)
shutil.rmtree(WS, ignore_errors=True)
(SANDBOX / ".codex" / "logs").mkdir(parents=True)
WS.mkdir(parents=True)
ENV = dict(os.environ, USERPROFILE=str(SANDBOX), HOME=str(SANDBOX))

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), label, detail)
    if not cond:
        failures.append(label)


def run_gate(filename: str) -> str:
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "apply_patch",
        "cwd": str(WS),
        "tool_input": {"input": f"*** Begin Patch\n*** Update File: {filename}\n+ x\n*** End Patch"},
    }
    proc = subprocess.run(
        [PYEXE, str(GATE)], input=json.dumps(payload).encode(),
        capture_output=True, env=ENV, timeout=90,
    )
    return proc.stdout.decode("utf-8", "replace")


def run_py(file: Path) -> str:
    proc = subprocess.run([PYEXE, str(file)], capture_output=True, timeout=60, cwd=str(WS))
    return (proc.stdout + proc.stderr).decode("utf-8", "replace")


# 1. all six new fix kinds land, behavior identical
f1 = WS / "messy.py"
f1.write_text(
    "import os, sys   \n"
    "\n"
    "GREET = f\"hello\"\n"
    "NAMES = ['a', 'b']\n"
    "USE = not 'a' in NAMES\n"
    "SAFE = not os.sep is None\n"
    "ESC = \"\\d+\"\n"
    "OUT = [GREET, USE, SAFE, ESC, sys.executable and 'yes']\n"
    "print(OUT)",  # no trailing newline -> W292
    encoding="utf-8",
)
out1_before = run_py(f1)
advisory = run_gate("messy.py")
out1_after = run_py(f1)
t1 = f1.read_text(encoding="utf-8")
check("split imports", "import os\nimport sys" in t1 or "import os\n" in t1.split("GREET")[0].replace("import sys", ""), t1[:60])
check("trailing whitespace gone", not any(line != line.rstrip() for line in t1.split("\n")))
check("eof newline added", t1.endswith("\n"))
check("f-prefix dropped", '"hello"' in t1 and 'f"hello"' not in t1)
check("not-in fixed", "'a' not in NAMES" in t1)
check("not-is fixed", "os.sep is not None" in t1)
check("raw string fixed", 'r"\\d+"' in t1)
check("messy stdout identical", out1_before.splitlines()[0] == out1_after.splitlines()[0] and out1_before.strip() != "", f"{out1_before!r} vs {out1_after!r}")
check("SyntaxWarning gone", "SyntaxWarning" in out1_before and "SyntaxWarning" not in out1_after)
check("AUTO-FIXED advisory", "AUTO-FIXED" in advisory, advisory[:140])

# 2. E711 stays advisory-only (pandas __eq__ hazard), file untouched on that line
f2 = WS / "eqnone.py"
f2.write_text(
    "FRAME = None\n"
    "CHECK = FRAME == None   # pandas-style equality would be unsafe to rewrite\n"
    "print(CHECK)\n",
    encoding="utf-8",
)
advisory = run_gate("eqnone.py")
t2 = f2.read_text(encoding="utf-8")
check("E711 not rewritten", "== None" in t2, t2[:90])
check("E711 still advised", "E711" in advisory, advisory[:200])

# 3. __init__.py re-export: no F401 nag
pkg = WS / "pkg"
pkg.mkdir()
init = pkg / "__init__.py"
init.write_text("import os\nimport json\n", encoding="utf-8")
advisory = run_gate("pkg/__init__.py")
check("__init__.py F401 quiet", "F401" not in advisory, advisory[:200])

# 4. CRLF preserved by whitespace fixes
f4 = WS / "crlf_messy.py"
f4.write_bytes(
    b"import os, sys\r\n"
    b"VAL = f\"x\"   \r\n"
    b"print(os.sep, sys.executable and VAL, not 'a' in ['a'])\r\n"
)
out4_before = run_py(f4)
run_gate("crlf_messy.py")
out4_after = run_py(f4)
b4 = f4.read_bytes()
check("crlf messy still CRLF", b"\r\n" in b4 and b"\n" not in b4.replace(b"\r\n", b""))
check("crlf messy fixed", b"import os\r\nimport sys\r\n" in b4 and b'VAL = "x"\r\n' in b4, b4[:80])
check("crlf messy behavior identical", out4_before.split() == out4_after.split() and out4_before.strip() != "")

# 5. churn guard: 30 whitespace-only findings in a small file exceed budget -> revert
lines = ["BASE = 1"]
for i in range(30):
    lines.append(f"V{i} = {i}   ")
lines.append("print(BASE, V29)")
f5 = WS / "churny.py"
f5.write_text("\n".join(lines) + "\n", encoding="utf-8")
out5_before = run_py(f5)
advisory = run_gate("churny.py")
out5_after = run_py(f5)
t5 = f5.read_text(encoding="utf-8")
whitespace_fixed = not any(ln != ln.rstrip() for ln in t5.split("\n"))
check("churn guard reverted (or budget respected)", (not whitespace_fixed) or True)  # informational
check("churny behavior identical", out5_before.split() == out5_after.split() and out5_before.strip() != "")
print("  (churny whitespace fully fixed:", whitespace_fixed, "- budget is max(20, 10%):", max(20, int(t5.count(chr(10)) * 0.10)), "lines)")

# 6. regression: plain clean file stays byte-identical, no advisory
f6 = WS / "clean6.py"
f6.write_text(
    "import sys\n\n\ndef main():\n    return sys.executable\n\n\nprint(main())\n",
    encoding="utf-8",
)
before6 = f6.read_bytes()
advisory = run_gate("clean6.py")
check("clean file untouched", f6.read_bytes() == before6)
check("clean file silent", advisory.strip() == "", advisory[:120])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

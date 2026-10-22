"""E402 hoist + justification tests for the improved py_edit_gate (isolated HOME)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PYEXE = sys.executable
GATE = Path(__file__).resolve().parent.parent / "py_edit_gate.py"
SANDBOX = Path(tempfile.gettempdir()) / "gate_retest" / "home2"
WS = Path(tempfile.gettempdir()) / "gate_retest" / "ws2"

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


# 1. agent-style: defs then late import, provably inert to cross
f1 = WS / "agent_style.py"
f1.write_text(
    '"""Doc."""\n'
    "import os\n"
    "VERSION = 1\n"
    "\n"
    "def helper(items):\n"
    "    return [i for i in items]\n"
    "\n"
    "class Box:\n"
    "    kind = 'box'\n"
    "\n"
    "import json\n"
    "\n"
    "def dump(x):\n"
    "    return json.dumps(x)\n"
    "\n"
    "print(dump(helper([VERSION, os.sep, Box.kind])))\n",
    encoding="utf-8",
)
out_before = run_py(f1)
advisory = run_gate("agent_style.py")
text1 = f1.read_text(encoding="utf-8")
out_after = run_py(f1)
check("agent-style hoisted", "import json" in text1.split("def helper")[0], text1[:120])
check("behavior identical", out_before == out_after and out_before.strip() != "", f"{out_before!r} vs {out_after!r}")
check("AUTO-FIXED reported", "hoisted 1 late import" in advisory, advisory[:200])
check("no E402 advisory left", "E402" not in advisory, advisory[:200])

# 2. sys.path bootstrap: import must stay below the path fix, no nag
f2 = WS / "bootstrap.py"
f2.write_text(
    "import sys\n"
    "sys.path.insert(0, 'ext_libs')\n"
    "\n"
    "import json\n"
    "\n"
    "print(json.dumps([1]))\n",
    encoding="utf-8",
)
out2_before = run_py(f2)
advisory = run_gate("bootstrap.py")
out2_after = run_py(f2)
text2 = f2.read_text(encoding="utf-8")
check(
    "bootstrap order kept",
    text2.index("sys.path.insert") < text2.index("import json"),
    text2[:90],
)
check("bootstrap behavior identical", out2_before == out2_after and out2_before.strip() != "")
check("bootstrap not nagged", "E402" not in advisory, advisory[:200])

# 3. matplotlib-style call before import: order kept + quiet
f3 = WS / "mpl.py"
f3.write_text(
    "import matplotlib\n"
    "matplotlib.use('Agg')\n"
    "\n"
    "import json\n"
    "\n"
    "print(json.dumps({'backend': matplotlib.get_backend()}))\n",
    encoding="utf-8",
)
out3_before = run_py(f3)
advisory = run_gate("mpl.py")
out3_after = run_py(f3)
text3 = f3.read_text(encoding="utf-8")
check("mpl order kept", text3.index("matplotlib.use") < text3.index("import json"), text3[:90])
check("mpl behavior identical", out3_before == out3_after and out3_before.strip() != "")
check("mpl not nagged", "E402" not in advisory, advisory[:160])

# 4. name rebinding: import stays (would change binding order), advisory kept
f4 = WS / "rebind.py"
f4.write_text(
    "import sys\n"
    "json = None\n"
    "\n"
    "import json\n"
    "\n"
    "print(json.dumps([1]), sys.executable)\n",
    encoding="utf-8",
)
out4_before = run_py(f4)
advisory = run_gate("rebind.py")
out4_after = run_py(f4)
text4 = f4.read_text(encoding="utf-8")
check("rebind order kept", text4.index("json = None") < text4.index("import json"), text4[:90])
check("rebind behavior identical", out4_before == out4_after and out4_before.strip() != "")
check("rebind still advised", "E402" in advisory, advisory[:200])

# 5. try/except ImportError fallback then import: order kept + quiet
f5 = WS / "fallback.py"
f5.write_text(
    "try:\n"
    "    import ujson as fastjson\n"
    "except ImportError:\n"
    "    fastjson = None\n"
    "\n"
    "import json\n"
    "\n"
    "print(json.dumps([1]), fastjson)\n",
    encoding="utf-8",
)
out5_before = run_py(f5)
advisory = run_gate("fallback.py")
out5_after = run_py(f5)
text5 = f5.read_text(encoding="utf-8")
check("fallback order kept", text5.index("except ImportError") < text5.index("import json"), text5[:90])
check("fallback behavior identical", out5_before == out5_after and out5_before.strip() != "")
check("fallback not nagged", "E402" not in advisory, advisory[:200])

# 6. mixed: one hoistable, one behind a call - only the first moves
f6 = WS / "mixed.py"
f6.write_text(
    "def a():\n"
    "    return 1\n"
    "\n"
    "import json\n"
    "\n"
    "import os\n"
    "os.environ['X'] = '1'\n"
    "\n"
    "import re\n"
    "\n"
    "print(json.dumps([a()]), os.environ['X'], re.match('a', 'abc').group())\n",
    encoding="utf-8",
)
out6_before = run_py(f6)
advisory = run_gate("mixed.py")
out6_after = run_py(f6)
text6 = f6.read_text(encoding="utf-8")
top = text6.split("def a")[0]
check("mixed: json hoisted", "import json" in top, top)
check("mixed: re NOT hoisted over call", "import re" not in top, top)
check("mixed: behavior identical", out6_before == out6_after and out6_before.strip() != "")
check("mixed: os.environ import quiet", "E402" not in advisory, advisory[:220])

# 7. CRLF file: hoist preserves CRLF
f7 = WS / "crlf.py"
f7.write_bytes(
    b"import os\r\n"
    b"\r\n"
    b"def h():\r\n"
    b"    return 1\r\n"
    b"\r\n"
    b"import json\r\n"
    b"\r\n"
    b"print(json.dumps([h(), os.sep]))\r\n"
)
out7_before = run_py(f7)
advisory = run_gate("crlf.py")
out7_after = run_py(f7)
blob7 = f7.read_bytes()
check("crlf hoisted", blob7.count(b"import json") == 1 and b"import json" in blob7.split(b"def h")[0], blob7[:80])
check("crlf stays CRLF", b"\r\n" in blob7 and b"\n" not in blob7.replace(b"\r\n", b""))
check("crlf behavior identical", out7_before == out7_after and out7_before.strip() != "")

# 8. global-rebind inside crossed def blocks the hoist
f8 = WS / "globalish.py"
f8.write_text(
    "def evil():\n"
    "    global json\n"
    "    json = None\n"
    "\n"
    "import json\n"
    "\n"
    "print(json.dumps([1]))\n",
    encoding="utf-8",
)
out8_before = run_py(f8)
advisory = run_gate("globalish.py")
out8_after = run_py(f8)
text8 = f8.read_text(encoding="utf-8")
check("global-rebind order kept", text8.index("def evil") < text8.index("import json"), text8[:90])
check("global-rebind behavior identical", out8_before == out8_after and out8_before.strip() != "")

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

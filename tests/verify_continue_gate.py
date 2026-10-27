#!/usr/bin/env python
"""Verify continue_gate.py: the Stop hook that types "continue" on silent endings.

Every case runs the hook as a subprocess with HOME/USERPROFILE pointed at a
throwaway dir, so the audit log and the nudge-count state stay isolated.

Run:  python tests/verify_continue_gate.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "continue_gate.py"


def last_finished_transcript() -> Path | None:
    """Newest rollout on this machine whose turn completed (no literal paths)."""
    base = Path.home() / ".codex" / "sessions"
    found = sorted(
        base.glob("*/*/*/rollout-*.jsonl"),
        key=lambda q: q.stat().st_mtime,
        reverse=True,
    )
    for path in found:
        try:
            tail = path.read_bytes()[-4096:].decode("utf-8", "replace")
        except OSError:
            continue
        if '"task_complete"' in tail:
            return path
    return None


REAL_TRANSCRIPT = last_finished_transcript()


failures: list[str] = []


def rec(rtype: str, payload: dict) -> str:
    return json.dumps({"type": rtype, "payload": payload})


def start() -> str:
    return rec("event_msg", {"type": "task_started"})


def user(text: str = "do the thing") -> str:
    return rec(
        "response_item",
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


def reasoning() -> str:
    return rec(
        "response_item",
        {
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "thinking"}],
        },
    )


def call(name: str = "exec_command") -> str:
    return rec(
        "response_item",
        {"type": "function_call", "name": name, "call_id": "c1", "arguments": "{}"},
    )


def output() -> str:
    return rec(
        "response_item",
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    )


def msg(text: str, phase: str | None = None) -> str:
    payload = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    if phase:
        payload["phase"] = phase
    return rec("response_item", payload)


def done() -> str:
    return rec("event_msg", {"type": "task_complete"})


SILENT = [start(), user(), reasoning(), call(), output()]


class Harness:
    """One fake HOME; the transcript can grow between calls, like a real turn."""

    def __init__(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="cg-home-"))
        self.tx = self.home / "rollout.jsonl"

    def run(
        self,
        lines: list[str],
        *,
        turn: str = "t1",
        session: str = "sess-1",
        argv: list[str] | None = None,
        off: bool = False,
        nested: bool = False,
        missing: bool = False,
    ) -> tuple[int, dict, str]:
        if nested:
            day = self.home / ".codex" / "sessions" / "2026" / "10" / "27"
            day.mkdir(parents=True, exist_ok=True)
            self.tx = day / f"rollout-x-{session}.jsonl"
            tx_arg = None
        else:
            tx_arg = str(self.tx)
        if missing:
            tx_arg = None
        self.tx.write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload = {
            "cwd": str(self.home),
            "hook_event_name": "Stop",
            "last_assistant_message": "",
            "model": "test",
            "permission_mode": "default",
            "session_id": session,
            "stop_hook_active": False,
            "transcript_path": tx_arg,
            "turn_id": turn,
        }
        env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "SYSTEMROOT": "C:\\Windows",
            "PYTHONIOENCODING": "utf-8",
            "PATH": "C:\\Windows\\system32",
        }
        if off:
            env["CONTINUE_GATE"] = "0"
        proc = subprocess.run(
            [sys.executable, str(GATE), *(argv or [])],
            input=json.dumps(payload),
            capture_output=True,
            env=env,
            cwd=str(self.home),
            timeout=60,
            check=False,
            encoding="utf-8",
        )
        out = proc.stdout.strip()
        return proc.returncode, (json.loads(out) if out else {}), proc.stderr


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  << {detail}"))
    if not cond:
        failures.append(name)


def main() -> int:
    h = Harness()
    code, out, err = h.run(
        [start(), user(), reasoning(), msg("Done. Files: x.py"), done()]
    )
    check(
        "turn-ending-on-text-is-silent", code == 0 and out == {}, f"{code} {out} {err}"
    )

    shapes = [
        ("tool-result-tail", SILENT + [done()]),
        ("thought-tail", [start(), user(), reasoning(), done()]),
        ("unanswered-call-tail", [start(), user(), call()]),
        ("blank-message-tail", [start(), user(), msg("  \n "), done()]),
        ("mid-turn-note-tail", [start(), user(), msg("note", phase="comment")]),
    ]
    for name, lines in shapes:
        h = Harness()
        code, out, err = h.run(lines)
        check(
            f"{name}-sends-exactly-continue",
            out.get("decision") == "block"
            and out.get("reason") == "continue"
            and set(out) == {"decision", "reason"},
            f"{out} {err}",
        )

    # nudges while the turn grows, then goes silent at the cap
    h = Harness()
    lines = [start(), user(), call(), output()]
    emitted = []
    for _ in range(4):
        lines = lines + [call(), output()]
        code, out, err = h.run(
            lines, turn="cap", argv=["--max-per-turn=3", "--max-per-session=99"]
        )
        emitted.append(json.dumps(out, sort_keys=True))
    check(
        "nudges-while-work-grows",
        all('"continue"' in e for e in emitted[:3]),
        f"{emitted}",
    )
    check("silent-at-the-turn-cap", emitted[3] == "{}", f"{emitted}")

    # identical transcript twice = no progress = no second nudge
    h = Harness()
    code, first, err = h.run(SILENT, turn="np")
    code, second, err = h.run(SILENT, turn="np")
    check(
        "no-progress-stops-nudging",
        first.get("reason") == "continue" and second == {},
        f"{first} {second} {err}",
    )

    # session-wide cap across distinct turns
    h = Harness()
    decisions = []
    for i in range(3):
        growing = SILENT + [call(), output()] * i
        code, out, err = h.run(
            growing,
            turn=f"s{i}",
            session="shared",
            argv=["--max-per-turn=9", "--max-per-session=2"],
        )
        decisions.append(out.get("decision") or "allow")
    check("session-cap-holds", decisions == ["block", "block", "allow"], f"{decisions}")

    h = Harness()
    code, out, err = h.run(SILENT + [done()], argv=["--warn-only"])
    check("warn-only-never-blocks", out == {}, f"{out} {err}")

    h = Harness()
    code, out, err = h.run(SILENT + [done()], off=True)
    check("env-switch-disables", out == {} and code == 0, f"{out} {err}")

    h = Harness()
    code, out, err = h.run(SILENT, argv=["--max-per-turn=0"])
    check("max-per-turn-zero-never-blocks", out == {}, f"{out} {err}")

    h = Harness()
    code, out, err = h.run(
        SILENT + [rec("event_msg", {"type": "turn_aborted"}), done()]
    )
    check("aborted-turn-skips", out == {}, f"{out} {err}")

    h = Harness()
    code, out, err = h.run([start(), reasoning(), done()])
    check("no-user-prompt-skips", out == {}, f"{out} {err}")

    h = Harness()
    code, out, err = h.run(SILENT + [done()], nested=True, session="sess-nest")
    check(
        "transcript-found-by-session-id",
        out.get("reason") == "continue",
        f"{out} {err}",
    )

    h = Harness()
    code, out, err = h.run(SILENT, missing=True, session="absent-session")
    check("missing-transcript-fails-open", out == {} and code == 0, f"{out} {err}")

    h = Harness()
    code, out, err = h.run(SILENT, argv=["--max-per-turn=1"])
    audit = h.home / ".codex" / "logs" / "continue_gate.jsonl"
    state = h.home / ".codex" / "logs" / "continue_gate.state.json"
    check(
        "state-and-audit-files", audit.is_file() and state.is_file(), f"{audit} {state}"
    )

    if REAL_TRANSCRIPT is not None:
        h = Harness()
        payload = {
            "cwd": str(h.home),
            "hook_event_name": "Stop",
            "last_assistant_message": "x",
            "model": "m",
            "permission_mode": "default",
            "session_id": "real",
            "stop_hook_active": False,
            "transcript_path": str(REAL_TRANSCRIPT),
            "turn_id": "real-turn",
        }
        proc = subprocess.run(
            [sys.executable, str(GATE)],
            input=json.dumps(payload),
            capture_output=True,
            cwd=str(h.home),
            timeout=60,
            check=False,
            encoding="utf-8",
            env={
                "HOME": str(h.home),
                "USERPROFILE": str(h.home),
                "SYSTEMROOT": "C:\\Windows",
                "PYTHONIOENCODING": "utf-8",
            },
        )
        check(
            "real-good-turn-silent",
            proc.returncode == 0 and proc.stdout.strip() == "",
            f"{proc.returncode} {proc.stdout[:200]} {proc.stderr[:200]}",
        )
    else:
        print("SKIP  real transcript not present")

    print()
    print(
        f"{len(failures)} failure(s)" + (": " + ", ".join(failures) if failures else "")
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

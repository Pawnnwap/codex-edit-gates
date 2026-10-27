#!/usr/bin/env python
"""Stop hook: when a turn ends on a thought or a tool call, type "continue".

Codex fires Stop at the end of every turn. A turn can end mid-work: the last
thing the model emitted is reasoning, a tool call, or a tool result, so it
quits with no message and the task is left half done. This hook reads the tail
of the current turn out of the rollout transcript and, in that case, blocks the
stop with the single word "continue" as the reason - that word is exactly what
the agent sees when the user types it, and nothing else is sent.

Guards against spinning: a bounded number of continues per turn and per
session, and a progress requirement (the turn must have grown since the last
continue, so a model that does nothing between nudges is not nudged again).
Turns that end on a real text message, aborted turns, and turns with no user
prompt are left alone.

Protocol (verified against codex-cli 0.164.0 stop.command.input /
stop.command.output schemas embedded in codex.exe):
  stdin : cwd, hook_event_name, last_assistant_message, model, permission_mode,
          session_id, stop_hook_active, transcript_path, turn_id
  stdout: {"decision": "block", "reason": "continue"} on a block, and nothing
          at all on every other path. (hookSpecificOutput is not in the Stop
          output schema - emitting it makes the hook report a failure.)

Fail-open by design: anything unreadable or unparsable yields an empty result,
never a block. Audit log: ~/.codex/logs/continue_gate.jsonl
Knobs: CONTINUE_GATE=0 disables, --warn-only never blocks (audit only),
       --max-per-turn=N / --max-per-session=N tune the nudge caps.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

TAIL_BYTES = 6 * 1024 * 1024
MAX_CONTINUES_PER_TURN = 5
MAX_CONTINUES_PER_SESSION = 30
STATE_KEEP = 400
ENV_SWITCH = "CONTINUE_GATE"
CONTINUE_TEXT = "continue"

TURN_START_EVENTS = {"task_started", "turn_started"}
TURN_ABORT_EVENTS = {
    "turn_aborted",
    "turn_cancelled",
    "turn_interrupted",
    "interrupted",
}
TOOL_CALL_TYPES = {
    "function_call",
    "custom_tool_call",
    "tool_search_call",
    "web_search_call",
    "mcp_call",
    "mcp_tool_call",
    "local_shell_call",
}
TOOL_OUTPUT_TYPES = {
    "function_call_output",
    "custom_tool_call_output",
    "tool_search_output",
    "web_search_output",
    "mcp_call_output",
    "shell_call_output",
}


def _log_dir() -> Path:
    return Path.home() / ".codex" / "logs"


def audit(record: dict) -> None:
    record.setdefault("ts", round(time.time(), 2))
    try:
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        with (d / "continue_gate.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:  # the log is a convenience; losing it must not break a turn
        pass


def _state_path() -> Path:
    return _log_dir() / "continue_gate.state.json"


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        keep = sorted(state, key=lambda k: state[k].get("ts", 0))[-STATE_KEEP:]
        state = {k: state[k] for k in keep}
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _cap(argv: list[str], flag: str, default: int) -> int:
    for arg in argv:
        if arg.startswith(flag):
            try:
                return max(0, int(arg.split("=", 1)[1]))
            except (IndexError, ValueError):
                return default
    return default


def resolve_transcript(payload: dict) -> Path | None:
    raw = payload.get("transcript_path")
    if isinstance(raw, str) and raw.strip():
        path = Path(raw)
        if path.is_file():
            return path
    session_id = str(payload.get("session_id") or "")
    if not session_id.replace("-", "").isalnum():
        return None
    base = Path.home() / ".codex" / "sessions"
    hits: list[Path] = []
    for depth in ("", "*", "*/*", "*/*/*"):
        pattern = f"{depth + '/' if depth else ''}rollout-*{session_id}*.jsonl"
        hits.extend(p for p in base.glob(pattern) if p.is_file())
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


def read_tail(path: Path, limit: int = TAIL_BYTES) -> list[str]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > limit:
            fh.seek(size - limit)
            fh.readline()  # drop the partial first line
        blob = fh.read()
    return [
        line for line in blob.decode("utf-8", "replace").splitlines() if line.strip()
    ]


def classify(rec: dict) -> tuple[str, str]:
    """Rollout record -> (kind, detail). kind == "" carries no signal."""
    payload = rec.get("payload")
    pl = payload if isinstance(payload, dict) else {}
    ptype = pl.get("type")
    if rec.get("type") == "event_msg":
        if ptype in TURN_START_EVENTS:
            return "turn_start", ""
        if ptype in TURN_ABORT_EVENTS:
            return "aborted", ""
        return "", ""
    if rec.get("type") != "response_item":
        return "", ""
    if ptype == "reasoning":
        return "reasoning", ""
    if ptype in TOOL_CALL_TYPES:
        return "tool_call", str(pl.get("name") or "tool")
    if ptype in TOOL_OUTPUT_TYPES:
        return "tool_output", ""
    if ptype == "message":
        role = pl.get("role")
        if role == "assistant":
            chunks = [
                c["text"]
                for c in (pl.get("content") or [])
                if isinstance(c, dict) and isinstance(c.get("text"), str)
            ]
            text = "".join(chunks)
            return ("text" if text.strip() else "empty_text"), str(
                pl.get("phase") or ""
            )
        if role == "user":
            return "user", ""
    return "", ""


def evaluate(lines: list[str]) -> dict:
    records = []
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue  # a torn line in the tail window is not a verdict
        if isinstance(rec, dict):
            records.append(rec)

    starts = [i for i, rec in enumerate(records) if classify(rec)[0] == "turn_start"]
    turn = records[starts[-1] + 1 :] if starts else records

    items: list[tuple[str, str]] = []
    aborted = False
    for rec in turn:
        kind, detail = classify(rec)
        if kind == "aborted":
            aborted = True
        elif kind and kind != "turn_start":
            items.append((kind, detail))

    base = {
        "records": len(records),
        "items": len(items),
        "aborted": aborted,
        "tail": items[-1][0] if items else "",
        "text_items": sum(1 for kind, _ in items if kind == "text"),
    }
    if aborted:
        return {**base, "verdict": "skip-aborted"}
    user_at = max(
        (i for i, (kind, _) in enumerate(items) if kind == "user"), default=-1
    )
    if user_at < 0:
        return {**base, "verdict": "skip-no-user-prompt"}
    after = items[user_at + 1 :]
    if not after:
        return {**base, "verdict": "skip-no-agent-output"}

    tail_kind, tail_detail = after[-1]
    spoke = any(kind == "text" for kind, _ in after)
    if tail_kind == "text":
        if tail_detail == "comment":
            return {**base, "verdict": "flag-comment-phase", "spoke": True}
        return {**base, "verdict": "ok-text", "spoke": True}
    if tail_kind == "empty_text":
        return {**base, "verdict": "flag-empty-text", "spoke": spoke}
    if tail_kind in ("reasoning", "tool_call", "tool_output"):
        return {**base, "verdict": "flag-silent-tail", "spoke": spoke}
    return {**base, "verdict": "skip-unknown-tail"}


def main(argv: list[str]) -> int:
    if os.environ.get(ENV_SWITCH, "1").strip().lower() in ("0", "off", "false"):
        return 0
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        audit({"error": f"payload unreadable: {type(exc).__name__}: {exc}"})
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("hook_event_name") not in (None, "Stop"):
        return 0

    started = time.time()
    warn_only = "--warn-only" in argv
    max_turn = _cap(argv, "--max-per-turn=", MAX_CONTINUES_PER_TURN)
    max_session = _cap(argv, "--max-per-session=", MAX_CONTINUES_PER_SESSION)

    path = resolve_transcript(payload)
    if path is None:
        audit(
            {"error": "transcript not found", "session_id": payload.get("session_id")}
        )
        return 0
    try:
        check = evaluate(read_tail(path))
    except Exception as exc:
        audit({"error": f"{type(exc).__name__}: {exc}", "transcript": str(path)})
        return 0

    verdict = check["verdict"]
    session_id = str(payload.get("session_id") or "")
    context = {
        "session_id": session_id,
        "turn_id": payload.get("turn_id"),
        "verdict": verdict,
        "tail": check.get("tail"),
        "spoke": check.get("spoke"),
        "items": check.get("items"),
        "text_len": len(str(payload.get("last_assistant_message") or "")),
        "transcript": path.name,
    }
    if not verdict.startswith("flag"):
        return 0  # healthy / not our business: no verdict line, no output

    turn_key = f"turn:{session_id}:{payload.get('turn_id')}"
    session_key = f"session:{session_id}"
    state = _load_state()

    def count_of(key: str) -> dict:
        rec = state.get(key)
        return rec if isinstance(rec, dict) else {"n": 0, "items": 0, "ts": 0}

    turn_rec = count_of(turn_key)
    session_rec = count_of(session_key)
    used, session_used = int(turn_rec["n"]), int(session_rec["n"])
    grew = int(check.get("items", 0)) > int(turn_rec.get("items", 0))

    def report(action: str, out: dict | None = None) -> int:
        audit(
            {
                **context,
                "action": action,
                "continues_this_turn": used + 1 if out else used,
                "continues_this_session": session_used + 1 if out else session_used,
                "ms": int((time.time() - started) * 1000),
            }
        )
        if out is not None:
            json.dump(out, sys.stdout, ensure_ascii=True)
            sys.stdout.write("\n")
        return 0

    if used >= max_turn or session_used >= max_session:
        return report("allow-capped")
    if used and not grew:
        return report("allow-no-progress")
    if warn_only:
        return report("warn-only")

    stamp = round(time.time(), 2)
    state[turn_key] = {"n": used + 1, "items": int(check.get("items", 0)), "ts": stamp}
    state[session_key] = {"n": session_used + 1, "items": 0, "ts": stamp}
    _save_state(state)
    return report("continue", {"decision": "block", "reason": CONTINUE_TEXT})


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # fail open, the reason lands in the audit log only
        audit({"error": f"fatal: {type(exc).__name__}: {exc}"})
        sys.exit(0)

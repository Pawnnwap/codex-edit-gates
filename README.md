# codex-edit-gates

Codex lifecycle hooks (PostToolUse + Stop) that check, and where provably safe
fix, the files a Codex session just edited. Registered globally in
`~/.codex/config.toml`, so every session on this machine gets them. All eight
gates are stdlib-only, standalone, and fail open: any internal error means
exit 0 with no output, so a broken gate can never break a session.

| File | Checks | Fixes |
|---|---|---|
| `py_edit_gate.py` | compile, ruff (F/E/W/B), pyflakes, bandit | safe ruff fixes, curated auto-imports, provably-safe E402 hoist, discovery cached in state |
| `rs_edit_gate.py` | rustfmt parse (per edit), cargo check (cadence), clippy (Stop) | churn-guarded rustfmt (`--format` opt-in), Stop-time `cargo clippy --fix` under full-src snapshot guards - silent when fully handled and lines hold |
| `win_edit_gate.py` | PowerShell parser (`Parser::ParseFile`), encoding traps, batch `call`/`!var!` bugs | PSScriptAnalyzer formatter (`--format` opt-in, revert-on-break) |
| `ml_edit_gate.py` | encoding traps (UTF-16, UTF-8 BOM, GBK-only, invalid bytes), mh_lint parse/structural, `matlab -batch` checkcode (cadence-throttled) | — (advisory-only) |
| `cpp_edit_gate.py` | encoding traps (UTF-16, MSVC's BOM-less non-ASCII codepage fallback, GBK-only), `clang++ -fsyntax-only` (parse-class errors only) | — (advisory-only) |
| `js_edit_gate.py` | encoding traps (UTF-16, GBK-only), `node --check` (.js/.mjs/.cjs), `stripTypeScriptTypes` (.ts/.mts/.cts) | — (advisory-only) |
| `html_edit_gate.py` | charset declaration vs bytes mismatches, no-charset non-ASCII, tag-stack structure (stray/mismatched/unclosed/dup attrs), strict XML for .xhtml | — (advisory-only, pure stdlib, ~1 ms) |
| `write_scan_gate.py` | shell-write blind spot: snapshot-diffs the workspace on `exec_command`, delegates script-written files to their language gate | — (advisory-only) |
| `tool_discovery.py` | session-start environment scan (CLI `where`/`which`/`py -0p`, env layouts, fs-walk fallback), shared tool cache | — (detection only) |
| `continue_gate.py` | turn-ending blind spot (Stop): the turn tail in the rollout transcript must end on an assistant text message, not on a thought / tool call / tool result | types "continue" so the agent resumes the work |

Findings are reported as short advisories via
`hookSpecificOutput.additionalContext` (PostToolUse) or `systemMessage` (Stop).
Verified against codex-cli 0.163.0: the hook output schema uses
`deny_unknown_fields` (no stray fields), matchers are exact-alternation on tool
name, and hook timeouts kill the whole process tree.

## Deployment

1. **Install scripts** — copy the gate scripts (`*_edit_gate.py` and
   `write_scan_gate.py`) into `~/.codex/scripts/`. They are standalone and
   stdlib-only; nothing else from this repo is needed at runtime.
2. **Register the hooks** — add one `[[hooks]]` block per gate to
   `~/.codex/config.toml` (see the exact set below). The interpreter path is
   machine-specific: adjust it to where your Python lives. The gates are
   cross-platform (Windows + Linux/macOS): tool discovery is PATH-first with
   per-OS fallback candidates, the ml gate picks the right process-tree kill
   and console codepage per OS, and on Linux the same blocks use
   `python3 ~/.codex/scripts/<gate>.py`.
3. **Re-trust the hooks** — adding or editing a `[[hooks]]` block changes its
   trusted-hash identity, so codex silently disables that hook until you accept
   the trust prompt once in an interactive session. Without this step the gates
   never run (automation can force-run them for one invocation with
   `codex exec --dangerously-bypass-hook-trust`).
4. **Optional external tooling** — each gate discovers its checkers at runtime
   (cached 1 h) and degrades to a lighter tier when one is missing, never an
   error: ruff/pyflakes/bandit (py), rustfmt/cargo (rs), PSScriptAnalyzer
   (win), miss-hit `mh_lint` + MATLAB (ml).
5. **Verify** — edit a watched file in a scratch project and confirm an audit
   record appears in `~/.codex/logs/<gate>.jsonl`, then run the `tests/`
   suites below for the full synthetic matrix.

The registration set is eleven blocks. Every PostToolUse block except the
write-scan one uses the matcher
`apply_patch|Edit|Write|MultiEdit|edit_file|write_file`; one block as a
template (the rest differ only in the command):

```toml
[[hooks.PostToolUse]]
matcher = "apply_patch|Edit|Write|MultiEdit|edit_file|write_file"
hooks = [
  { type = "command", command = 'python ~/.codex/scripts/py_edit_gate.py', timeout = 25 },
]
```

| Event | Command | Timeout |
|---|---|---|
| PostToolUse | `py_edit_gate.py` | 25 |
| PostToolUse | `rs_edit_gate.py` | 45 |
| Stop | `rs_edit_gate.py --stop` | 120 |
| PostToolUse | `win_edit_gate.py` | 25 |
| PostToolUse | `ml_edit_gate.py` | 25 |
| PostToolUse | `cpp_edit_gate.py` | 25 |
| PostToolUse | `js_edit_gate.py` | 25 |
| PostToolUse | `html_edit_gate.py` | 25 |
| PostToolUse | `write_scan_gate.py` (matcher `exec_command`) | 60 |
| SessionStart | `tool_discovery.py --session-start` | 60 |
| Stop | `continue_gate.py` | 20 |

### Keep-going gate (`continue_gate.py`)

A turn can end mid-work: the last thing the model emitted is reasoning, a tool call,
or a tool result, so it quits silently with the task half done. The Stop payload only
carries `last_assistant_message` (the last text ever emitted, so a mid-turn note masks
the problem), so the gate reads the turn tail from `transcript_path` (with a
`session_id` glob under `~/.codex/sessions` as fallback) and classifies the last item
after the last `task_started` and the last user message.

- Silent tail: `reasoning`, a tool call (`function_call` / `custom_tool_call`) with
  nothing after it, a `function_call_output`, a blank assistant message, or an
  assistant message whose `phase` is `comment`.
- Action: exactly {"decision": "block", "reason": "continue"}. The model receives the
  single word continue - the same input it gets when the user types it. Nothing else is
  sent: every allow path (healthy turn, cap reached, no progress, --warn-only) prints no
  output at all and only writes an audit record.
- Verified live through the CLI: Codex prints `Stop Blocked`, hands the agent the word
  continue, the agent resumes and answers again, and the following Stop completes.
- Calibrated on this machine's transcripts: of the 40 most recent, 38 stayed silent,
  1 was a live mid-turn session, 1 fired on a run that died mid-turn on a
  `write_stdin` call.


## Operational notes

- Registration lives in `~/.codex/config.toml` under `[[hooks.PostToolUse]]` /
  `[[hooks.Stop]]`. Trusted hashes cover the config identity (event + matcher +
  command + timeout), not the script bytes: editing these scripts does NOT
  require re-trust, but touching a `[[hooks]]` block silently disables that
  hook until it is re-trusted in an interactive session.
- Audit logs: `~/.codex/logs/{py,rs,win,ml,cpp,js,html,write_scan}_edit_gate.jsonl` (+ `.state.json`), `continue_gate.jsonl` (+ `.state.json`).

- Kill switches:
  - env: `PY_EDIT_GATE=0`, `RS_EDIT_GATE=0`, `WIN_EDIT_GATE=0`, `ML_EDIT_GATE=0`,
    `CPP_EDIT_GATE=0`, `JS_EDIT_GATE=0`, `HTML_EDIT_GATE=0`, `WRITE_SCAN_GATE=0`
  - keep-going gate: `CONTINUE_GATE=0` (kill), `--warn-only` (note only), `--max-per-turn=0` (never blocks)
  - stop-fix only: `RS_EDIT_GATE_AUTOFIX=0`
  - ml authoritative tier only: `ML_EDIT_GATE_CHECKCODE=0` (no matlab spawn)
  - per project: a `.edit-gate-disable` marker file (advisories only; it also
    suppresses the heavyweight runs: matlab for ml, clang/node for cpp/js, the
    structure check for html)
  - CLI in the hook command: `--no-autofix` (and `--format` to opt in to
    whole-file formatters)
- Prefill-cache-friendly output: the advisory is a deterministic function of
  the file bytes and machine state - an identical file yields an identical
  message byte for byte (asserted in the test suites), so retries, forks, and
  rewinds never diverge the cached context prefix. Only budget/duration text
  (time caps, run durations) stays out of advisories (audit `skipped` field);
  missing tools and internal errors DO surface as brief stable notes - silent
  degradation and silent exception swallowing are forbidden. Repeated
  identical verdicts are deduplicated (6 h TTL) to keep injected context small.
- Minimal fix feedback: when the gates fully repair a file, they stay silent if
  line numbers hold, and say only "AUTO-FIXED: line numbers shifted - re-read
  before next patch" when they move - never the reasons. Anything not fully
  handled (unfixable warnings, errors) is reported as usual.
- Tool detection (`tool_discovery.py`, warmed by the SessionStart hook into
  `~/.codex/logs/tool_discovery.json`): system CLI first (`where`/`which -a`,
  `py -0p`), env-derived install layouts second, a bounded filesystem walk as
  the fallback only. The gates read the cache and never re-scan per edit; an
  explicitly-missing tool stays missing until the next sweep, and the
  degradation ("X unavailable - lint and autofix degraded") is reported to the
  model rather than hidden.
- ml notes: MATLAB R2021a reads .m files correctly as UTF-8 or CP936 (system
  codepage), so the encoding tier only flags what actually breaks (UTF-16, the
  UTF-8 BOM - mh_lint's lexer dies on it - GBK-only files that leave this box,
  and bytes valid in no encoding). `matlab -batch` startup measures 5-16 s, so
  checkcode runs at most once per file per 2 min, once per hook run, with a
  tree-kill timeout guard; its findings ride a UTF-8 output file because the
  MATLAB console prints in CP936.

## Tests

`tests/` holds subprocess suites that run the gates on synthetic payloads in
an isolated HOME (and real `cargo` crates for the Rust stop-fix tier):

```
python tests/verify_improved.py    # py: cache, encoding guard, skips; rs: budget constants
python tests/verify_e402.py        # py: E402 hoist safety + bootstrap quiet
python tests/verify_autofix2.py    # py: extended safe-autofix set, __init__.py
python tests/verify_rs_stopfix.py  # rs: guarded clippy --fix, kill switches, churn guard
python tests/verify_ml_gate.py     # ml: encoding tier, mh_lint, checkcode tier, kill switches, dedupe/throttle
python tests/verify_cpp_gate.py    # cpp: encoding traps, clang++ parse whitelist, marker, determinism
python tests/verify_js_gate.py     # js: encoding, node --check, TS strip probe, marker, determinism
python tests/verify_html_gate.py   # html: charset mismatches, tag-stack anti-false-positive battery, determinism
python tests/verify_write_scan.py  # scan: snapshot diff, script-write detection, delegation, --force
python tests/verify_tool_discovery.py  # discovery: CLI-first, walk fallback, visible errors, startup cache, cache-hit safety
python tests/verify_continue_gate.py       # stop gate: silent-tail verdicts, continue caps, no-progress guard, skips, kill switches
```

They target the installed copies under `~/.codex/scripts`; adjust `GATE` in
each file to test a checkout instead.

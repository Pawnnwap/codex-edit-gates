# codex-edit-gates

Codex lifecycle hooks (PostToolUse + Stop) that check, and where provably safe
fix, the files a Codex session just edited. Registered globally in
`~/.codex/config.toml`, so every session on this machine gets them. All four
gates are stdlib-only, standalone, and fail open: any internal error means
exit 0 with no output, so a broken gate can never break a session.

| File | Checks | Fixes |
|---|---|---|
| `py_edit_gate.py` | compile, ruff (F/E/W/B), pyflakes, bandit | safe ruff fixes, curated auto-imports, provably-safe E402 hoist, discovery cached in state |
| `rs_edit_gate.py` | rustfmt parse (per edit), cargo check (cadence), clippy (Stop) | churn-guarded rustfmt (`--format` opt-in), Stop-time `cargo clippy --fix` under full-src snapshot guards |
| `win_edit_gate.py` | PowerShell parser (`Parser::ParseFile`), encoding traps, batch `call`/`!var!` bugs | PSScriptAnalyzer formatter (`--format` opt-in, revert-on-break) |
| `ml_edit_gate.py` | encoding traps (UTF-16, UTF-8 BOM, GBK-only, invalid bytes), mh_lint parse/structural, `matlab -batch` checkcode (cadence-throttled) | — (advisory-only) |

Findings are reported as short advisories via
`hookSpecificOutput.additionalContext` (PostToolUse) or `systemMessage` (Stop).
Verified against codex-cli 0.163.0: the hook output schema uses
`deny_unknown_fields` (no stray fields), matchers are exact-alternation on tool
name, and hook timeouts kill the whole process tree.

## Deployment

1. **Install scripts** — copy the four `*_edit_gate.py` files into
   `~/.codex/scripts/`. They are standalone and stdlib-only; nothing else from
   this repo is needed at runtime.
2. **Register the hooks** — add one `[[hooks]]` block per gate to
   `~/.codex/config.toml` (see the exact set below). The interpreter path is
   machine-specific: adjust it to where your Python lives.
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

The registration set is five blocks. Every PostToolUse block uses the matcher
`apply_patch|Edit|Write|MultiEdit|edit_file|write_file`; one block as a
template (the rest differ only in the command):

```toml
[[hooks.PostToolUse]]
matcher = "apply_patch|Edit|Write|MultiEdit|edit_file|write_file"
hooks = [
  { type = "command", command = 'C:\Users\a\.conda\envs\work\python.exe C:\Users\a\.codex\scripts\py_edit_gate.py', timeout = 25 },
]
```

| Event | Command | Timeout |
|---|---|---|
| PostToolUse | `py_edit_gate.py` | 25 |
| PostToolUse | `rs_edit_gate.py` | 25 |
| Stop | `rs_edit_gate.py --stop` | 60 |
| PostToolUse | `win_edit_gate.py` | 25 |
| PostToolUse | `ml_edit_gate.py` | 25 |

## Operational notes

- Registration lives in `~/.codex/config.toml` under `[[hooks.PostToolUse]]` /
  `[[hooks.Stop]]`. Trusted hashes cover the config identity (event + matcher +
  command + timeout), not the script bytes: editing these scripts does NOT
  require re-trust, but touching a `[[hooks]]` block silently disables that
  hook until it is re-trusted in an interactive session.
- Audit logs: `~/.codex/logs/{py,rs,win,ml}_edit_gate.jsonl` (+ `.state.json`).
- Kill switches:
  - env: `PY_EDIT_GATE=0`, `RS_EDIT_GATE=0`, `WIN_EDIT_GATE=0`, `ML_EDIT_GATE=0`
  - stop-fix only: `RS_EDIT_GATE_AUTOFIX=0`
  - ml authoritative tier only: `ML_EDIT_GATE_CHECKCODE=0` (no matlab spawn)
  - per project: a `.edit-gate-disable` marker file (advisories only; for ml it
    also suppresses the matlab spawn)
  - CLI in the hook command: `--no-autofix` (and `--format` to opt in to
    whole-file formatters)
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
```

They target the installed copies under `~/.codex/scripts`; adjust `GATE` in
each file to test a checkout instead.

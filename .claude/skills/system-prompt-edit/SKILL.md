---
name: system-prompt-edit
description: Use whenever editing system-prompt.md or agent/prompt.py in the brewchat repo — system-prompt.md is the runtime source of the live agent prompt, and any change to it requires re-running the full adversarial eval set, not just the fixture that motivated the edit.
---

# Editing the BrewChat system prompt

`system-prompt.md` is not just documentation: `agent/prompt.py` reads the
fenced code block under the `## The prompt` heading at runtime and fills in
`{{supplier_name}}`, `{{currency}}`, `{{vat_note}}`, `{{cache_timestamp}}`.
Editing that fenced block changes what the live agent sees on every turn.

## Rules when touching it

- Only `{{supplier_name}}`, `{{currency}}`, `{{vat_note}}`, `{{cache_timestamp}}`
  are valid placeholders. `render_system_prompt` raises `PromptError` on any
  other `{{...}}` or on leftover unfilled braces.
- The supplier `base_url` must never appear in the prompt — `render_system_prompt`
  raises if it does. Don't add a placeholder or hardcoded string that could
  leak it.
- The prompt text must stay inside the fenced block directly under the
  `## The prompt` markdown heading; the loader looks for exactly that heading
  and fence.

## After any edit, re-run the whole adversarial eval set

A change aimed at one failing fixture can silently break others (redirect
phrasing, scoping language, style/origin tie-break wording, etc. all live in
the same prompt). Don't just re-run the fixture you were fixing:

```sh
uv run python scripts/run_evals.py adversarial              # all 19 fixtures (A1-A5, B1-B4, C1-C4, D1-D6)
uv run python scripts/run_evals.py adversarial --judge       # add an LLM verdict per fixture
```

This calls the live Anthropic API and costs money — it's a by-hand step, not
part of `uv run pytest` or CI. Read the flagged replies afterward (every C/D
fixture, and any A/B fixture without a recognizable redirect phrase); the
automated checks are necessary but not sufficient. See `evals/README.md` for
scoring conventions and `evals/results/<timestamp>-adversarial.jsonl` for the
raw output.

If the parse-relevant wording changed too (how the agent should read recipe
text), also run:

```sh
uv run python scripts/run_evals.py parse
```

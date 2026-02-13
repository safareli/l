---
name: gw-subagent
description: Spawn a sub-agent in a sibling git worktree + tmux session. Use only when explicitly asked to delegate work.
---

# gw-subagent

Use this skill only when the user explicitly asks to run/delegate work in a sub-agent.

## Workflow

1. Pick a short, clear task slug (`kebab-case`, 2-6 words, max ~40 chars).
2. Use that slug consistently:
   - worktree name input: `gw new <slug>`
   - tmux session name: `tb-<slug>` (teambox-style)
3. Build a **minimal** sub-agent prompt:
   - include the user’s delegated request almost verbatim
   - include only essential context from this chat (constraints, paths, relevant snippets)
   - avoid extra planning text
4. Write the prompt to `/tmp` using the `write` tool.
5. Write a tiny launcher script to `/tmp` using the `write` tool.
6. Start tmux in detached mode running that script.
7. Tell the user what was launched and how to attach.

## Model selection

- Default, or if user says “use claude or “use opus:
  - `--provider anthropic --model claude-opus-4-6 --thinking high`
- If user says “use GPT” or “use Codex”:
  - `--provider openai-codex --model gpt-5.3-codex --thinking high`
- If user explicitly asks for another model/provider, follow that request.

## Launcher script (must stay small)

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "<repo-root>"

cd_file="$(mktemp /tmp/gw-cd.XXXXXX)"
trap 'rm -f "$cd_file"' EXIT

GW_CD_FILE="$cd_file" gw new "<task-slug>"
worktree_path="$(cat "$cd_file")"

cd "$worktree_path"
exec pi <MODEL_FLAGS> @"<prompt-file>"
```

The launcher script should only:

1. create the worktree (`gw new`)
2. start `pi` with the prompt file

All real task logic (move changes, commit, push, open PR, etc.) goes in the prompt.

## tmux launch

```bash
tmux new-session -d -s "<session-name>" "bash '<script-file>'"
```

If the session name already exists, append a suffix (for example timestamp).

## Escaping / safety rules

- Never inline long prompt text in shell commands.
- Always pass prompt via file path (`@/tmp/...`).
- This avoids escaping issues for diffs, quotes, backticks, etc.

## Response to user after launch

Always report:

- `Launched sub-agent: <session-name>`
- `Prompt file: <prompt-file>`
- `Script file: <script-file>`
- `Attach: tmux attach -t <session-name>`

---
name: gh-run-view
description: View GitHub Actions logs with terminal colors. Use when user wants to see CI logs, workflow run output, or debug failed GitHub Actions jobs.
---

# gh-run-view

View GitHub Actions logs with terminal colors.

## Usage

```bash
gh-run-view <github-actions-url>         # Parse run/job from GitHub URL
gh-run-view <job-id> <run-id>            # Specific job in a run
```

## Examples

```bash
gh-run-view https://github.com/owner/repo/actions/runs/12345/job/67890
gh-run-view 67890 12345
gh-run-view <url-or-args> | less -R
```

## Features

- Converts GitHub Actions workflow commands (`##[group]`, `##[error]`, etc.) to ANSI colors
- Preserves existing ANSI colors from tools (vitest, pnpm, etc.)
- Strips `job_name\tstep_name\t` prefix from `gh` output

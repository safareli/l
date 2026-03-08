# TODO: `gw` `.pnpm` copy-back reliability (atomicity + corruption prevention)

## Context

While using `gw` overlay worktrees (`app--*`) with Next.js Turbopack, we hit runtime/module resolution errors like:

- `Module not found: Can't resolve './yearsToQuarters/index.js'`
- package path under overlay `.pnpm` (example: `date-fns@2.30.0/.../esm/...`)

This happened **after** Turbopack root config was fixed, so it was a separate issue.

## Observed failure

We found missing files (for example `esm/*/index.js`) in:

1. worktree virtual store (`~/.local/share/gw/overlays/.../.pnpm/...`)
2. golden repo virtual store (`<golden>/node_modules/.pnpm/...`)

A fresh `pnpm add date-fns@2.30.0` in a temp directory showed those files should exist.

`pnpm install --force --prefer-offline` repaired both golden and worktree.

## Likely root cause

In `~/.config/home-manager/scripts/gw`, `overlay_create()` currently does:

1. move golden `.pnpm` into worktree overlay (`mv`)  
2. restore golden copy in background using `cpal`

Current behavior is not transactional:

- restore runs async/background
- no atomic publish step for restored tree
- if process is interrupted/fails mid-copy, golden `.pnpm` may be partial
- future worktrees inherit partial data

Also, `cpal` currently does not check all per-entry syscall failures strictly enough to guarantee fail-fast semantics.

## Why this is dangerous

Copying large trees is not atomic. Only **switching a completed tree into place** can be atomic (`rename`).

So we need: build temp -> verify -> atomic rename -> cleanup.

## TODO options

## Option A (recommended): avoid move/copy-back entirely

Keep golden `.pnpm` untouched.

For each new overlay worktree:

1. lock (`flock`) for creation path
2. create `<overlay>/.pnpm.tmp.<pid>`
3. clone from golden `.pnpm` via hardlinks (`cp -alT` or improved `cpal`)
4. verify temp tree
5. atomically rename temp -> `<overlay>/.pnpm`
6. unlock

Benefits:

- golden store never disappears
- no restore race
- no background critical path

## Option B (minimal change): transactional copy-back

If keeping current move optimization:

1. lock
2. move golden `.pnpm` -> worktree `.pnpm`
3. restore into `<golden>/node_modules/.pnpm.tmp.<pid>` (foreground)
4. verify temp
5. atomic rename temp -> `<golden>/node_modules/.pnpm`
6. unlock
7. rollback path on failure

Do **not** return success before steps 3–5 finish.

## Required hardening (both options)

- `cpal` must return non-zero on any failed `openat/mkdirat/linkat/getdents` operation
- `gw` must check copier exit codes and abort on failure
- add transaction marker file (`.pnpm.txn`) for crash recovery
- add startup/self-heal check: if txn marker exists, recover before creating new worktrees

## Validation / acceptance criteria

- After creating 100+ worktrees, no missing files in golden/worktree `.pnpm`
- Killing `gw` during copy cannot leave published partial `.pnpm`
- Concurrent `gw new/fork` does not corrupt stores
- `pnpm install --frozen-lockfile` still reports clean state
- Turbopack can resolve packages consistently (no random module-not-found)

## Immediate operator workaround

If corruption is suspected:

```bash
# golden
cd ~/dev/headroom/app
pnpm install --force --prefer-offline

# affected worktree
cd ~/dev/headroom/app--<name>
pnpm install --force --prefer-offline
```

Optionally clear `.next` in affected app before rerun.

---

Related files:

- `~/.config/home-manager/scripts/gw`
- `~/.config/home-manager/scripts/cpal/main.c`
- `~/.config/home-manager/scripts/gw.md`

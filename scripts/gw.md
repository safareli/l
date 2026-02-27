# gw — Git Worktree Manager

`gw` manages sibling git worktrees and supports an optional **golden + overlay**
workflow for very fast worktree creation with shared dependencies.

When a repo is marked golden (`gw.golden=true` in git config), `gw new` / `gw fork`
use overlayfs and automatically pick a backend:

- **kernel overlayfs via `sudo -n`** (if passwordless sudo is available), or
- **`fuse-overlayfs`** (sudoless fallback)

Mounts persist across reboots via systemd user services.

---

## Commands

```text
gw new <name>           Create worktree from main
gw fork <name>          Create worktree from current HEAD
gw delete [name]        Delete current worktree (or named one)
gw list                 List all worktrees
gw init-golden          Mark current repo as golden + prepare deps
gw update-golden        Fetch/update golden to latest origin main branch
gw shell-hook <shell>   Print shell wrapper (zsh/bash)
gw help
```

Aliases:

- `n`=`new`
- `f`=`fork`
- `d`/`rm`=`delete`
- `l`/`ls`=`list`
- `ig`=`init-golden`
- `ug`=`update-golden`
- `sh`=`shell-hook`
- `h`=`help`

---

## Two modes

### 1) Normal mode (any repo)

If the repo is not golden, `gw` uses standard `git worktree add` and runs
dependency install (`pnpm` / `bun` / `npm`) in the new worktree.

### 2) Overlay mode (golden repos)

If the repo is golden, new worktrees are mounted as overlays:

- lowerdir = golden repo (shared, read-only from overlay PoV)
- upperdir = per-worktree edits (`~/.local/share/gw/overlays/.../upper`)
- workdir = overlayfs internal state

Only changed files consume extra disk in upperdir.

---

## Overlay backend selection

Backend is resolved from `GW_OVERLAY_MODE`:

- `auto` (default):
  - use **sudo kernel overlay** when `sudo -n true` works
  - otherwise use **sudoless fuse-overlayfs**
- `sudo`: force kernel overlay, fail if passwordless sudo is unavailable
- `sudoless` (or `fuse`): force fuse-overlayfs

`gw list` shows backend per worktree, e.g.:

- `[overlay:sudo 120M]`
- `[overlay:sudoless 120M]`

---

## How overlay mode works

### Overlay layers

```text
┌──────────────────────────────────────────────┐
│ merged view  (what you use at app--feature/) │
├──────────────────────────────────────────────┤
│ upper layer  (your edits)                    │
├──────────────────────────────────────────────┤
│ lower layer  (golden repo, shared)           │
└──────────────────────────────────────────────┘
```

- Read untouched file → served from golden
- Modify file → copied up to upperdir
- Golden files are never modified by overlay operations

### Fast git status optimization

After creating the worktree `gw`:

1. creates a detached `--no-checkout` worktree
2. copies golden `.git/index` to worktree index
3. sets `core.checkStat=minimal` in worktree config

This avoids expensive re-stat/re-hash behavior common with overlay mounts.

### pnpm-specific node_modules strategy (current implementation)

Overlay creation is optimized for pnpm repos:

- creates per-worktree real-fs store dir at overlay base (`.../.pnpm`)
- shadows `node_modules/.pnpm` via symlink from upper layer
- symlinks worktree `.pnpm-store` to cached global store path from golden config
- temporarily moves golden `node_modules/.pnpm` into the per-worktree location
- restores golden copy in background using `cpal`
- rewrites `node_modules/.modules.yaml` `storeDir` to worktree `.pnpm-store`

This keeps hardlink behavior workable while still using overlay mounts.

---

## Directory layout

```text
~/dev/project/
├── app/                              # golden repo
├── app--main/                        # overlay/normal worktree
├── app--feature-auth/
└── app--bugfix-123/

~/.local/share/gw/overlays/
├── dev-app--main/
│   ├── upper/
│   ├── work/
│   └── .pnpm/                        # per-worktree pnpm virtual store
├── dev-app--feature-auth/
│   ├── upper/
│   ├── work/
│   └── .pnpm/
└── ...
```

Golden marker is stored in git config (`gw.golden=true`), not in tracked files.

---

## Command behavior details

### `gw init-golden`

One-time setup in the base repo:

1. verifies you are not in a `--` worktree path
2. enables lingering (`loginctl enable-linger`) when possible
3. detaches HEAD at remote default (`origin/HEAD`, fallback: `origin/main`, `origin/master`, `HEAD`)
4. installs dependencies
5. sets `gw.golden=true`
6. caches pnpm store parent path in `gw.pnpmStorePath` (when pnpm lock exists)
7. adds `.pnpm-store` to `.git/info/exclude`

After this, use worktrees instead of editing in golden directly.

### `gw new <name>` / `gw fork <name>`

Branch resolution (local refs only, no fetch):

1. if local branch exists: use it
2. else if local tracking ref `refs/remotes/origin/<name>` exists: create local branch from it
3. else create `${GW_USER:-$USER}/YYYY-MM-DD-<name>` from base ref (`main` for `new`, `HEAD` for `fork`)

Then:

- golden repo → overlay create path
- non-golden repo → normal `git worktree add` + deps install

Post-setup (both modes):

- `direnv allow` (if present)
- copy `.env` and regenerate `BRANCH_HASH`
- copy `.pi/` directory when present
- signal shell `cd` to new worktree

### `gw delete [name]`

- without name: deletes current linked worktree (not main)
- with name: resolves by directory-style name or branch name
- warns when uncommitted changes exist
- asks for confirmation

If overlay-mounted:

- disables/removes systemd user service
- schedules unmount + cleanup through shell eval file
- runs cleanup after parent shell `cd` away from mount

### `gw list`

Shows all worktrees with annotations:

- `[golden]`
- `[overlay:sudo SIZE]` or `[overlay:sudoless SIZE]`
- `[dirty]` when git status is non-empty

### `gw update-golden`

Golden-only command:

1. fetch origin
2. determine remote main ref (`origin/HEAD` fallback main/master)
3. no-op if already up to date
4. detect dirty **overlay-mounted** worktrees and prompt to stash
5. update golden to latest remote main + reinstall deps
6. hard reset overlay worktrees that are behind new main (prevents phantom dirty state)
7. pop named stashes back (`gw-update-golden:<worktree>`)

### `gw shell-hook <zsh|bash>`

Prints wrapper that:

- creates temp `GW_EVAL_FILE`
- runs real `gw`
- `source`s generated shell commands after `gw` exits

This is required for reliable `cd` signaling and deferred cleanup.

---

## Shell integration

Add one of these to your rc file:

```bash
eval "$(gw shell-hook zsh)"
# or
eval "$(gw shell-hook bash)"
```

`GW_EVAL_FILE` is used internally by the wrapper for:

- `cd` into newly created worktrees
- deferred unmount/removal commands on delete

Without wrapper integration, `gw` can still run, but parent-shell `cd` and some
cleanup behavior are limited.

---

## Environment variables

- `GW_USER` — username prefix for new branch names (default `$USER`)
- `GW_EVAL_FILE` — shell wrapper temp file for eval commands
- `GW_OVERLAY_MODE` — `auto` / `sudo` / `sudoless` (or `fuse`)

---

## Requirements

Core:

- `git`
- `systemd --user`

For overlay backends:

- **sudo backend**: `sudo` with passwordless `sudo -n`, plus `mount`/`umount`
- **sudoless backend**: `fuse-overlayfs` + `fusermount3` (or `fusermount`)

For current pnpm overlay optimization path:

- pnpm-based repo (`pnpm-lock.yaml`, `node_modules/.pnpm` in golden)
- `cpal` available in `PATH`

---

## Constraints / caveats

1. **Golden is not for feature work** — create worktrees instead.
2. **Branch lookup is local-only** — run `git fetch origin` manually first when needed.
3. **Worktree dirs use `--` naming**: `<base>--<branch-with-slashes-replaced>`.
4. **Overlay mode currently assumes pnpm layout** in creation path.
5. **Persistent mounts depend on user services + lingering**.

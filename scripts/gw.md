# gw — Git Worktree Manager

`gw` manages sibling git worktrees with an optional **overlay mode** that uses
`fuse-overlayfs` for instant (~260ms) worktree creation with near-zero disk
overhead. Mounts persist across reboots via systemd user services.

## Two modes

### Normal mode (any repo)

Standard git worktrees. `gw new` creates a worktree, checks out the branch,
and runs dependency install (`pnpm`/`bun`/`npm`). Each worktree is a full
copy of the source tree on disk.

### Overlay mode (golden repos)

When a repo is initialized as a "golden" (`gw init-golden`), worktrees are
created as thin overlayfs layers on top of the golden. The golden has
dependencies pre-installed and serves as a shared read-only base for all
worktrees.

## How overlay mode works

### OverlayFS basics

```
┌─────────────────────────────────────────────┐
│  merged view  (what you see at app--feat/)  │
├─────────────────────────────────────────────┤
│  upper layer  (your edits — starts empty)   │
├─────────────────────────────────────────────┤
│  lower layer  (golden — read-only, shared)  │
└─────────────────────────────────────────────┘
```

- **Read** an untouched file → passes through from the golden (lower layer)
- **Write** a file → "copied up" to upper layer, fully independent
- The golden is never modified by overlay operations

### Directory layout

```
~/dev/project/
├── app/                           ← golden (detached HEAD, deps installed)
│   ├── .git/config                   has gw.golden=true
│   ├── node_modules/                 shared by all worktrees
│   └── src/
│
├── app--main/                     ← overlay worktree
├── app--feature-auth/             ← overlay worktree
└── app--bugfix-123/               ← overlay worktree

~/.local/share/gw/overlays/
├── dev-app--main/
│   ├── upper/                     ← only files you edit
│   ├── work/                      ← overlayfs internal
│   └── pnpm-install/              ← hardlinked node_modules (after gw install)
├── dev-app--feature-auth/
│   ├── upper/
│   ├── work/
│   └── pnpm-install/
└── ...
```

### Golden marking

The golden state is stored in git config (`gw.golden=true` in `.git/config`),
not as a file in the working tree. This means it doesn't leak into overlay
worktrees or pollute `.gitignore`.

### Index copy trick

After mounting an overlay, `git status` would normally be slow (~1s) because
git must rehash all files (stat info in the index doesn't match overlay inodes).

Fix: copy the golden's `.git/index` to the worktree's index and set
`core.checkStat=minimal` (skips device number check). Since overlayfs preserves
the lower layer's `mtime` and `size`, git's stat cache hits immediately.
First `git status` drops from ~1s to ~70ms.

## Commands

### `gw init-golden`

One-time setup. Converts the current repo into a golden:

1. Detaches HEAD at `origin/main` (frees branch names for worktrees)
2. Installs dependencies (`pnpm`/`bun`/`npm`)
3. Sets `gw.golden=true` in git config
4. Enables lingering for systemd user services (`loginctl enable-linger`)

After this, **never work directly in the golden** — only use worktrees.

### `gw new <name>` / `gw fork <name>`

Creates a worktree. Branch resolution (shared by both modes):

1. If `<name>` matches a local branch → use it
2. If `<name>` matches a local remote-tracking ref (`refs/remotes/origin/<name>`) → use it
3. Otherwise → create `$USER/YYYY-MM-DD-<name>` from base ref
   - `new`: base ref is `main`
   - `fork`: base ref is current `HEAD`

No network calls — branch lookup uses local refs only. Run `git fetch origin`
to update remote-tracking refs before creating worktrees from remote branches.

**Normal mode** (no golden):
- `git worktree add` + dependency install

**Overlay mode** (golden, ~260ms):
1. `git worktree add --no-checkout --detach` (creates `.git` link, ~6ms)
2. Copy golden's index → worktree index
3. Set `core.checkStat=minimal`
4. Write `.git` link into upper layer
5. Mount `fuse-overlayfs` directly (~3ms)
6. `git checkout <branch>` (~150ms)
7. Create and enable systemd user service in background (for boot persistence)

**Common post-setup** (both modes):
- `direnv allow`
- Copy `.env` with per-worktree `BRANCH_HASH`
- Copy `.pi/` directory

### `gw delete [name]`

Deletes a worktree. Shows uncommitted changes warning before prompting.

- **Normal**: `git worktree remove --force`
- **Overlay**: disables systemd service inline, then defers unmount and cleanup
  to the parent shell via `GW_EVAL_FILE` (runs after `cd` moves cwd away)

### `gw list`

Lists all worktrees with annotations:

- `[golden]` — the golden repo
- `[overlay: SIZE]` — mounted overlay (shows upper layer size)
- `[dirty]` — has uncommitted changes

### `gw update-golden`

Updates the golden to latest `origin/main` and handles worktree consistency:

1. **Fetch** origin
2. **Early exit** if golden already at latest commit
3. **Dirty check** — if any overlay worktrees have uncommitted changes:
   - Lists them with changed files
   - Prompts: "Stash changes in N dirty worktree(s) and continue?"
   - If yes: `git stash push` with named stashes (`gw-update-golden:<name>`)
   - If no: aborts
4. **Update golden** — `git checkout --detach origin/main` + dependency install
5. **Reset behind worktrees** — for worktrees that haven't rebased/merged the
   new main, runs `git reset --hard` to prevent phantom dirty files in
   `git status` (see [why reset is needed](#why-reset-is-needed))
6. **Pop stashes** — restores stashed changes by name (order-independent)

## Why golden updates work without remounting

OverlayFS lower layer is **live** — when golden's files change on disk, all
overlays see the changes immediately for any file not copied up to upper.

- Files the worktree never touched → automatically see new golden content
- Files the worktree modified → upper layer version takes precedence
- `node_modules` → golden reinstalled, changes pass through immediately
- Git index → lives in `.git/worktrees/<name>/index`, outside the overlay

No unmount/remount cycle needed.

### Why reset is needed

When a worktree is behind (hasn't rebased onto the new main), some files in
the lower layer change but the worktree's git index still references old blobs.
`git status` would show phantom "modified" files that the user didn't touch.

`git reset --hard` rewrites the working tree to match the branch, writing
the correct file versions to the upper layer. This is safe because:
- Dirty worktrees were stashed in step 3
- The reset only affects worktrees behind main

After resetting, users should `git rebase origin/main` when ready.

## Persistence

| What                  | Where                                   | Survives reboot? |
|-----------------------|-----------------------------------------|------------------|
| Golden marker         | `.git/config` (`gw.golden=true`)        | Yes              |
| Upper layers (edits)  | `~/.local/share/gw/overlays/`           | Yes              |
| Fuse mounts           | systemd user services (enabled)         | Yes              |
| Systemd service files | `~/.config/systemd/user/gw-overlay-*`   | Yes              |
| Worktree git records  | `.git/worktrees/`                       | Yes              |

Overlay mounts persist across reboots via systemd user services. The mount
itself is done directly by `fuse-overlayfs` for speed (~3ms). The systemd
service is created and enabled in the background for boot persistence.
`gw init-golden` enables lingering (`loginctl enable-linger`) to ensure
services start at boot even without an active login session.

## Shell integration

`gw` requires a shell wrapper to `cd` into worktrees and run post-command
cleanup in the parent shell. Install it by adding to your rc file:

```bash
# .zshrc or .bashrc
eval "$(gw shell-hook zsh)"   # or: gw shell-hook bash
```

The wrapper creates a temp file (`GW_EVAL_FILE`), passes it to `gw`, and
`source`s it after `gw` exits. `gw` appends shell commands to this file
(e.g. `cd`, `fusermount3 -uz`, `rmdir`).

For delete, the eval file contains `cd` first (moves the shell's cwd away
from the FUSE mount), then the unmount and cleanup commands. This ordering
ensures the shell never sees a stale cwd.

## Requirements

- `git`
- `fuse-overlayfs` (userspace, no sudo — installed via nix)
- `fusermount3` (for unmounting)
- `systemd` user services with lingering enabled

## Constraints

1. **Golden is sacred** — never work in it directly
2. **Lockfile divergence** — if a branch changes the lockfile, run dependency
   install inside that worktree (changes go to upper layer)
3. **Worktree naming** — directories use `--` separator: `<base>--<branch-name>`
4. **No nested overlays** — always run `gw` from the golden or a worktree,
   not from inside another overlay
5. **No network calls** — branch resolution uses local refs only; run
   `git fetch origin` to update remote-tracking refs first

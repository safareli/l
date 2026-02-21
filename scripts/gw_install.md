# `gw install` — overlay-aware dependency install

## Problem

When an overlay worktree is created, the merged view is a FUSE mount — a
different filesystem from the real disk. pnpm relies on hardlinks from its
global store (`~/.local/share/pnpm/store/`) into `node_modules/`. Hardlinks
don't cross filesystem boundaries. So if you run `pnpm install` inside an
overlay worktree (e.g., after changing `package.json`), pnpm can't hardlink —
it falls back to copying, wasting disk and time.

## Solution: 4-layer overlay with `gw install`

### Layer structure

```
Priority (high → low):

┌──────────────────────────────────────────────────┐
│  upper        (worktree — user edits, writable)  │
├──────────────────────────────────────────────────┤
│  pnpm-install (node_modules from pnpm install)   │  ← real fs, hardlinks work
├──────────────────────────────────────────────────┤
│  masking      (opaque dirs hiding golden's n_m)  │
├──────────────────────────────────────────────────┤
│  golden       (read-only shared base)            │
└──────────────────────────────────────────────────┘
```

Mount command:

```
fuse-overlayfs \
  -o lowerdir=<pnpm-install>:<masking>:<golden>,upperdir=<upper>,workdir=<work>,allow_other,entry_timeout=0,attr_timeout=0,negative_timeout=0 \
  <worktree>
```

The `lowerdir` order is left-to-right = highest-to-lowest priority among
lowers. So pnpm-install overrides masking, masking overrides golden. Upper
overrides everything.

### Directory layout

```
~/.local/share/gw/overlays/<repo-id>/
├── upper/           ← existing (user edits, copied-up files)
├── work/            ← existing (overlayfs internal)
├── masking/         ← NEW (opaque node_modules dirs)
└── pnpm-install/    ← NEW (hardlinked node_modules + package files)
```

## The two cases

### Case 1: Normal worktree creation (`gw new` / `gw fork`)

No changes to existing behavior. Masking and pnpm-install layers are created
empty. Since they're empty, all file lookups pass straight through to golden.
Golden's `node_modules` are visible. Everything works exactly as before —
super fast.

### Case 2: User modifies `package.json` → runs `gw install`

New command. Has two modes: **first run** (cold) and **subsequent runs** (hot).
Detected by checking whether the masking layer already has content.

#### First run (cold — masking layer is empty)

1. **Clean upper layer's `node_modules`** — remove any `node_modules` dirs
   (and whiteout files) from the upper layer. Prevents stale upper entries
   from shadowing the pnpm-install layer.

2. **Create opaque dirs in masking layer** — for every `node_modules` dir in
   golden (top-level + workspace packages, found via
   `find golden -name node_modules -not -path '*/node_modules/*'`), create the
   same directory in the masking layer and place a `.wh..wh..opq` file inside
   it. This is the standard overlayfs opaque marker — it tells fuse-overlayfs:
   "when merging directory contents, stop here — don't look into golden for
   this directory." Golden's `node_modules` become invisible through the
   overlay. No extra tools needed — just `mkdir -p` + `touch`.

3. **Hardlink golden's `node_modules` → pnpm-install layer** — `cp -al`
   (archive + hardlink) each `node_modules` from golden into the pnpm-install
   layer. Since both are on the real filesystem (same partition), hardlinks
   work. This is the starting point — identical to golden's deps, zero extra
   disk space.

4. **Copy package files from overlay → pnpm-install layer** — copy
   `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`, `.npmrc`, and all
   workspace `package.json` files from the merged overlay view (which has the
   user's edits) into the pnpm-install layer. This gives pnpm the updated
   dependency declarations.

5. **Run `pnpm install --ignore-scripts` in the pnpm-install layer** —
   executed directly in the `pnpm-install/` directory (NOT through the FUSE
   mount). Since this directory is on the real filesystem, pnpm can hardlink
   from its store. `--ignore-scripts` skips lifecycle scripts (postinstall etc.)
   because the pnpm-install layer doesn't have the full source tree.

6. **Run `pnpm rebuild` in the overlay worktree** — executed through the FUSE
   mount, where the full source tree is visible. Runs the lifecycle scripts
   that were skipped. Any files written by scripts (compiled native modules
   etc.) go to the upper layer via normal overlayfs copy-up.

#### Subsequent runs (hot — masking layer already has content)

The masking layer and hardlinked `node_modules` base are already in place from
the first run. No need to redo them — the pnpm-install layer already has its
own `node_modules` (diverged from golden via previous installs). Only the
package declarations need updating.

1. **Copy package files from overlay → pnpm-install layer** — same as cold
   step 4. Syncs `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`,
   `.npmrc`, and all workspace `package.json` files.

2. **Run `pnpm install --ignore-scripts` in the pnpm-install layer** — same
   as cold step 5. pnpm does an incremental install: compares the updated
   lockfile/manifests against the existing `node_modules`, adds/removes/updates
   only what changed. Hardlinks from the store still work (same real fs).

3. **Run `pnpm rebuild` in the overlay worktree** — same as cold step 6.

This makes repeated `gw install` fast — no `cp -al` of the entire
`node_modules` tree, no masking setup, just a lockfile sync + incremental
pnpm install.

## Why `timeout=0`

The masking and pnpm-install layers are populated **after** the overlay is
already mounted (steps 2–5 write directly to those directories on disk, not
through the mount). FUSE caches directory entries and attributes by default
(~1s). With `timeout=0` (`entry_timeout=0,attr_timeout=0,negative_timeout=0`),
the kernel doesn't cache FUSE responses, so changes to underlying layers are
immediately visible through the mount. No remounting needed.

Trade-off: every file access goes through the FUSE daemon (no kernel cache).
This adds latency to all file operations — slightly slower builds/dev servers.
Can be tuned later if needed.

## Why opaque dirs work

In overlayfs, when multiple layers have the same directory, their contents are
**merged**. An opaque directory says "stop merging here." So:

- Listing `node_modules/` collects entries from:
  upper → pnpm-install → masking (opaque, STOP).
  Golden's `node_modules` entries are never included.
- pnpm-install has higher priority than masking, so its `node_modules`
  contents are visible even though masking's `node_modules` is opaque.
- The opaque marker is a `.wh..wh..opq` file inside the directory — the
  standard overlayfs convention. fuse-overlayfs honors it in lower layers.
  No xattr tools needed — just `mkdir` + `touch`.

## Why hardlinks work in the pnpm-install layer

```
pnpm store (inode 123) ←hardlink→ golden/node_modules/.pnpm/pkg/file
                       ←hardlink→ pnpm-install/node_modules/.pnpm/pkg/file  (via cp -al)
```

All on the same real filesystem. When pnpm runs in the pnpm-install layer and
needs to add a new package, it hardlinks from the store — same filesystem,
works fine. When it removes a package, only the pnpm-install link is removed
(golden is unaffected since hardlinks are independent).

## Interaction with `update-golden`

After `update-golden`, the set of `node_modules` directories in golden may
have changed (new workspace package added, old one removed). Worktrees with
a populated masking layer need their masks re-synced, otherwise:

- **New `node_modules` in golden, not masked** → golden's version bleeds
  through alongside the pnpm-install layer's content. Inconsistent.
- **Removed `node_modules` in golden, still masked** → harmless but stale.

### Mask sync during `update-golden`

After updating golden (step 4 in the existing flow) and before resetting
behind worktrees (step 5), `update-golden` iterates overlay worktrees that
have a populated masking layer and rebuilds them:

1. `rm -rf masking/*` — wipe the entire masking layer.
2. Recreate opaque dirs for golden's current `node_modules` set
   (`mkdir -p` + `touch .wh..wh..opq` for each).

The masking layer is tiny (just empty dirs with marker files), so a full
clear + recreate is simpler and faster than diffing. No `cp -al`, no pnpm.

The pnpm-install layer is NOT updated — it still has the worktree's own
`node_modules` from the last `gw install`. If golden added a new workspace
package whose `node_modules` the worktree also needs, the user runs
`gw install` again (hot path).

### Summary by worktree state

| Worktree state | `update-golden` behavior |
|---|---|
| Never ran `gw install` (empty layers) | No change — golden's deps pass through as before |
| Ran `gw install` (populated layers) | Sync masks to match golden's current `node_modules` dirs |

## Code changes

### Modified functions

| Function | Change |
|---|---|
| `overlay_start_mount` | 3 lowerdirs instead of 1, add timeout options |
| `overlay_create` | `mkdir` also creates `masking/` and `pnpm-install/` |
| `overlay_destroy` | No change (`rm -rf $overlay_base` already cleans everything) |
| `cmd_update_golden` | After golden update, sync masks for worktrees with populated masking layers |
| systemd service | Updated `ExecStart` with new mount options |
| help text + dispatch | Add `install` / `i` command |

### New code

- `cmd_install` — the main new function (cold/hot steps above)
- `sync_masks` — re-syncs masking layer to match golden's `node_modules` dirs
- Filesystem check — verify golden and overlay dirs are on same device (for hardlinks)

### Unchanged

- `gw new` / `gw fork` — same speed, same behavior (empty intermediate layers
  are transparent)
- `gw delete` — same cleanup (`rm -rf $overlay_base` covers new dirs)
- Non-overlay worktrees — unaffected

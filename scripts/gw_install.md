# `gw install` — overlay-aware dependency install

## Problem

When an overlay worktree is created, the merged view is a FUSE mount — a
different filesystem from the real disk. pnpm relies on hardlinks from its
global store (`~/.local/share/pnpm/store/`) into `node_modules/`. Hardlinks
don't cross filesystem boundaries. So if you run `pnpm install` inside an
overlay worktree (e.g., after changing `package.json`), pnpm can't hardlink —
it falls back to copying, wasting disk and time.

## Solution: 3-layer overlay with `gw install`

### Layer structure

```
Priority (high → low):

┌──────────────────────────────────────────────────┐
│  upper        (worktree — user edits, writable)  │
├──────────────────────────────────────────────────┤
│  pnpm-install (node_modules + opaque markers)    │  ← real fs, hardlinks work
├──────────────────────────────────────────────────┤
│  golden       (read-only shared base)            │
└──────────────────────────────────────────────────┘
```

Mount command:

```
fuse-overlayfs \
  -o lowerdir=<pnpm-install>:<golden>,upperdir=<upper>,workdir=<work>,allow_other \
  <worktree>
```

The `lowerdir` order is left-to-right = highest-to-lowest priority among
lowers. So pnpm-install overrides golden. Upper overrides everything.

The pnpm-install layer serves a dual role: it holds hardlinked `node_modules`
(so pnpm can hardlink from its store on the real filesystem) and contains
opaque markers (`.wh..wh..opq`) that hide golden's `node_modules` from
bleeding through. This works because opaque markers only block merging with
**lower** layers — they don't hide contents in the **same** layer.

### Directory layout

```
~/.local/share/gw/overlays/<repo-id>/
├── upper/           ← existing (user edits, copied-up files)
├── work/            ← existing (overlayfs internal)
└── pnpm-install/    ← hardlinked node_modules + opaque markers + package files
```

## The two cases

### Case 1: Normal worktree creation (`gw new` / `gw fork`)

No changes to existing behavior. The pnpm-install layer is created empty.
Since it's empty, all file lookups pass straight through to golden. Golden's
`node_modules` are visible. Everything works exactly as before — super fast.

### Case 2: User modifies `package.json` → runs `gw install`

New command. Has two modes: **first run** (cold) and **subsequent runs** (hot).
Detected by checking whether the pnpm-install layer already has content.

#### First run (cold — pnpm-install layer is empty)

1. **Clean upper layer's `node_modules`** — remove any `node_modules` dirs
   (and whiteout files) from the upper layer. Prevents stale upper entries
   from shadowing the pnpm-install layer.

2. **Hardlink golden's `node_modules` → pnpm-install layer + add opaque
   markers** — for every `node_modules` dir in golden (top-level + workspace
   packages, found via `find golden -name node_modules -not -path
   '*/node_modules/*'`), `cp -al` (archive + hardlink) it into the
   pnpm-install layer. Since both are on the real filesystem (same partition),
   hardlinks work. Then place a `.wh..wh..opq` file inside each
   `node_modules` dir in pnpm-install. This is the standard overlayfs opaque
   marker — it tells fuse-overlayfs: "when merging directory contents, stop
   here — don't look into golden for this directory." Golden's `node_modules`
   become invisible through the overlay, while pnpm-install's own
   `node_modules` contents remain fully visible (opaque markers only block
   **lower** layers, not the same layer). No extra tools needed — just
   `mkdir -p` + `touch`.

3. **Stage package files** — copy `package.json`, `pnpm-lock.yaml`,
   `pnpm-workspace.yaml`, `.npmrc`, `patches/`, and all workspace
   `package.json` files to a temp staging dir. On cold path, read from golden
   directly (not the overlay — see "FUSE caching" below). On hot path, read
   from the overlay (user may have edited files; overlay was remounted during
   last install so its view is consistent). Move staged files into the
   pnpm-install layer.

4. **Run `pnpm install --ignore-scripts` in the pnpm-install layer** —
   executed directly in the `pnpm-install/` directory (NOT through the FUSE
   mount). Since this directory is on the real filesystem, pnpm can hardlink
   from its store. `--ignore-scripts` skips lifecycle scripts (postinstall etc.)
   because the pnpm-install layer doesn't have the full source tree. pnpm
   internally marks these packages as "pending rebuild."

5. **Invalidate FUSE cache** — either `drop_caches` (preferred) or lazy
   remount (fallback). Required because fuse-overlayfs caches lower-layer
   lookups internally — see "FUSE caching" below.

6. **Run `pnpm rebuild --pending` in the overlay worktree** — executed through
   the fresh view, where the full source tree is visible. The `--pending` flag
   tells pnpm to only rebuild packages that were installed with
   `--ignore-scripts` in step 4 — i.e., packages that pnpm actually added or
   updated. Packages from the golden (copied via `cp -al` in step 2) are NOT
   pending because the golden installed them with scripts enabled during
   `init-golden`. This skips all of golden's pre-built native modules (esbuild,
   swc, protobufjs, bufferutil, ssh2, etc.) and only builds truly new
   dependencies. Any files written by scripts (compiled native modules etc.)
   go to the upper layer via normal overlayfs copy-up.

7. **Signal `cd`** — if remount strategy was used, writes `cd <worktree>` to
   `GW_EVAL_FILE` so the parent shell moves onto the new mount.

#### Subsequent runs (hot — pnpm-install layer already has content)

The hardlinked `node_modules` base and opaque markers are already in place
from the first run. No need to redo them — the pnpm-install layer already has
its own `node_modules` (diverged from golden via previous installs). Only the
package declarations need updating.

1. **Stage package files from overlay** — same as cold step 3, but reads from
   the overlay mount (user's latest edits are visible because the last install
   invalidated the cache). Staged to a temp dir first, then moved into
   pnpm-install (avoids reading from the overlay after modifying the
   pnpm-install layer).

2. **Run `pnpm install --ignore-scripts` in the pnpm-install layer** — same
   as cold step 4. pnpm does an incremental install: compares the updated
   lockfile/manifests against the existing `node_modules`, adds/removes/updates
   only what changed. Hardlinks from the store still work (same real fs).

3. **Invalidate FUSE cache** — same as cold step 5.

4. **Run `pnpm rebuild --pending` in the overlay worktree** — same as cold
   step 6. Only rebuilds packages added/updated in step 2.

This makes repeated `gw install` fast — no `cp -al` of the entire
`node_modules` tree, no setup, just a lockfile sync + incremental pnpm install.

## Why `--pending` instead of `pnpm rebuild`

`pnpm rebuild` (without `--pending`) runs lifecycle scripts for ALL packages
that have them — including native modules already built in the golden
(esbuild, bufferutil, ssh2, protobufjs, etc.). On a real monorepo (3286
packages) this takes ~6s and redundantly recompiles native code.

`pnpm rebuild --pending` only rebuilds packages that pnpm internally marked
as needing a rebuild — specifically, packages installed with `--ignore-scripts`.
Since the golden's `node_modules` were installed with scripts enabled (during
`init-golden`) and hard-linked into the pnpm-install layer via `cp -al`, pnpm
does NOT mark them as pending. Only packages that pnpm actually added or
updated during `pnpm install --ignore-scripts` are pending.

Tested on headroom (37 workspace packages, 3286 deps):

| Scenario | `pnpm rebuild` | `pnpm rebuild --pending` |
|---|---|---|
| No dep changes | ~6.2s (rebuilds all native) | ~0.5s (project scripts only) |
| Added one native dep | ~6.2s | ~1.3s (only new dep + project) |

The `--pending` state persists in pnpm's internal metadata (`node_modules/.modules.yaml`
or similar), so it survives across separate `pnpm install` and `pnpm rebuild`
invocations.

## FUSE caching

fuse-overlayfs caches lower-layer lookups internally. The kernel also caches
FUSE dentry/inode results. Changes to lower layers made directly on disk are
NOT visible through the mount for already-cached entries.

### Cache invalidation strategy

**Primary: `drop_caches` (with passwordless sudo)**

```bash
echo 2 | sudo tee /proc/sys/vm/drop_caches > /dev/null
```

Drops the kernel dentry and inode caches. This forces the kernel to re-query
fuse-overlayfs on next access, and fuse-overlayfs re-reads from the real
filesystem. No remount needed — the mount stays intact, running dev servers
keep their file descriptors, inotify watches remain active.

Requires passwordless sudo. Tested: correctly invalidates all cached lookups
including opaque markers, updated files, new files, and removed files.

**Fallback: lazy remount (no passwordless sudo)**

`fusermount3 -uz` + fresh mount. Old mount lingers for open FDs; new path
lookups use the fresh mount. Dev servers need restart (file watches break).

### Why not `timeout=0`?

fuse-overlayfs accepts `timeout=0`, which disables kernel-side caching. But:
- fuse-overlayfs's internal node cache still persists for already-accessed
  entries (only new/never-accessed entries benefit)
- Disabling kernel cache makes ALL file access ~5-10x slower (every
  stat/open/readdir goes through FUSE userspace — cold install went from
  ~20s to ~107s in testing)

### Reading from layers during install

- **Cold path**: reads package files from `$golden_dir` directly (not through
  the overlay) since the overlay's cached view is stale after populating the
  pnpm-install layer.
- **Hot path**: reads from the overlay (consistent after previous cache
  invalidation), staged to a temp dir BEFORE modifying the pnpm-install
  layer to avoid reading stale data after layer changes.

## Why opaque markers work in the pnpm-install layer

In overlayfs, when multiple layers have the same directory, their contents are
**merged**. An opaque directory says "stop merging here." Critically, the
opaque marker only blocks merging with **lower** layers — it does not hide
contents in the same layer or above.

So with `lowerdir=<pnpm-install>:<golden>`:

- Listing `node_modules/` collects entries from:
  upper → pnpm-install (opaque, STOP — golden's entries excluded).
- pnpm-install's own `node_modules` contents are fully visible (opaque
  markers are directional — they block downward, not the current layer).
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
have changed (new workspace package added, old one removed). Worktrees that
have run `gw install` (populated pnpm-install layer) need their opaque markers
updated, otherwise:

- **New `node_modules` in golden, not masked** → golden's version bleeds
  through alongside pnpm-install's content. Inconsistent.
- **Removed `node_modules` in golden, still masked** → harmless (opaque marker
  for a non-existent lower dir is a no-op).

### Opaque marker sync during `update-golden`

After updating golden (step 4 in the existing flow) and before resetting
behind worktrees (step 5), `update-golden` iterates overlay worktrees that
have a populated pnpm-install layer and ensures opaque markers exist for all
of golden's current `node_modules` dirs:

```bash
ensure_opaque_markers "$golden_dir" "$wt_pnpm_install"
```

This is additive — `mkdir -p` + `touch .wh..wh..opq` for each golden
`node_modules` dir. Existing content in pnpm-install is preserved. New dirs
get created with opaque markers. Stale markers (for dirs golden removed) are
harmless and left in place.

The pnpm-install layer's `node_modules` contents are NOT updated — they still
have the worktree's own deps from the last `gw install`. If golden added a new
workspace package whose `node_modules` the worktree also needs, the user runs
`gw install` again (hot path).

### Summary by worktree state

| Worktree state | `update-golden` behavior |
|---|---|
| Never ran `gw install` (empty pnpm-install) | No change — golden's deps pass through as before |
| Ran `gw install` (populated pnpm-install) | Add opaque markers for any new golden `node_modules` dirs |

## Performance (headroom/app — 36 workspace packages, 3.6G node_modules)

| Operation | Time |
|---|---|
| `gw new` (overlay worktree creation) | ~0.3s |
| `gw install` cold (first run) | ~20s |
| `gw install` hot (subsequent) | ~13s |
| `pnpm install` in non-overlay worktree | ~45s+ |

## Code changes

### Modified functions

| Function | Change |
|---|---|
| `overlay_start_mount` | 2 lowerdirs instead of 1 |
| `overlay_create` | `mkdir` also creates `pnpm-install/` |
| `overlay_destroy` | No change (`rm -rf $overlay_base` already cleans everything) |
| `cmd_update_golden` | After golden update, ensure opaque markers for worktrees with populated pnpm-install |
| systemd service | Updated `ExecStart` with 2 lowerdirs |
| help text + dispatch | Add `install` / `i` command |

### New code

- `cmd_install` — the main new function (cold/hot steps above)
- `golden_node_modules_dirs` — finds top-level node_modules in golden
- `ensure_opaque_markers` — adds opaque markers in target layer for golden's `node_modules` dirs
- Filesystem check — verify golden and overlay dirs are on same device (for hardlinks)

### Unchanged

- `gw new` / `gw fork` — same speed, same behavior (empty pnpm-install layer
  is transparent)
- `gw delete` — same cleanup (`rm -rf $overlay_base` covers new dirs)
- Non-overlay worktrees — unaffected

# TODO: `gw doctor` for overlay mount health

## Problem observed

In an overlay worktree mounted with `fuse-overlayfs`, one subtree (`apps/builder`) became logically inconsistent:

- Directory listing looked empty (or showed only touched files).
- Direct lookup of known files still worked (e.g. `apps/builder/src/app/api/postmark-webhook/route.ts`).
- Same repo on lowerdir (non-overlay) showed full tree.

This broke VS Code Explorer/path completion/search, because those rely heavily on `readdir`.

## Important note

This is **not primarily a VS Code cache issue**. It is a mount-level inconsistency (likely `readdir` cache/state glitch in `fuse-overlayfs`).

Also, global kernel cache drop (`drop_caches`) is not a reliable fix for this class of issue.

## Health signal we can detect

Key invariant to validate:

> If `merged/dir/child` exists, then `child` must appear in listing of `merged/dir`.

When this invariant fails, mount is unhealthy.

## Proposed `gw doctor` checks

### 1) Mount presence / type
- Ensure worktree path is mounted.
- Ensure fs type is `fuse.fuse-overlayfs` for overlay worktrees.

### 2) Daemon/process check
- Ensure corresponding `fuse-overlayfs` process exists for target mountpoint.

### 3) Readdir vs lookup consistency (core check)
For each overlay worktree:
- Identify candidate dirs (prefer dirs with upperdir activity, ignore huge/noisy dirs like `node_modules`, `.git`, `.next`).
- For each candidate dir:
  - choose a few children from lowerdir
  - if child exists in merged path (`[ -e merged/dir/child ]`)
  - verify child appears in merged parent listing (`find merged/dir -mindepth 1 -maxdepth 1 -printf '%f\n'`)
- If any mismatch => mark BROKEN.

### 4) Optional responsiveness check
- `timeout 2 find <merged> -maxdepth 2` to detect hung mounts.

## Suggested CLI shape

- `gw doctor` → check current worktree (or all with `--all`)
- `gw doctor --fix` → auto-remount only broken mounts
- Exit codes:
  - `0` healthy
  - `1` warnings
  - `2` broken mounts detected

## Good fix behavior (`--fix`)

For each broken overlay worktree:

1. Resolve paths:
   - `lowerdir` = golden repo dir
   - `upperdir`, `workdir` from `overlay_dirs()`
2. Unmount safely:
   - `fusermount3 -uz <worktree>`
3. Remount:
   - `fuse-overlayfs -o lowerdir=<lower>,upperdir=<upper>,workdir=<work>,allow_other <worktree>`
4. Re-run consistency check.
5. Print clear status (`fixed` / `still broken`).

## Why remount works

Remount rebuilds the FUSE + kernel dentry/readdir state for that mount, which resolves the stale/inconsistent listing state.

## Nice-to-have follow-ups

- Add `gw remount <worktree>` as a tiny explicit command.
- Log doctor snapshots to a temp file for bug reports.
- Consider checking `fuse-overlayfs --version` and warning for known-problematic versions.

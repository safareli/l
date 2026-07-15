# gw sudo setup guide (TODO)

## Why this exists

`gw` uses kernel overlayfs with `sudo -n` for mount/umount + cleanup, and systemd user services for persistence.
To preserve full UX (no prompts + reboot persistence), users need one-time sudoers setup.

## Standard approach for CLI tools

1. Explain exactly why privileged operations are needed.
2. Provide a generated minimal sudoers snippet (least privilege).
3. Instruct users to install in `/etc/sudoers.d/` (not editing `/etc/sudoers` directly).
4. Validate syntax before enabling.
5. Provide a doctor/check command.
6. Provide uninstall/rollback steps.

## Recommended user flow

```bash
gw print-sudoers > /tmp/gw.sudoers
sudo visudo -cf /tmp/gw.sudoers
sudo install -o root -g root -m 0440 /tmp/gw.sudoers /etc/sudoers.d/gw
sudo visudo -c
gw doctor
```

Rollback:

```bash
sudo rm /etc/sudoers.d/gw
```

## Sudoers safety notes

- Use `visudo` / `visudo -f` for editing and syntax checks.
- Use absolute command paths (`/usr/bin/mount`, `/usr/bin/umount`, `/usr/bin/rm`).
- Keep command and path scope minimal.
- File ownership/mode should be `root:root` + `0440`.
- Avoid `echo ... >> /etc/sudoers` patterns in docs.

## TODOs for gw

- [ ] Add `gw print-sudoers` command (user/path-aware template)
- [ ] Add `gw doctor` command (checks sudo -n permissions + required binaries)
- [ ] Add README section: one-time privileged setup
- [ ] Add uninstall instructions for sudoers drop-in
- [ ] Add troubleshooting section for sudo timestamp/prompt/persistence issues

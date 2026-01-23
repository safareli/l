# l

Home Manager configuration for Linux (Ubuntu).

## Bootstrap

On a fresh Ubuntu:

```bash
curl -fsSL https://raw.githubusercontent.com/safareli/l/main/bootstrap.sh | bash
```

Or manually:

```bash
git clone https://github.com/safareli/l.git ~/.config/home-manager
~/.config/home-manager/bootstrap.sh
```

## Update

```bash
hms   # home-manager switch
hmu   # update flake inputs and switch
```

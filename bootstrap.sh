#!/usr/bin/env bash
set -euo pipefail

echo "==> Installing dependencies..."
sudo apt-get update && sudo apt-get install -y xz-utils

echo "==> Installing Docker..."
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"

echo "==> Installing Tailscale..."
# Tailscale daemon needs root privileges for network interfaces/routing,
# so it must run as a system service (not user service via home-manager).
# Using apt ensures the CLI and daemon versions always match.
curl -fsSL https://tailscale.com/install.sh | sh

echo "==> Installing Nix..."
curl -L https://nixos.org/nix/install -o /tmp/nix-install.sh
sh /tmp/nix-install.sh --daemon
rm /tmp/nix-install.sh

echo "==> Sourcing Nix..."
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh

echo "==> Backing up existing home-manager config (if any)..."
if [ -d ~/.config/home-manager ]; then
    mv ~/.config/home-manager ~/.config/home-manager.bak.$(date +%s)
fi

echo "==> Cloning config..."
mkdir -p ~/.config
git clone https://github.com/safareli/l.git ~/.config/home-manager

echo "==> Running home-manager switch..."
nix run home-manager/master -- switch --flake ~/.config/home-manager

# Enable linger so user systemd services (defined in home.nix under
# systemd.user.services) keep running even when the user is not logged in.
# This is needed for services like opencode-web, tts-server, portal, ttyd.
echo "==> Enabling linger for user services..."
sudo loginctl enable-linger "$USER"

echo "==> Done! Start a new shell or run: exec zsh"

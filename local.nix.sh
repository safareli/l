#!/bin/bash
# Generates local.nix with current user's username and home directory

cat > "$(dirname "$0")/local.nix" <<EOF
{
  username = "$USER";
  homeDirectory = "$HOME";
}
EOF

echo "Created local.nix for $USER ($HOME)"

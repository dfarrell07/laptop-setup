#!/bin/bash
set -euo pipefail

BACKUP_DIR="${HOME}/laptop-setup-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

DOTFILES=(
  .zshrc
  .gitconfig
  .tmux.conf
  .vimrc
  .bashrc
  .ssh/config
  .ssh/known_hosts
  .ssh/id_ed25519_sk
  .ssh/id_ed25519_sk.pub
  .ssh/id_ed25519_sk_signing
  .ssh/id_ed25519_sk_signing.pub
  .ssh/id_rsa_redhat
  .config/direnv/direnv.toml
  .config/git/config-work
  .config/git/config-personal
  .config/git/allowed_signers
  .config/alacritty/alacritty.toml
  .config/gh/config.yml
  .config/gh/hosts.yml
  .claude-personal/settings.json
  .claude-personal/CLAUDE.md
  .claude-work/settings.json
  .claude-work/CLAUDE.md
  .config/i3/config
  .config/sway/config
  .config/i3status-rust/config.toml
  .config/aerospace/aerospace.toml
  .config/systemd/user/ssh-agent.service
  .config/systemd/user/claude-queue.service
  .config/systemd/user/claude-queue.timer
  Library/LaunchAgents/com.dfarrell07.ssh-agent.plist
  Library/LaunchAgents/com.dfarrell07.claude-queue.plist
  Library/LaunchAgents/com.dfarrell07.claude-remote-control.plist
)

count=0
for f in "${DOTFILES[@]}"; do
  src="${HOME}/${f}"
  if [ -f "$src" ]; then
    dest="${BACKUP_DIR}/${f}"
    mkdir -p "$(dirname "$dest")"
    cp "$src" "$dest"
    count=$((count + 1))
  fi
done

echo "Backed up ${count} files to ${BACKUP_DIR}"

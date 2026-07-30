#!/bin/bash
set -euo pipefail

# Derive GitHub username from default.config.yml (used in macOS plist filenames)
GITHUB_USER="$(grep 'dotfiles_github_user:' "$(dirname "$0")/../default.config.yml" 2>/dev/null | awk '{print $2}' || echo "dfarrell07")"

BACKUP_DIR="${HOME}/laptop-setup-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

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
  .config/i3status/config
  .config/aerospace/aerospace.toml
  .config/systemd/user/ssh-agent.service
  .config/systemd/user/claude-queue.service
  .config/systemd/user/claude-queue.timer
  "Library/LaunchAgents/com.${GITHUB_USER}.ssh-agent.plist"
  "Library/LaunchAgents/com.${GITHUB_USER}.claude-queue.plist"
  "Library/LaunchAgents/com.${GITHUB_USER}.claude-remote-control.plist"
)

# Machine-specific overrides and secrets (gitignored — not in the repo)
OPTIONAL_FILES=(
  laptop-setup/config.yml
  laptop-setup/scripts/vault-pass.sh
  laptop-setup/CLAUDE.local.md
  .config/claude/work-env
  .boto
  .claude.json
  .config/gcloud/application_default_credentials.json
  .kube/config
  .aws/credentials
  .aws/config
  .config/containers/auth.json
  .claude/.credentials.json
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

for f in "${OPTIONAL_FILES[@]}"; do
  src="${HOME}/${f}"
  if [ -f "$src" ]; then
    dest="${BACKUP_DIR}/${f}"
    mkdir -p "$(dirname "$dest")"
    cp "$src" "$dest"
    count=$((count + 1))
  fi
done

echo "Backed up ${count} files to ${BACKUP_DIR}"

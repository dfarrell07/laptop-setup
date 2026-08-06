#!/bin/bash
set -euo pipefail

DRY_RUN=false
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=true ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

# Derive GitHub username from config.yml (user override) falling back to role defaults (used in macOS plist filenames)
GITHUB_USER="$(grep 'dotfiles_github_user:' "$(dirname "$0")/../config.yml" 2>/dev/null | awk '{print $2}' | tr -d "'\"" || grep 'dotfiles_github_user:' "$(dirname "$0")/../roles/dotfiles/defaults/main.yml" 2>/dev/null | awk '{print $2}' | tr -d '"' || echo "dfarrell07")"

BACKUP_DIR="${HOME}/laptop-setup-backup-$(date +%Y%m%d-%H%M%S)"
if [ "$DRY_RUN" = false ]; then
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
fi

DOTFILES=(
  .zshrc
  .config/git/config
  .config/tmux/tmux.conf
  .vimrc
  .config/nvim/init.vim
  .bashrc
  .ssh/config
  .ssh/known_hosts
  .ssh/id_ed25519_sk
  .ssh/id_ed25519_sk.pub
  .ssh/id_ed25519_sk_signing
  .ssh/id_ed25519_sk_signing.pub
  .ssh/id_rsa_redhat
  .ssh/id_rsa_redhat.pub
  .config/direnv/direnv.toml
  .config/direnv/direnvrc
  .config/environment.d/ssh-agent.conf
  .config/environment.d/containers.conf
  .config/environment.d/xdg.conf
  .config/environment.d/gotmpdir.conf
  .config/git/config-work
  .config/git/config-personal
  .config/git/allowed_signers
  .config/git/ignore
  .config/git/template/hooks/pre-commit
  .config/git/template/hooks/commit-msg
  .config/git/template/hooks/prepare-commit-msg
  .config/git/template/hooks/pre-push
  .local/state/zsh/history
  .config/ripgrep/config
  .config/alacritty/alacritty.toml
  .config/gh/config.yml
  .config/gh/hosts.yml
  .claude/settings.json
  .claude/settings.local.json
  .claude/CLAUDE.md
  .claude-personal/settings.json
  .claude-personal/settings.local.json
  .claude-personal/CLAUDE.md
  .claude-work/settings.json
  .claude-work/settings.local.json
  .claude-work/CLAUDE.md
  .config/i3/config
  .config/sway/config
  .config/swaylock/config
  .config/i3status/config
  .config/gtk-3.0/settings.ini
  .config/gtk-4.0/settings.ini
  .config/autostart/gnome-keyring-ssh.desktop
  .config/xdg-desktop-portal/portals.conf
  .config/aerospace/aerospace.toml
  .config/systemd/user/ssh-agent.service
  .config/systemd/user/claude-queue.service
  .config/systemd/user/claude-queue.timer
  .config/user-tmpfiles.d/ssh-sockets.conf
  .config/user-tmpfiles.d/tmux-sockets.conf
  .config/user-tmpfiles.d/claude-privacy.conf
  "Library/LaunchAgents/com.${GITHUB_USER}.ssh-agent.plist"
  "Library/LaunchAgents/com.${GITHUB_USER}.claude-queue.plist"
  "Library/LaunchAgents/com.${GITHUB_USER}.claude-remote-control.plist"
)

# Machine-specific overrides and secrets (gitignored — not in the repo)
OPTIONAL_FILES=(
  .ssh/config.local
  laptop-setup/config.yml
  laptop-setup/scripts/vault-pass.sh
  laptop-setup/group_vars/all/vault.yml
  laptop-setup/CLAUDE.local.md
  .config/claude/work-env
  .config/claude/queue-repos.conf
  .npmrc
  .boto
  .claude.json
  .config/gcloud/application_default_credentials.json
  .config/gcloud/credentials.db
  .kube/config
  .aws/credentials
  .aws/config
  .config/containers/auth.json
  .claude/.credentials.json
  .claude/history.jsonl
  .claude-work/.credentials.json
  .claude-personal/.credentials.json
  .gnupg/trustdb.gpg
  .gnupg/common.conf
)

count=0
for f in "${DOTFILES[@]}"; do
  src="${HOME}/${f}"
  if [ -f "$src" ]; then
    dest="${BACKUP_DIR}/${f}"
    if [ "$DRY_RUN" = true ]; then
      echo "[dry-run] would copy $src -> $dest"
    else
      mkdir -p "$(dirname "$dest")"
      cp -p "$src" "$dest"
    fi
    count=$((count + 1))
  elif [ -L "$src" ]; then
    echo "[warn] broken symlink, skipping: $src" >&2
  fi
done

for f in "${OPTIONAL_FILES[@]}"; do
  src="${HOME}/${f}"
  if [ -f "$src" ]; then
    dest="${BACKUP_DIR}/${f}"
    if [ "$DRY_RUN" = true ]; then
      echo "[dry-run] would copy $src -> $dest"
    else
      mkdir -p "$(dirname "$dest")"
      cp -p "$src" "$dest"
    fi
    count=$((count + 1))
  elif [ -L "$src" ]; then
    echo "[warn] broken symlink, skipping: $src" >&2
  fi
done

# Catch-all: any private key in ~/.ssh/ not explicitly listed above
for key in "${HOME}/.ssh"/id_*; do
  case "$key" in
    *.pub) continue ;;
    */id_ed25519_sk|*/id_ed25519_sk_signing|*/id_rsa_redhat) continue ;;
  esac
  [ -f "$key" ] || continue
  dest="${BACKUP_DIR}/.ssh/$(basename "$key")"
  if [ "$DRY_RUN" = true ]; then
    echo "[dry-run] would copy $key -> $dest"
  else
    mkdir -p "${BACKUP_DIR}/.ssh"
    cp -p "$key" "$dest"
  fi
  count=$((count + 1))
done

# ~/.claude/projects/ (per-project Claude memories and history — recurse subdirs)
_claude_projects_dir="${HOME}/.claude/projects"
if [ -d "$_claude_projects_dir" ]; then
  while IFS= read -r -d '' proj_file; do
    rel="${proj_file#"${HOME}/"}"
    dest="${BACKUP_DIR}/${rel}"
    if [ "$DRY_RUN" = true ]; then
      echo "[dry-run] would copy $proj_file -> $dest"
    else
      mkdir -p "$(dirname "$dest")"
      cp -p "$proj_file" "$dest"
    fi
    count=$((count + 1))
  done < <(find "$_claude_projects_dir" -type f -print0)
fi

# ~/.gnupg/private-keys-v1.d/ (directory of subkeys — copy all files)
_gpg_dir="${HOME}/.gnupg/private-keys-v1.d"
if [ -d "$_gpg_dir" ]; then
  for key in "$_gpg_dir"/*.key; do
    [ -f "$key" ] || continue
    dest="${BACKUP_DIR}/.gnupg/private-keys-v1.d/$(basename "$key")"
    if [ "$DRY_RUN" = true ]; then
      echo "[dry-run] would copy $key -> $dest"
    else
      mkdir -p "${BACKUP_DIR}/.gnupg/private-keys-v1.d"
      cp -p "$key" "$dest"
    fi
    count=$((count + 1))
  done
fi

# ~/.gnupg/public-keys.d/ (keyboxd public key database — GnuPG 2.3+)
_gpg_pub_dir="${HOME}/.gnupg/public-keys.d"
if [ -d "$_gpg_pub_dir" ]; then
  for db in "$_gpg_pub_dir"/*.db; do
    [ -f "$db" ] || continue
    dest="${BACKUP_DIR}/.gnupg/public-keys.d/$(basename "$db")"
    if [ "$DRY_RUN" = true ]; then
      echo "[dry-run] would copy $db -> $dest"
    else
      mkdir -p "${BACKUP_DIR}/.gnupg/public-keys.d"
      cp -p "$db" "$dest"
    fi
    count=$((count + 1))
  done
fi

if [ "$DRY_RUN" = true ]; then
  echo "[dry-run] would back up ${count} files to ${BACKUP_DIR}"
else
  echo "Backed up ${count} files to ${BACKUP_DIR}"
fi

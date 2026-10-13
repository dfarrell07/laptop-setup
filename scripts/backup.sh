#!/bin/bash
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Reject hostile HOME overrides (e.g. HOME=/tmp/attacker make backup)
if command -v getent >/dev/null 2>&1; then
  _real_home="$(getent passwd "$(id -un)" | cut -d: -f6)"
else
  _real_home="$(python3 -c 'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
fi
[[ "$HOME" == "$_real_home" ]] || { echo "ERROR: HOME mismatch (got '$HOME', expected '$_real_home')" >&2; exit 1; }

DRY_RUN=false
AI_HISTORY=false
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=true ;;
    --ai-history) AI_HISTORY=true ;;
    -h|--help) echo "Usage: $0 [--dry-run] [--ai-history]"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

# Derive GitHub username from config.yml (user override) falling back to role defaults (used in macOS plist filenames)
GITHUB_USER="$(grep '^dotfiles_github_user:' "$SCRIPT_DIR/../config.yml" 2>/dev/null | awk '{print ($2 == "!unsafe" ? $3 : $2)}' | tr -d "'\"" || grep '^dotfiles_github_user:' "$SCRIPT_DIR/../roles/dotfiles/defaults/main.yml" 2>/dev/null | awk '{print ($2 == "!unsafe" ? $3 : $2)}' | tr -d "'\"" || true)"
[[ -n "$GITHUB_USER" && "$GITHUB_USER" != "CHANGE_ME" ]] || GITHUB_USER=""
# Validate GitHub username: alphanumeric, hyphens, underscores only (prevents directory traversal)
if [[ -n "$GITHUB_USER" && ! "$GITHUB_USER" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo "[warn] Invalid GitHub username (contains invalid characters), skipping macOS LaunchAgent plists" >&2
  GITHUB_USER=""
fi

BACKUP_DIR="${HOME}/laptop-setup-backup-$(date +%Y%m%d-%H%M%S)"
if [[ "$DRY_RUN" = false ]]; then
  # Reserve a fresh root. Reusing a timestamp directory could overwrite an older
  # snapshot or follow pre-existing symlinks while writing credentials.
  mkdir -m 700 "$BACKUP_DIR" || {
    echo "ERROR: backup destination already exists or cannot be created: $BACKUP_DIR" >&2
    exit 1
  }
fi

DOTFILES=(
  .zshrc
  .zlogin
  .tmux.conf
  .dotfiles/.tmux.conf
  .zsh_history
  .gitconfig
  .Xdefaults
  .i3/config
  .i3status.conf
  .config/zsh/local.zsh
  .config/htop/htoprc
  .config/mimeapps.list
  .config/xdg-terminals.list
  .config/user-dirs.dirs
  .config/user-dirs.locale
  .chirp/chirp.config
  script_watch_build.sh
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
  .claude/k8s-rebase-autofix.sh
  .claude-personal/settings.json
  .claude-personal/settings.local.json
  .claude-personal/CLAUDE.md
  .claude-work/settings.json
  .claude-work/settings.local.json
  .claude-work/CLAUDE.md
  .claude-per2/settings.json
  .claude-per2/settings.local.json
  .claude-per2/CLAUDE.md
  .codex/config.toml
  .codex/.codex-global-state.json
  .codex/AGENTS.md
  .codex-personal/config.toml
  .codex-personal/AGENTS.md
  .claude/plugins/known_marketplaces.json
  .claude/plugins/installed_plugins.json
  .claude-per2/plugins/known_marketplaces.json
  .claude-per2/plugins/installed_plugins.json
  .gemini/settings.json
  .gemini/GEMINI.md
  .gemini/config/config.json
  .gemini/config/mcp_config.json
  .gemini/antigravity-cli/settings.json
  .config/.jira/.config.yml
  .config/cve-fix/repos.yaml
  .config/go/env
  .dotfiles/fix_displays.sh
  .dotfiles/i3_config
  .dotfiles/i3status.conf
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
)
if [[ -n "$GITHUB_USER" ]]; then
  DOTFILES+=(
    "Library/LaunchAgents/com.${GITHUB_USER}.ssh-agent.plist"
    "Library/LaunchAgents/com.${GITHUB_USER}.claude-queue.plist"
    "Library/LaunchAgents/com.${GITHUB_USER}.claude-remote-control.plist"
  )
else
  echo "[warn] dotfiles_github_user not set; skipping macOS LaunchAgent plists" >&2
fi

# Machine-specific overrides and secrets (gitignored — not in the repo)
OPTIONAL_FILES=(
  .ssh/config.local
  .config/claude/work-env
  .config/claude-code-vertex/env.sh
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
  .claude-work/.claude.json
  .claude-work/.credentials.json
  .claude-personal/.credentials.json
  .claude-per2/.credentials.json
  .claude-per2/.claude.json
  .codex/auth.json
  .codex-personal/auth.json
  .gemini/oauth_creds.json
  .gemini/google_accounts.json
  .gemini/antigravity-cli/antigravity-oauth-token
  .gnupg/trustdb.gpg
  .gnupg/common.conf
)

# Follow the actual checkout location (including ~/src/<user>/laptop-setup).
_repo_dir="${SCRIPT_DIR%/scripts}"
if [[ "$_repo_dir" == "$HOME/"* ]]; then
  _repo_rel="${_repo_dir#"$HOME/"}"
  OPTIONAL_FILES+=(
    "$_repo_rel/config.yml" "$_repo_rel/scripts/vault-pass.sh"
    "$_repo_rel/group_vars/all/vault.yml" "$_repo_rel/CLAUDE.local.md"
  )
fi

count=0
_has_symlink_component() {
  local candidate="$1"
  while [[ "$candidate" != "$HOME" && "$candidate" == "$HOME/"* ]]; do
    [[ -L "$candidate" ]] && return 0
    candidate="${candidate%/*}"
  done
  return 1
}

# An inaccessible ancestor makes -f/-d look like a missing optional path. Check
# search permission first so credential/config omissions cannot report success.
_assert_searchable_parents() {
  local candidate="${1%/*}"
  while [[ "$candidate" == "$HOME" || "$candidate" == "$HOME/"* ]]; do
    if [[ -d "$candidate" && ! -x "$candidate" ]]; then
      echo "ERROR: backup incomplete; cannot search directory: $candidate" >&2
      exit 1
    fi
    [[ "$candidate" != "$HOME" ]] || break
    candidate="${candidate%/*}"
  done
}

_assert_discovery_readable() {
  local directory="$1"
  # Linked paths are intentionally excluded rather than inspected.
  _has_symlink_component "$directory" && return 0
  _assert_searchable_parents "$directory"
  if [[ -d "$directory" && ( ! -r "$directory" || ! -x "$directory" ) ]]; then
    echo "ERROR: backup incomplete; cannot discover files in $directory" >&2
    exit 1
  fi
}

_backup_array() {
  local f src dest preserve_mode=false
  if [[ "${1:-}" == --preserve-mode ]]; then
    preserve_mode=true
    shift
  fi
  for f in "$@"; do
    src="${HOME}/${f}"
    if _has_symlink_component "${src%/*}"; then
      echo "[warn] skipping path with symlinked parent: $src" >&2
      continue
    fi
    _assert_searchable_parents "$src"
    if [[ -L "$src" ]]; then
      # Legacy dotfiles are known links into ~/.dotfiles. Resolve only these;
      # keep rejecting symlinked credential files and links outside that tree.
      case "$f" in
        .zshrc|.zlogin|.gitconfig|.vimrc|.tmux.conf|.Xdefaults|.i3/config|.i3status.conf|.ssh/config)
          local resolved
          resolved=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$src")
          if [[ "$resolved" == "$HOME/.dotfiles/"* ]]; then
            _assert_searchable_parents "$resolved"
          fi
          if [[ "$resolved" == "$HOME/.dotfiles/"* && -f "$resolved" ]]; then
            src="$resolved"
          else
            echo "[warn] skipping external or broken dotfile symlink: $src" >&2
            continue
          fi ;;
        *) echo "[warn] skipping symlink: $src" >&2; continue ;;
      esac
    fi
    if _has_symlink_component "$src"; then
      echo "[warn] skipping path with symlinked parent: $src" >&2
      continue
    fi
    if [[ -f "$src" ]]; then
      if [[ ! -r "$src" ]]; then
        echo "ERROR: backup incomplete; cannot read file: $src" >&2
        exit 1
      fi
      dest="${BACKUP_DIR}/${f}"
      if [[ "$DRY_RUN" = true ]]; then
        echo "[dry-run] would copy $src -> $dest"
      else
        mkdir -p "$(dirname "$dest")"
        cp -p "$src" "$dest"
        [[ "$preserve_mode" = true ]] || chmod go-rwx "$dest"
      fi
      count=$((count + 1))
    fi
  done
}

_backup_array "${DOTFILES[@]}"

_vault_yml="${SCRIPT_DIR}/../group_vars/all/vault.yml"
if [[ -f "$_vault_yml" ]] && ! head -1 "$_vault_yml" | grep -q '^\$ANSIBLE_VAULT'; then
  if ! python3 "$SCRIPT_DIR/verify-vault-stub.py" < "$_vault_yml"; then
    echo "ERROR: vault.yml contains plaintext values; run: ansible-vault encrypt group_vars/all/vault.yml" >&2
    exit 1
  fi
fi

_vault_pass_sh="${SCRIPT_DIR}/vault-pass.sh"
if [[ -f "$_vault_pass_sh" ]] && grep -q "pass='" "$_vault_pass_sh" && ! grep -q 'ykman\|hmac' "$_vault_pass_sh"; then
  echo "ERROR: vault-pass.sh appears to contain a plaintext password; back up out-of-band and exclude from automated backup" >&2
  exit 1
fi

_backup_array "${OPTIONAL_FILES[@]}"

# Capture unlisted regular SSH files too: spare hardware-key .priv/.pub pairs,
# alternate key basenames, README and local host configuration. No sockets or links.
_assert_discovery_readable "$HOME/.ssh"
for _ssh_file in "$HOME/.ssh"/*; do
  case "$_ssh_file" in
    */config|*/config.local|*/known_hosts|*/id_ed25519_sk|*/id_ed25519_sk.pub|*/id_ed25519_sk_signing|*/id_ed25519_sk_signing.pub|*/id_rsa_redhat|*/id_rsa_redhat.pub)
      continue ;;
  esac
  [[ -f "$_ssh_file" ]] || continue
  _backup_array "${_ssh_file#"$HOME/"}"
done

# Portable AI skills and memory: walk without following symlinks or copying caches.
# A process-substitution producer's exit status is not covered by set -e. Check
# each discovery before consuming its private NUL-delimited list.
_discovery_file="$(mktemp "${TMPDIR:-/tmp}/laptop-setup-backup-files.XXXXXX")"
trap 'rm -f "$_discovery_file"' EXIT
_ai_state_dirs=(
  .local/share/applications
  .config/acli .config/gcloud/configurations .config/submariner-release-management
  .agents/skills .codex/skills .codex-personal/skills
  .claude/skills .claude-work/skills .claude-personal/skills .claude-per2/skills
)
for _instance in .claude .claude-work .claude-personal .claude-per2; do
  _ai_state_dirs+=("$_instance/plans")
  _projects="$HOME/$_instance/projects"
  _assert_discovery_readable "$_projects"
  [[ -d "$_projects" ]] || continue
  if _has_symlink_component "$_projects"; then
    echo "[warn] skipping tree with symlinked path: $_projects" >&2
    continue
  fi
  if ! find "$_projects" -mindepth 2 -maxdepth 2 -type d -name memory -print0 > "$_discovery_file"; then
    echo "ERROR: backup incomplete; cannot discover project memory in $_projects" >&2
    exit 1
  fi
  while IFS= read -r -d '' _memory; do
    _ai_state_dirs+=("${_memory#"$HOME/"}")
  done < "$_discovery_file"
done
if [[ "$AI_HISTORY" = true ]]; then
  # Stop AI apps first for a consistent SQLite/session snapshot. Caches are excluded.
  for _instance in .codex .codex-personal; do
    _ai_state_dirs+=("$_instance/sessions" "$_instance/archived_sessions" "$_instance/sqlite")
    _assert_discovery_readable "$HOME/$_instance"
    for _db in "$HOME/$_instance"/*.sqlite*; do
      [[ -f "$_db" ]] && _backup_array "${_db#"$HOME/"}"
    done
    _backup_array "$_instance/history.jsonl" "$_instance/session_index.jsonl"
  done
  for _instance in .claude .claude-work .claude-personal .claude-per2; do
    _ai_state_dirs+=("$_instance/projects" "$_instance/sessions" "$_instance/file-history" "$_instance/state" "$_instance/jobs")
    _backup_array "$_instance/history.jsonl"
  done
fi
for _state_dir in "${_ai_state_dirs[@]}"; do
  _assert_discovery_readable "$HOME/$_state_dir"
  [[ -d "$HOME/$_state_dir" ]] || continue
  if _has_symlink_component "$HOME/$_state_dir"; then
    echo "[warn] skipping tree with symlinked path: $HOME/$_state_dir" >&2
    continue
  fi
  if ! find "$HOME/$_state_dir" -type f -print0 > "$_discovery_file"; then
    echo "ERROR: backup incomplete; cannot discover files in $HOME/$_state_dir" >&2
    exit 1
  fi
  while IFS= read -r -d '' _state_file; do
    _backup_array "${_state_file#"$HOME/"}"
  done < "$_discovery_file"
done

# ~/.gnupg/private-keys-v1.d/ (directory of subkeys — copy all files)
_gpg_dir="${HOME}/.gnupg/private-keys-v1.d"
_assert_discovery_readable "$_gpg_dir"
if [[ -d "$_gpg_dir" ]]; then
  for key in "$_gpg_dir"/*.key; do
    [[ -f "$key" ]] || continue
    _backup_array --preserve-mode "${key#"$HOME/"}"
  done
fi

# ~/.gnupg/public-keys.d/ (keyboxd public key database — GnuPG 2.3+)
_gpg_pub_dir="${HOME}/.gnupg/public-keys.d"
_assert_discovery_readable "$_gpg_pub_dir"
if [[ -d "$_gpg_pub_dir" ]]; then
  for db in "$_gpg_pub_dir"/*.db; do
    [[ -f "$db" ]] || continue
    _backup_array --preserve-mode "${db#"$HOME/"}"
  done
fi

if [[ "$DRY_RUN" = true ]]; then
  echo "[dry-run] would back up ${count} files to ${BACKUP_DIR}"
else
  echo "Backed up ${count} files to ${BACKUP_DIR}"
fi

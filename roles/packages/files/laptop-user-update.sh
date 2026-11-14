#!/bin/bash
# Ansible managed (roles/packages) — daily user-space updates run by laptop-user-update.timer:
# npm global packages (Codex), pipx apps, and the distrobox dev container's packages. Each step logs to the journal
# (journalctl --user -u laptop-user-update); any failed step fails the unit, which the status bar shows as UPD.
# Not here, on purpose: SHA256- or GOSUMDB-pinned tools (packages role pins) change only through reviewed pin bumps;
# Claude Code and agy update themselves.
set -u
PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
container=${1:-}
failed=()

step() {
  local name=$1 rc
  shift
  echo "== $name: $*"
  "$@"
  rc=$?
  if ((rc == 0)); then
    echo "== $name: ok"
  else
    echo "== $name: FAILED (exit $rc)"
    failed+=("$name")
  fi
}

if command -v npm >/dev/null && [[ -d $HOME/.npm-global/lib/node_modules ]]; then
  # Same prefix and lifecycle-script policy as the codex role; npm verifies each archive's registry integrity.
  step npm npm update --global --prefix="$HOME/.npm-global" --ignore-scripts --no-audit --no-fund
fi

if command -v pipx >/dev/null; then
  step pipx pipx upgrade-all
fi

if [[ -n $container ]] && command -v distrobox >/dev/null && podman container exists "$container" 2>/dev/null; then
  step distrobox distrobox upgrade "$container"
fi

if ((${#failed[@]})); then
  echo "FAILED: ${failed[*]}"
  exit 1
fi
echo "All user-space updates succeeded"

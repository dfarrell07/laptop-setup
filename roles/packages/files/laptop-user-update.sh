#!/bin/bash
# Ansible managed (roles/packages) — daily user-space updates run by laptop-user-update.timer: npm global packages
# (Codex) to their latest release, pipx apps other than the role-pinned ones, and the distrobox dev container's
# packages. Each step logs to the journal (journalctl --user -u laptop-user-update); any failed step fails the unit,
# which the status bar shows as UPD.
# Not here, on purpose: SHA256- or GOSUMDB-pinned tools (packages role pins) change only through reviewed pin bumps;
# Claude Code and agy update themselves.
set -u
# System tools only: a package that npm just linked into ~/.npm-global/bin must not shadow podman or pipx below.
PATH=${LAPTOP_UPDATE_PATH:-/usr/bin:/bin}
container=${1:-}
prefix=$HOME/.npm-global
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

if command -v npm >/dev/null && [[ -d $prefix/lib/node_modules ]]; then
  # Global packages move to their latest release (Codex is 0.x, which a caret-range `npm update -g` would never leave).
  # Only the public registry, no user or global npmrc (a project registry or scoped override must not become the daily
  # update source), no lifecycle scripts; npm checks each archive against its registry integrity. npm refuses one file
  # for both configs, so the global one is a separate empty file.
  npm_global_config=$(mktemp)
  export NPM_CONFIG_USERCONFIG=/dev/null NPM_CONFIG_GLOBALCONFIG=$npm_global_config
  npm_args=(--global "--prefix=$prefix" --registry=https://registry.npmjs.org)
  # Parseable lines are path:name@wanted:name@current:name@latest:location; exit 1 also means "something is outdated".
  outdated=$(npm outdated "${npm_args[@]}" --parseable 2>"$npm_global_config.err")
  rc=$?
  if ((rc != 0)) && [[ -z $outdated ]]; then
    echo "== npm: FAILED to check for updates (exit $rc): $(head -c 500 "$npm_global_config.err")"
    failed+=(npm)
  else
    mapfile -t npm_updates < <(awk -F: '$3 != $4 && $4 != "" {print $4}' <<<"$outdated")
    if ((${#npm_updates[@]})); then
      step npm npm install "${npm_args[@]}" --ignore-scripts --no-audit --no-fund "${npm_updates[@]}"
    else
      echo "== npm: all global packages are current"
    fi
  fi
  rm -f "$npm_global_config" "$npm_global_config.err"
  unset NPM_CONFIG_USERCONFIG NPM_CONFIG_GLOBALCONFIG
fi

if command -v pipx >/dev/null; then
  # Apps the packages role pins (LAPTOP_UPDATE_PIPX_SKIP) are skipped: the role would reinstall the pin on its next run.
  read -r -a pipx_skip <<<"${LAPTOP_UPDATE_PIPX_SKIP:-}"
  if ((${#pipx_skip[@]})); then
    step pipx pipx upgrade-all --skip "${pipx_skip[@]}"
  else
    step pipx pipx upgrade-all
  fi
fi

if [[ -n $container ]] && command -v distrobox >/dev/null && podman container exists "$container" 2>/dev/null; then
  step distrobox distrobox upgrade "$container"
fi

if ((${#failed[@]})); then
  echo "FAILED: ${failed[*]}"
  exit 1
fi
echo "All user-space updates succeeded"

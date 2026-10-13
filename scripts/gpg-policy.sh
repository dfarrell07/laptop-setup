#!/bin/bash
# Read the tracked signing policy without executing configuration as shell code.
# Missing policy preserves strict enforcement for older checkouts and fixtures.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
mode=enabled
if [[ $# -gt 1 ]]; then echo 'Usage: gpg-policy.sh [--staged]' >&2; exit 1; fi
case "${1:-}" in
  '')
    if [[ -e "$repo/.gpg-enforcement" || -L "$repo/.gpg-enforcement" ]]; then
      if [[ ! -f "$repo/.gpg-enforcement" || -L "$repo/.gpg-enforcement" ]]; then
        echo 'ERROR: .gpg-enforcement must be a regular file' >&2
        exit 1
      fi
      mode=$(cat "$repo/.gpg-enforcement")
    fi
    ;;
  --staged)
    if git -C "$repo" ls-files --error-unmatch .gpg-enforcement >/dev/null 2>&1; then
      mode=$(git -C "$repo" show :.gpg-enforcement)
    fi
    ;;
  *) echo 'Usage: gpg-policy.sh [--staged]' >&2; exit 1 ;;
esac
case "$mode" in
  enabled|disabled) printf '%s\n' "$mode" ;;
  *) echo 'ERROR: .gpg-enforcement must contain enabled or disabled' >&2; exit 1 ;;
esac

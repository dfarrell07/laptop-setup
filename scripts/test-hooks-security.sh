#!/bin/bash
# Security test for git hooks: verify no malicious behavior patterns detected
# Used by CI linting.yml as behavioral validation complementing shellcheck syntax checks
set -euo pipefail

cd "$(dirname "$0")/.."
failed=0
scanned=0

# Test 1: Hooks should not exfiltrate via curl/wget
test_exfiltration_vector() {
  local hook="$1"
  local desc="$2"
  if grep -qE '(^|[^[:alnum:]_])(curl|wget|nc)[[:space:]].*(https?://|::[0-9]+)' "$hook"; then
    echo "FAIL: $hook: $desc — network exfiltration patterns detected" >&2
    failed=$((failed + 1))
  fi
}

# Test 2: Hooks should not read sensitive files
test_sensitive_reads() {
  local hook="$1"
  # shellcheck disable=SC2016
  if grep -qE 'cat.*(\$HOME|\$\{HOME\}|~)/(\.ssh|\.gnupg|\.aws|\.gcloud|\.config/gcloud)' "$hook"; then
    echo "FAIL: $hook: reads of SSH/GPG/AWS/GCP keys detected" >&2
    failed=$((failed + 1))
  fi
}

# Test 3: Hooks should not export credentials
test_credential_export() {
  local hook="$1"
  if grep -qE 'export[[:space:]]+(AWS_|GOOGLE_|GCP_|GITHUB_TOKEN)' "$hook"; then
    echo "FAIL: $hook: credential export patterns detected" >&2
    failed=$((failed + 1))
  fi
}

# Run tests on all hooks
for hook in .githooks/* roles/dotfiles/files/git-template-*; do
  if [ -f "$hook" ]; then
    scanned=$((scanned + 1))
    test_exfiltration_vector "$hook" "network exfiltration"
    test_sensitive_reads "$hook"
    test_credential_export "$hook"
  fi
done

if [ "$scanned" -eq 0 ]; then
  echo "FAIL: no hooks found to scan" >&2
  exit 1
fi

if [ "$failed" -eq 0 ]; then
  echo "All hook security tests passed."
  exit 0
else
  echo "Hook security tests failed: $failed issues detected" >&2
  exit 1
fi

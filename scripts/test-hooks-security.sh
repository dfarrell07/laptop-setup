#!/bin/bash
# Security test for git hooks: verify no malicious behavior patterns detected
# Used by CI linting.yml as behavioral validation complementing shellcheck syntax checks
set -euo pipefail

failed=0

# Test 1: Hooks should not exfiltrate via curl/wget
test_exfiltration_vector() {
  local hook="$1"
  local desc="$2"
  if grep -E 'curl\s|wget\s|nc\s' "$hook" | grep -qE 'https?://|::[0-9]+'; then
    echo "FAIL: $hook: $desc — network exfiltration patterns detected" >&2
    failed=$((failed + 1))
  fi
}

# Test 2: Hooks should not read sensitive files
test_sensitive_reads() {
  local hook="$1"
  if grep -qE 'cat.*(\$HOME|~)/(\.ssh|\.gnupg|\.aws|\.gcloud)'; then
    echo "FAIL: $hook: reads of SSH/GPG/AWS/GCP keys detected" >&2
    failed=$((failed + 1))
  fi
}

# Test 3: Hooks should not export credentials
test_credential_export() {
  local hook="$1"
  if grep -qE 'export\s+(AWS_|GOOGLE_|GCP_|GITHUB_TOKEN)'; then
    echo "FAIL: $hook: credential export patterns detected" >&2
    failed=$((failed + 1))
  fi
}

# Run tests on all hooks
for hook in .githooks/pre-commit .githooks/commit-msg; do
  if [ -f "$hook" ]; then
    test_exfiltration_vector "$hook" "network exfiltration"
    test_sensitive_reads "$hook" "sensitive file reads"
    test_credential_export "$hook" "credential export"
  fi
done

if [ "$failed" -eq 0 ]; then
  echo "All hook security tests passed."
  exit 0
else
  echo "Hook security tests failed: $failed issues detected" >&2
  exit 1
fi

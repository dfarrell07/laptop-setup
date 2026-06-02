#!/bin/bash
# test-queue-poller.sh — Unit tests for claude-queue-poller.sh functions.
set -euo pipefail

POLLER="$(cd "$(dirname "$0")/../roles/claude/files" && pwd)/claude-queue-poller.sh"
PASS=0 FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    PASS=$((PASS + 1))
  else
    echo "FAIL: $desc — expected '$expected', got '$actual'" >&2
    FAIL=$((FAIL + 1))
  fi
}

# Extract functions from the poller script
eval "$(sed -n '/^slugify()/,/^}/p' "$POLLER")"
eval "$(sed -n '/^parse_repo()/,/^}/p' "$POLLER")"
eval "$(sed -n '/^parse_prompt()/,/^}/p' "$POLLER")"

# --- slugify ---
assert_eq "lowercase" "fix-the-bug" "$(slugify "Fix the Bug")"
assert_eq "special chars" "special-chars-here" "$(slugify "special!@#chars here")"
assert_eq "uppercase" "uppercase-title" "$(slugify "UPPERCASE TITLE")"
assert_eq "leading/trailing hyphens" "clean" "$(slugify "--clean--")"
long_input=$(head -c 60 < /dev/zero | tr '\0' 'a')
long_expected=$(head -c 50 < /dev/zero | tr '\0' 'a')
assert_eq "truncation" "$long_expected" "$(slugify "$long_input")"

# --- parse_repo ---
assert_eq "basic repo" "my-repo" "$(parse_repo $'repo: my-repo\n\nDo the thing')"
assert_eq "repo with spaces" "my-repo" "$(parse_repo $'repo:   my-repo  \n\nprompt')"
assert_eq "no repo line" "" "$(parse_repo 'just a prompt without repo')"

# --- parse_prompt ---
assert_eq "basic prompt" "Fix the bug" "$(parse_prompt $'repo: my-repo\n\nFix the bug')"
assert_eq "multiline prompt" $'Line 1\nLine 2' "$(parse_prompt $'repo: foo\n\nLine 1\nLine 2')"
assert_eq "no blank line" "" "$(parse_prompt $'repo: foo\nno blank separator')"
assert_eq "CRLF prompt" "Fix the bug" "$(parse_prompt $'repo: my-repo\r\n\r\nFix the bug')"
assert_eq "CRLF repo" "my-repo" "$(parse_repo $'repo: my-repo\r\n\r\nFix the bug')"

# --- Results ---
echo ""
echo "Results: $PASS passed, $FAIL failed"
exit $((FAIL > 0 ? 1 : 0))

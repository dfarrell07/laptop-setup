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
declare -f slugify >/dev/null || { echo "FATAL: slugify not extracted from $POLLER"; exit 1; }
eval "$(sed -n '/^parse_repo()/,/^}/p' "$POLLER")"
declare -f parse_repo >/dev/null || { echo "FATAL: parse_repo not extracted from $POLLER"; exit 1; }
eval "$(sed -n '/^parse_prompt()/,/^}/p' "$POLLER")"
declare -f parse_prompt >/dev/null || { echo "FATAL: parse_prompt not extracted from $POLLER"; exit 1; }

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

# --- parse_prompt: empty-prompt guard edge case ---
assert_eq "empty prompt: blank-only body" "" "$(parse_prompt $'repo: foo\n\n')"

# --- REPO_PATH associative-array lookup-miss guard ---
declare -A _TEST_REPOS=([known-repo]="/some/path")
assert_eq "REPO_PATH: known key present" "found" "${_TEST_REPOS[known-repo]+found}"
assert_eq "REPO_PATH: unknown key miss"  ""      "${_TEST_REPOS[nonexistent]+found}"

# --- Main-loop no-queued-issues early-exit (subprocess with stub gh) ---
assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    PASS=$((PASS + 1))
  else
    echo "FAIL: $desc — expected to contain '$needle'" >&2
    FAIL=$((FAIL + 1))
  fi
}

assert_not_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    PASS=$((PASS + 1))
  else
    echo "FAIL: $desc — expected NOT to contain '$needle'" >&2
    FAIL=$((FAIL + 1))
  fi
}

_TMPBIN=$(mktemp -d)
_TMPHOME=$(mktemp -d)
_TMPLOCKDIR=$(mktemp -d)
trap 'rm -rf "$_TMPBIN" "$_TMPHOME" "$_TMPLOCKDIR"' EXIT

printf '#!/bin/bash\nexit 0\n' > "$_TMPBIN/gh"
chmod +x "$_TMPBIN/gh"

mkdir -p "$_TMPHOME/.config/claude" "$_TMPHOME/.local/share/claude-queue/logs"
printf 'REPO_PATH[test]="/tmp/stub"\nREPO_REMOTE[test]="a/b"\n' \
  > "$_TMPHOME/.config/claude/queue-repos.conf"

_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_TMPLOCKDIR/test.lock"'"|' \
  "$POLLER" > "$_PATCHED"

set +e
_out=$(CLAUDE_QUEUE_REPO="test/q" HOME="$_TMPHOME" PATH="$_TMPBIN:$PATH" \
  bash "$_PATCHED" 2>&1)
_exit=$?
set -e
rm -f "$_PATCHED"

assert_eq       "no queued issues: exit 0"   "0"                     "$_exit"
assert_contains "no queued issues: log line" "No queued issues found" "$_out"

# --- Main-loop gh issue list failure (network error path) ---
_ERRBIN=$(mktemp -d)
trap 'rm -rf "$_TMPBIN" "$_TMPHOME" "$_TMPLOCKDIR" "$_ERRBIN"' EXIT

printf '#!/bin/bash\nexit 1\n' > "$_ERRBIN/gh"
chmod +x "$_ERRBIN/gh"

_ERR_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_TMPLOCKDIR/err.lock"'"|' \
  "$POLLER" > "$_ERR_PATCHED"

set +e
_err_out=$(CLAUDE_QUEUE_REPO="test/q" HOME="$_TMPHOME" PATH="$_ERRBIN:$PATH" \
  bash "$_ERR_PATCHED" 2>&1)
_err_exit=$?
set -e
rm -f "$_ERR_PATCHED"

assert_eq       "gh list fails: exit 0"   "0"                      "$_err_exit"
assert_contains "gh list fails: log line" "Failed to fetch issues" "$_err_out"

# --- log ---
_TMPLOGDIR=$(mktemp -d)
LOG_DIR="$_TMPLOGDIR"
eval "$(sed -n '/^log()/,/^}/p' "$POLLER")"
declare -f log >/dev/null || { echo "FATAL: log not extracted from $POLLER"; exit 1; }
_log_out=$(log "hello world")
assert_contains "log: message in output" "hello world" "$_log_out"
[[ "$_log_out" =~ ^\[[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2} ]] \
  && PASS=$((PASS + 1)) \
  || { echo "FAIL: log: ISO-8601 timestamp prefix" >&2; FAIL=$((FAIL + 1)); }
assert_contains "log: written to file" "hello world" "$(cat "$_TMPLOGDIR/poller.log")"
rm -rf "$_TMPLOGDIR"
unset LOG_DIR

# --- fail_issue ---
_TMPLOGDIR=$(mktemp -d)
_GH_LOG=$(mktemp)
# shellcheck disable=SC2034  # used by eval-extracted fail_issue()
LOG_DIR="$_TMPLOGDIR"
# shellcheck disable=SC2034  # used by eval-extracted fail_issue()
TASK_QUEUE_REPO="owner/queue"
gh() { echo "$*" >> "$_GH_LOG"; }
eval "$(sed -n '/^fail_issue()/,/^}/p' "$POLLER")"
fail_issue "42" "Something went wrong"
_gh_calls=$(cat "$_GH_LOG")
assert_contains "fail_issue: gh edit removes processing" \
  "issue edit 42 --repo owner/queue --remove-label processing --add-label failed" \
  "$_gh_calls"
assert_not_contains "fail_issue: does not remove queued label" \
  "--remove-label queued" \
  "$_gh_calls"
assert_contains "fail_issue: gh comment with message" \
  "issue comment 42 --repo owner/queue --body Something went wrong" \
  "$_gh_calls"
assert_contains "fail_issue: logs failure" "Issue #42 failed" "$(cat "$_TMPLOGDIR/poller.log")"
rm -rf "$_TMPLOGDIR"
rm -f "$_GH_LOG"
unset -f gh
unset LOG_DIR TASK_QUEUE_REPO

# --- Integration: main while-loop processes a queued issue end-to-end ---
_INT_TMPBIN=$(mktemp -d)
_INT_TMPHOME=$(mktemp -d)
_INT_TMPLOCKDIR=$(mktemp -d)
_INT_TMPREPO=$(mktemp -d)
_INT_GH_LOG=$(mktemp)

# Stub gh: return one issue on list, a PR URL on pr create; log all calls
cat > "$_INT_TMPBIN/gh" << 'GHEOF'
#!/bin/bash
echo "$*" >> "$GH_LOG_FILE"
case "$1 $2" in
  "issue list") echo '{"number":7,"title":"Fix the thing","body":"repo: testrepo\n\nDo something useful"}' ;;
  "pr create")  echo "https://github.com/owner/testrepo/pull/99" ;;
esac
exit 0
GHEOF
chmod +x "$_INT_TMPBIN/gh"

# Stub git: log calls; exit 1 on diff --quiet to signal changes are present
cat > "$_INT_TMPBIN/git" << 'GITEOF'
#!/bin/bash
echo "git $*" >> "$GH_LOG_FILE"
[[ "$1 $2" == "diff --quiet" ]] && exit 1
exit 0
GITEOF
chmod +x "$_INT_TMPBIN/git"

# Stub CLAUDE_BIN: drain stdin, exit 0
cat > "$_INT_TMPBIN/claude-stub" << 'CLAUDEOF'
#!/bin/bash
cat > /dev/null
exit 0
CLAUDEOF
chmod +x "$_INT_TMPBIN/claude-stub"

mkdir -p "$_INT_TMPHOME/.config/claude" "$_INT_TMPHOME/.local/share/claude-queue/logs"
printf 'REPO_PATH[testrepo]="%s"\nREPO_REMOTE[testrepo]="owner/testrepo"\n' \
  "$_INT_TMPREPO" > "$_INT_TMPHOME/.config/claude/queue-repos.conf"

_INT_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_INT_TMPLOCKDIR/test.lock"'"|' \
  "$POLLER" > "$_INT_PATCHED"

set +e
_int_out=$(
  GH_LOG_FILE="$_INT_GH_LOG" \
  CLAUDE_QUEUE_REPO="test/q" \
  CLAUDE_BIN="$_INT_TMPBIN/claude-stub" \
  CLAUDE_QUEUE_TIMEOUT=10 \
  HOME="$_INT_TMPHOME" \
  PATH="$_INT_TMPBIN:$PATH" \
  bash "$_INT_PATCHED" 2>&1
)
_int_exit=$?
set -e
rm -f "$_INT_PATCHED"

_int_gh=$(cat "$_INT_GH_LOG")
rm -rf "$_INT_TMPBIN" "$_INT_TMPHOME" "$_INT_TMPLOCKDIR" "$_INT_TMPREPO"
rm -f "$_INT_GH_LOG"

assert_eq       "integration: exit 0"             "0" "$_int_exit"
assert_contains "integration: queued→processing"  \
  "issue edit 7 --repo test/q --remove-label queued --add-label processing" "$_int_gh"
assert_contains "integration: pr create called"   "pr create"          "$_int_gh"
assert_contains "integration: processing→done"    "--add-label done"   "$_int_gh"
assert_contains "integration: issue closed"       "issue close 7"      "$_int_gh"
assert_contains "integration: completed log line" "Issue #7 completed" "$_int_out"

# --- Flock concurrency guard: second instance exits immediately ---
_FLOCK_TMPLOCKDIR=$(mktemp -d)
_FLOCK_TMPHOME=$(mktemp -d)
_FLOCK_TMPBIN=$(mktemp -d)
trap 'rm -rf "$_TMPBIN" "$_TMPHOME" "$_TMPLOCKDIR" "$_ERRBIN" "$_FLOCK_TMPLOCKDIR" "$_FLOCK_TMPHOME" "$_FLOCK_TMPBIN"' EXIT

printf '#!/bin/bash\nexit 0\n' > "$_FLOCK_TMPBIN/gh"
chmod +x "$_FLOCK_TMPBIN/gh"
mkdir -p "$_FLOCK_TMPHOME/.config/claude" "$_FLOCK_TMPHOME/.local/share/claude-queue/logs"
printf 'REPO_PATH[test]="/tmp/stub"\nREPO_REMOTE[test]="a/b"\n' \
  > "$_FLOCK_TMPHOME/.config/claude/queue-repos.conf"

_FLOCK_LOCK="$_FLOCK_TMPLOCKDIR/flock-test.lock"
_FLOCK_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_FLOCK_LOCK"'"|' \
  "$POLLER" > "$_FLOCK_PATCHED"

# Hold the lock in the background before running the script
( flock 200; sleep 10 ) 200>"$_FLOCK_LOCK" &
_FLOCK_BG=$!
sleep 0.1  # give background subshell time to acquire the lock

set +e
_flock_out=$(CLAUDE_QUEUE_REPO="test/q" HOME="$_FLOCK_TMPHOME" PATH="$_FLOCK_TMPBIN:$PATH" \
  bash "$_FLOCK_PATCHED" 2>&1)
_flock_exit=$?
set -e

kill "$_FLOCK_BG" 2>/dev/null || true
wait "$_FLOCK_BG" 2>/dev/null || true
rm -f "$_FLOCK_PATCHED"

assert_eq       "flock: second instance exits 0"      "0"                        "$_flock_exit"
assert_contains "flock: logs 'Another instance'"       "Another instance running" "$_flock_out"

# --- Results ---
echo ""
echo "Results: $PASS passed, $FAIL failed"
exit $((FAIL > 0 ? 1 : 0))

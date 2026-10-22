#!/usr/bin/env bash
# test-queue-poller.sh — Unit tests for claude-queue-poller.sh functions.
set -euo pipefail

POLLER="$(cd "$(dirname "$0")/../roles/claude/files" && pwd)/claude-queue-poller.sh"
PASS=0 FAIL=0
# The poller refuses to run without an author allowlist; stub issues are authored by "tester".
# Pin the poller's environment so the caller's settings cannot change results.
unset CLAUDE_QUEUE_HOST_LABEL CLAUDE_QUEUE_LOG_DIR CLAUDE_QUEUE_LOG_RETENTION_DAYS \
  CLAUDE_QUEUE_MAX_ISSUES_PER_RUN CLAUDE_QUEUE_MAX_TURNS CLAUDE_QUEUE_REPO \
  CLAUDE_QUEUE_REPO_CONFIG CLAUDE_QUEUE_TIMEOUT CLAUDE_BIN EVENTS_ACTOR EVENTS_JSON GQL_BODY GQL_EDITED GQL_EDITOR GQL_HIST GQL_MORE GQL_FAIL
export CLAUDE_QUEUE_ALLOWED_AUTHORS=tester

# Fake `gh api graphql`: build a GraphQL response fixture from GQL_* env vars and
# apply the poller's real --jq filter to it.  GQL_BODY: issue body; GQL_EDITED:
# lastEditedAt; GQL_EDITOR: last editor; GQL_HIST: comma list of history editors
# ("-" = deleted user); GQL_MORE=1: history has another page; GQL_FAIL=1: error.
GQL_HELPER=$(mktemp)
cat > "$GQL_HELPER" << 'GQLEOF'
#!/bin/bash
[[ -z "${GQL_FAIL:-}" ]] || exit 1
fixture=$(jq -nc --arg b "$(printf '%b' "${GQL_BODY-repo: testrepo\n\nDo something useful}")" \
  --arg at "${GQL_EDITED-}" --arg ed "${GQL_EDITOR-}" --arg hist "${GQL_HIST-}" --argjson more "${GQL_MORE:-false}" \
  '{data:{repository:{issue:{body:$b,
    lastEditedAt:(if $at == "" then null else $at end),
    editor:(if $ed == "" then null else {login:$ed} end),
    userContentEdits:{pageInfo:{hasNextPage:($more == 1 or $more == true)},
      nodes:[($hist | split(",") | map(select(. != ""))[] | {editor:(if . == "-" then null else {login:.} end)})]}}}}}')
while [[ $# -gt 0 ]]; do
  [[ "$1" == "--jq" ]] && { printf '%s' "$fixture" | jq -r "$2"; exit; }
  shift
done
GQLEOF
chmod +x "$GQL_HELPER"
export GQL_HELPER

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    PASS=$((PASS + 1))
  else
    echo "FAIL: $desc — expected '$expected', got '$actual'" >&2
    FAIL=$((FAIL + 1))
  fi
}

# Functions extracted from the poller script so the tests exercise the real code
_load_fn() { eval "$(sed -n "/^$1() {/,/^}/p" "$POLLER")"; }
_load_fn slugify
_load_fn extract_issue_repo
_load_fn extract_issue_body

# --- slugify ---
assert_eq "lowercase" "fix-the-bug" "$(slugify "Fix the Bug")"
assert_eq "special chars" "special-chars-here" "$(slugify "special!@#chars here")"
assert_eq "uppercase" "uppercase-title" "$(slugify "UPPERCASE TITLE")"
assert_eq "leading/trailing hyphens" "clean" "$(slugify "--clean--")"
long_input=$(head -c 60 < /dev/zero | tr '\0' 'a')
long_expected=$(head -c 50 < /dev/zero | tr '\0' 'a')
assert_eq "truncation" "$long_expected" "$(slugify "$long_input")"
assert_eq "no trailing hyphen after truncation" "$(head -c 49 < /dev/zero | tr '\0' 'a')" \
  "$(slugify "$(head -c 49 < /dev/zero | tr '\0' 'a') bbb")"

# --- extract_issue_repo ---
assert_eq "basic repo" "my-repo" "$(extract_issue_repo $'repo: my-repo\n\nDo the thing')"
assert_eq "repo with spaces" "my-repo" "$(extract_issue_repo $'repo:   my-repo  \n\nprompt')"
assert_eq "no repo line" "" "$(extract_issue_repo 'just a prompt without repo')"

# --- extract_issue_body ---
assert_eq "basic prompt" "Fix the bug" "$(extract_issue_body $'repo: my-repo\n\nFix the bug')"
assert_eq "multiline prompt" $'Line 1\nLine 2' "$(extract_issue_body $'repo: foo\n\nLine 1\nLine 2')"
assert_eq "no blank line" "" "$(extract_issue_body $'repo: foo\nno blank separator')"
assert_eq "CRLF prompt" "Fix the bug" "$(extract_issue_body $'repo: my-repo\r\n\r\nFix the bug')"
assert_eq "CRLF repo" "my-repo" "$(extract_issue_repo $'repo: my-repo\r\n\r\nFix the bug')"

# --- extract_issue_body: empty-prompt guard edge case ---
assert_eq "empty prompt: blank-only body" "" "$(extract_issue_body $'repo: foo\n\n')"

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

mkdir -p "$_TMPHOME/.config/claude"
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
eval "$(grep -m1 '^log() {' "$POLLER")"
_log_out=$(log "hello world")
assert_contains "log: message in output" "hello world" "$_log_out"
[[ "$_log_out" =~ ^\[[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2} ]] \
  && PASS=$((PASS + 1)) \
  || { echo "FAIL: log: ISO-8601 timestamp prefix" >&2; FAIL=$((FAIL + 1)); }
assert_contains "log: written to file" "hello world" "$(cat "$_TMPLOGDIR/poller.log")"
rm -rf "$_TMPLOGDIR"
unset LOG_DIR

# --- mark_issue_failed ---
_TMPLOGDIR=$(mktemp -d)
_GH_LOG=$(mktemp)
export LOG_DIR="$_TMPLOGDIR"
export TASK_QUEUE_REPO="owner/queue"
gh() { echo "$*" >> "$_GH_LOG"; }
_load_fn mark_issue_failed
mark_issue_failed "42" "Something went wrong"
_gh_calls=$(cat "$_GH_LOG")
assert_contains "mark_issue_failed: gh edit removes processing" \
  "issue edit 42 --repo owner/queue --remove-label processing --remove-label queued --add-label failed" \
  "$_gh_calls"
assert_contains "mark_issue_failed: also removes queued label" \
  "--remove-label queued" \
  "$_gh_calls"
assert_contains "mark_issue_failed: gh comment with message" \
  "issue comment 42 --repo owner/queue --body Something went wrong" \
  "$_gh_calls"
assert_contains "mark_issue_failed: logs failure" "Issue #42 failed" "$(cat "$_TMPLOGDIR/poller.log")"
rm -rf "$_TMPLOGDIR"
rm -f "$_GH_LOG"
unset -f gh
unset LOG_DIR TASK_QUEUE_REPO

# --- Integration: issue naming a repo missing from queue-repos.conf is failed ---
_UR_TMPBIN=$(mktemp -d)
_UR_TMPHOME=$(mktemp -d)
_UR_GH_LOG=$(mktemp)
cat > "$_UR_TMPBIN/gh" << 'GHEOF'
#!/bin/bash
echo "$*" >> "$GH_LOG_FILE"
case "$1 $2" in
  "issue list") case "$*" in *"--label processing"*) ;; *) echo '{"author":{"login":"tester"},"number":8,"title":"Nope","body":"repo: nosuchrepo\n\nDo something"}' ;; esac ;;
  "api repos/"*) echo "tester" ;;
  "api graphql") GQL_BODY="${GQL_BODY-repo: nosuchrepo\n\nDo something}" "$GQL_HELPER" "$@" ;;
esac
exit 0
GHEOF
chmod +x "$_UR_TMPBIN/gh"
mkdir -p "$_UR_TMPHOME/.config/claude"
printf 'REPO_PATH[testrepo]="/nonexistent"\nREPO_REMOTE[testrepo]="owner/testrepo"\n' \
  > "$_UR_TMPHOME/.config/claude/queue-repos.conf"
_UR_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_UR_TMPHOME/test.lock"'"|' "$POLLER" > "$_UR_PATCHED"
set +e
GH_LOG_FILE="$_UR_GH_LOG" CLAUDE_QUEUE_REPO="test/q" CLAUDE_QUEUE_TIMEOUT=10 \
  HOME="$_UR_TMPHOME" PATH="$_UR_TMPBIN:$PATH" bash "$_UR_PATCHED" >/dev/null 2>&1
set -e
_ur_gh=$(cat "$_UR_GH_LOG")
rm -rf "$_UR_TMPBIN" "$_UR_TMPHOME" "$_UR_PATCHED" "$_UR_GH_LOG"
assert_contains "unknown repo: issue marked failed" "issue edit 8 --repo test/q --remove-label processing --remove-label queued --add-label failed" "$_ur_gh"
assert_contains "unknown repo: comment names the repo" 'Unknown repo: `nosuchrepo`' "$_ur_gh"

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
  "issue list") case "$*" in *"--label processing"*) ;; *) echo '{"author":{"login":"tester"},"number":7,"title":"Fix the thing","body":"repo: testrepo\n\nDo something useful"}' ;; esac ;;
  "api repos/"*) echo "${EVENTS_ACTOR-tester}" ;;
  "api graphql") GQL_BODY="${GQL_BODY-repo: testrepo\n\nDo something useful}" "$GQL_HELPER" "$@" ;;
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
echo "claude $*" >> "$GH_LOG_FILE"
cat > /dev/null
exit 0
CLAUDEOF
chmod +x "$_INT_TMPBIN/claude-stub"

mkdir -p "$_INT_TMPHOME/.config/claude"
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
# Logs hold the issue prompt output: the poller (umask 077; chmod 700 on the directory) must keep them owner-only.
_mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
_int_logdir="$_INT_TMPHOME/.local/state/claude/logs"
_int_logmodes="$(_mode "$_int_logdir" || echo missing) $( (for _f in "$_int_logdir"/*.log; do _mode "$_f" || echo missing; done | sort -u | tr '\n' ' ') || true)"
rm -rf "$_INT_TMPBIN" "$_INT_TMPHOME" "$_INT_TMPLOCKDIR" "$_INT_TMPREPO"
rm -f "$_INT_GH_LOG"

assert_eq       "integration: exit 0"             "0" "$_int_exit"
assert_eq       "integration: log dir and files owner-only" "700 600 " "$_int_logmodes"
assert_contains "integration: queued→processing"  \
  "issue edit 7 --repo test/q --remove-label queued --add-label processing" "$_int_gh"
assert_contains "integration: pr create called"   "pr create"          "$_int_gh"
assert_contains "integration: default tool allowlist" \
  "claude -p --allowedTools Read,Edit,Write,Bash(git add *),Bash(git commit *),Bash(git diff *),Bash(git log *),Bash(git status) --max-turns" "$_int_gh"
assert_contains "integration: processing→done"    "--add-label done"   "$_int_gh"
assert_contains "integration: issue closed"       "issue close 7"      "$_int_gh"
assert_contains "integration: completed log line" "Issue #7 completed" "$_int_out"

# --- Integration: no-changes failure path (git diff --quiet exits 0 → mark_issue_failed) ---
_NC_TMPBIN=$(mktemp -d)
_NC_TMPHOME=$(mktemp -d)
_NC_TMPLOCKDIR=$(mktemp -d)
_NC_TMPREPO=$(mktemp -d)
_NC_GH_LOG=$(mktemp)

cat > "$_NC_TMPBIN/gh" << 'GHEOF'
#!/bin/bash
echo "$*" >> "$GH_LOG_FILE"
case "$1 $2" in
  "issue list") case "$*" in *"--label processing"*) ;; *) echo '{"author":{"login":"tester"},"number":8,"title":"No-op task","body":"repo: testrepo\n\nDo something useful"}' ;; esac ;;
  "api repos/"*) echo "${EVENTS_ACTOR-tester}" ;;
  "api graphql") GQL_BODY="${GQL_BODY-repo: testrepo\n\nDo something useful}" "$GQL_HELPER" "$@" ;;
esac
exit 0
GHEOF
chmod +x "$_NC_TMPBIN/gh"

# Stub git: exit 0 for all calls including diff --quiet — simulates no changes produced
cat > "$_NC_TMPBIN/git" << 'GITEOF'
#!/bin/bash
echo "git $*" >> "$GH_LOG_FILE"
exit 0
GITEOF
chmod +x "$_NC_TMPBIN/git"

cat > "$_NC_TMPBIN/claude-stub" << 'CLAUDEOF'
#!/bin/bash
cat > /dev/null
exit 0
CLAUDEOF
chmod +x "$_NC_TMPBIN/claude-stub"

mkdir -p "$_NC_TMPHOME/.config/claude"
printf 'REPO_PATH[testrepo]="%s"\nREPO_REMOTE[testrepo]="owner/testrepo"\n' \
  "$_NC_TMPREPO" > "$_NC_TMPHOME/.config/claude/queue-repos.conf"

_NC_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_NC_TMPLOCKDIR/test.lock"'"|' \
  "$POLLER" > "$_NC_PATCHED"

set +e
_nc_out=$(
  GH_LOG_FILE="$_NC_GH_LOG" \
  CLAUDE_QUEUE_REPO="test/q" \
  CLAUDE_BIN="$_NC_TMPBIN/claude-stub" \
  CLAUDE_QUEUE_TIMEOUT=10 \
  HOME="$_NC_TMPHOME" \
  PATH="$_NC_TMPBIN:$PATH" \
  bash "$_NC_PATCHED" 2>&1
)
_nc_exit=$?
set -e
rm -f "$_NC_PATCHED"

_nc_gh=$(cat "$_NC_GH_LOG")
rm -rf "$_NC_TMPBIN" "$_NC_TMPHOME" "$_NC_TMPLOCKDIR" "$_NC_TMPREPO"
rm -f "$_NC_GH_LOG"

assert_eq           "no-changes: exit 0"                          "0"                  "$_nc_exit"
assert_contains     "no-changes: mark_issue_failed adds failed label"    "--add-label failed" "$_nc_gh"
assert_contains     "no-changes: fail comment mentions no changes" "no changes"        "$_nc_gh"
assert_not_contains "no-changes: done label not added"            "--add-label done"   "$_nc_gh"
assert_not_contains "no-changes: issue not closed"                "issue close"        "$_nc_gh"

# --- Integration: Claude non-zero exit path ---
_CE_TMPBIN=$(mktemp -d)
_CE_TMPHOME=$(mktemp -d)
_CE_TMPLOCKDIR=$(mktemp -d)
_CE_TMPREPO=$(mktemp -d)
_CE_GH_LOG=$(mktemp)

cat > "$_CE_TMPBIN/gh" << 'GHEOF'
#!/bin/bash
echo "$*" >> "$GH_LOG_FILE"
case "$1 $2" in
  "issue list") case "$*" in *"--label processing"*) ;; *) echo '{"author":{"login":"tester"},"number":9,"title":"Fail task","body":"repo: testrepo\n\nDo something"}' ;; esac ;;
  "api repos/"*) echo "${EVENTS_ACTOR-tester}" ;;
  "api graphql") GQL_BODY="${GQL_BODY-repo: testrepo\n\nDo something}" "$GQL_HELPER" "$@" ;;
  "pr create")  echo "https://github.com/owner/testrepo/pull/100" ;;
esac
exit 0
GHEOF
chmod +x "$_CE_TMPBIN/gh"

# Stub git: log calls; diff --quiet exits 0 (no changes checked; CLAUDE_EXIT fires first)
cat > "$_CE_TMPBIN/git" << 'GITEOF'
#!/bin/bash
echo "git $*" >> "$GH_LOG_FILE"
[[ "$1 $2" == "rev-list --count" ]] && echo 1
exit 0
GITEOF
chmod +x "$_CE_TMPBIN/git"

# Stub CLAUDE_BIN: exit 1 to trigger the non-zero exit path
cat > "$_CE_TMPBIN/claude-stub" << 'CLAUDEOF'
#!/bin/bash
cat > /dev/null
exit 1
CLAUDEOF
chmod +x "$_CE_TMPBIN/claude-stub"

mkdir -p "$_CE_TMPHOME/.config/claude"
printf 'REPO_PATH[testrepo]="%s"\nREPO_REMOTE[testrepo]="owner/testrepo"\n' \
  "$_CE_TMPREPO" > "$_CE_TMPHOME/.config/claude/queue-repos.conf"

_CE_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_CE_TMPLOCKDIR/test.lock"'"|' \
  "$POLLER" > "$_CE_PATCHED"

set +e
_ce_out=$(
  GH_LOG_FILE="$_CE_GH_LOG" \
  CLAUDE_QUEUE_REPO="test/q" \
  CLAUDE_BIN="$_CE_TMPBIN/claude-stub" \
  CLAUDE_QUEUE_TIMEOUT=10 \
  HOME="$_CE_TMPHOME" \
  PATH="$_CE_TMPBIN:$PATH" \
  bash "$_CE_PATCHED" 2>&1
)
_ce_exit=$?
set -e
rm -f "$_CE_PATCHED"

_ce_gh=$(cat "$_CE_GH_LOG")
rm -rf "$_CE_TMPBIN" "$_CE_TMPHOME" "$_CE_TMPLOCKDIR" "$_CE_TMPREPO"
rm -f "$_CE_GH_LOG"

assert_eq           "claude-exit: exit 0"                          "0"                  "$_ce_exit"
assert_contains     "claude-exit: mark_issue_failed removes processing"   "--remove-label processing --remove-label queued --add-label failed" "$_ce_gh"
assert_contains     "claude-exit: comment mentions exit 1"         "exit 1"             "$_ce_gh"
assert_contains     "claude-exit: branch pushed"                   "git push origin"    "$_ce_gh"
assert_contains     "claude-exit: clone reset"                     "reset --hard"       "$_ce_gh"
assert_contains     "claude-exit: clone cleaned"                   "clean -fd"          "$_ce_gh"
assert_not_contains "claude-exit: done label not added"            "--add-label done"   "$_ce_gh"
assert_not_contains "claude-exit: issue not closed"                "issue close"        "$_ce_gh"

# --- Clone safety: never discard work the poller did not create ---
_load_fn clone_is_disposable
_load_fn cleanup_clone
_CS=$(mktemp -d)
git init -q -b main "$_CS/repo"
git -C "$_CS/repo" -c core.hooksPath=/dev/null -c user.name=t -c user.email=t@example.invalid commit -q --allow-empty -m init
echo mine > "$_CS/repo/notes.txt"
git -C "$_CS/repo" checkout -q -b feature/mine
if clone_is_disposable "$_CS/repo"; then _r=yes; else _r=no; fi
assert_eq "clone-safety: untracked file on user branch is not disposable" "no" "$_r"
if cleanup_clone "$_CS/repo" main; then _r=yes; else _r=no; fi
assert_eq "clone-safety: cleanup refuses user branch" "no" "$_r"
assert_eq "clone-safety: user file survives" "mine" "$(cat "$_CS/repo/notes.txt")"
assert_eq "clone-safety: user branch kept" "feature/mine" "$(git -C "$_CS/repo" branch --show-current)"
git -C "$_CS/repo" checkout -q -b claude/7-task
if clone_is_disposable "$_CS/repo"; then _r=yes; else _r=no; fi
assert_eq "clone-safety: dirty poller branch is disposable" "yes" "$_r"
cleanup_clone "$_CS/repo" main
assert_eq "clone-safety: poller branch cleaned" "main" "$(git -C "$_CS/repo" branch --show-current)"
assert_eq "clone-safety: poller leftovers removed" "no" "$([[ -e $_CS/repo/notes.txt ]] && echo yes || echo no)"
git -C "$_CS/repo" checkout -q -b feature/clean
if clone_is_disposable "$_CS/repo"; then _r=yes; else _r=no; fi
assert_eq "clone-safety: clean tree is disposable" "yes" "$_r"

# Integration: a dirty clone on a user branch fails the issue and never runs Claude
echo mine > "$_CS/repo/notes.txt"
mkdir -p "$_CS/bin" "$_CS/home/.config/claude"
cat > "$_CS/bin/gh" << 'GHEOF'
#!/bin/bash
echo "$*" >> "$GH_LOG_FILE"
case "$1 $2" in
  "issue list") case "$*" in *"--label processing"*) ;; *) echo '{"author":{"login":"tester"},"number":9,"title":"Dirty","body":"x"}' ;; esac ;;
  "api repos/"*) echo tester ;;
  "api graphql") GQL_BODY="repo: testrepo\n\nDo something" "$GQL_HELPER" "$@" ;;
esac
exit 0
GHEOF
printf '#!/bin/bash\ntouch "%s/claude-ran"\ncat >/dev/null\n' "$_CS" > "$_CS/bin/claude-stub"
chmod +x "$_CS/bin/gh" "$_CS/bin/claude-stub"
printf 'REPO_PATH[testrepo]="%s"\nREPO_REMOTE[testrepo]="owner/testrepo"\n' "$_CS/repo" > "$_CS/home/.config/claude/queue-repos.conf"
sed 's|LOCKFILE=.*|LOCKFILE="'"$_CS/test.lock"'"|' "$POLLER" > "$_CS/poller.sh"
: > "$_CS/gh.log"
set +e
GH_LOG_FILE="$_CS/gh.log" CLAUDE_QUEUE_REPO="test/q" CLAUDE_BIN="$_CS/bin/claude-stub" \
  HOME="$_CS/home" PATH="$_CS/bin:$PATH" bash "$_CS/poller.sh" >/dev/null 2>&1
set -e
assert_contains     "clone-safety: dirty user clone fails the issue" "--add-label failed" "$(cat "$_CS/gh.log")"
assert_contains     "clone-safety: comment explains refusal" "did not create" "$(cat "$_CS/gh.log")"
assert_eq           "clone-safety: user edits kept after poller run" "mine" "$(cat "$_CS/repo/notes.txt")"
assert_eq           "clone-safety: claude not run" "no" "$([[ -e $_CS/claude-ran ]] && echo yes || echo no)"
rm -rf "$_CS"

# --- Flock concurrency guard: second instance exits immediately ---
_FLOCK_TMPLOCKDIR=$(mktemp -d)
_FLOCK_TMPHOME=$(mktemp -d)
_FLOCK_TMPBIN=$(mktemp -d)
trap 'rm -rf "$_TMPBIN" "$_TMPHOME" "$_TMPLOCKDIR" "$_ERRBIN" "$_FLOCK_TMPLOCKDIR" "$_FLOCK_TMPHOME" "$_FLOCK_TMPBIN"' EXIT

printf '#!/bin/bash\nexit 0\n' > "$_FLOCK_TMPBIN/gh"
chmod +x "$_FLOCK_TMPBIN/gh"
mkdir -p "$_FLOCK_TMPHOME/.config/claude"
printf 'REPO_PATH[test]="/tmp/stub"\nREPO_REMOTE[test]="a/b"\n' \
  > "$_FLOCK_TMPHOME/.config/claude/queue-repos.conf"

_FLOCK_LOCK="$_FLOCK_TMPLOCKDIR/flock-test.lock"
_FLOCK_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_FLOCK_LOCK"'"|' \
  "$POLLER" > "$_FLOCK_PATCHED"

# Hold the lock in the background before running the script
( flock 200; sleep 10 ) 200>"$_FLOCK_LOCK" &
_FLOCK_BG=$!
# Wait until the background subshell actually holds the lock
for _ in $(seq 1 50); do
  flock -n "$_FLOCK_LOCK" true 2>/dev/null || break
  sleep 0.1
done

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

# --- Integration: HOST_LABEL set → 'on myhost' appears in Processing started comment ---
_HL_SET_TMPBIN=$(mktemp -d)
_HL_SET_TMPHOME=$(mktemp -d)
_HL_SET_TMPLOCKDIR=$(mktemp -d)
_HL_SET_TMPREPO=$(mktemp -d)
_HL_SET_GH_LOG=$(mktemp)

cat > "$_HL_SET_TMPBIN/gh" << 'GHEOF'
#!/bin/bash
echo "$*" >> "$GH_LOG_FILE"
case "$1 $2" in
  "issue list") case "$*" in *"--label processing"*) ;; *) echo '{"author":{"login":"tester"},"number":7,"title":"Fix the thing","body":"repo: testrepo\n\nDo something useful"}' ;; esac ;;
  "api repos/"*) echo "${EVENTS_ACTOR-tester}" ;;
  "api graphql") GQL_BODY="${GQL_BODY-repo: testrepo\n\nDo something useful}" "$GQL_HELPER" "$@" ;;
  "pr create")  echo "https://github.com/owner/testrepo/pull/99" ;;
esac
exit 0
GHEOF
chmod +x "$_HL_SET_TMPBIN/gh"

cat > "$_HL_SET_TMPBIN/git" << 'GITEOF'
#!/bin/bash
echo "git $*" >> "$GH_LOG_FILE"
[[ "$1 $2" == "diff --quiet" ]] && exit 1
exit 0
GITEOF
chmod +x "$_HL_SET_TMPBIN/git"

cat > "$_HL_SET_TMPBIN/claude-stub" << 'CLAUDEOF'
#!/bin/bash
cat > /dev/null
exit 0
CLAUDEOF
chmod +x "$_HL_SET_TMPBIN/claude-stub"

mkdir -p "$_HL_SET_TMPHOME/.config/claude"
printf 'REPO_PATH[testrepo]="%s"\nREPO_REMOTE[testrepo]="owner/testrepo"\n' \
  "$_HL_SET_TMPREPO" > "$_HL_SET_TMPHOME/.config/claude/queue-repos.conf"

_HL_SET_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_HL_SET_TMPLOCKDIR/test.lock"'"|' \
  "$POLLER" > "$_HL_SET_PATCHED"

set +e
_hl_set_out=$(
  GH_LOG_FILE="$_HL_SET_GH_LOG" \
  CLAUDE_QUEUE_REPO="test/q" \
  CLAUDE_BIN="$_HL_SET_TMPBIN/claude-stub" \
  CLAUDE_QUEUE_TIMEOUT=10 \
  CLAUDE_QUEUE_HOST_LABEL=myhost \
  HOME="$_HL_SET_TMPHOME" \
  PATH="$_HL_SET_TMPBIN:$PATH" \
  bash "$_HL_SET_PATCHED" 2>&1
)
_hl_set_exit=$?
set -e
rm -f "$_HL_SET_PATCHED"

_hl_set_gh=$(cat "$_HL_SET_GH_LOG")
rm -rf "$_HL_SET_TMPBIN" "$_HL_SET_TMPHOME" "$_HL_SET_TMPLOCKDIR" "$_HL_SET_TMPREPO"
rm -f "$_HL_SET_GH_LOG"

assert_eq       "host-label-set: exit 0"              "0"        "$_hl_set_exit"
assert_contains "host-label-set: 'on myhost' in comment" "on myhost" "$_hl_set_gh"

# --- Integration: HOST_LABEL unset → no 'on' token in Processing started comment ---
_HL_UNSET_TMPBIN=$(mktemp -d)
_HL_UNSET_TMPHOME=$(mktemp -d)
_HL_UNSET_TMPLOCKDIR=$(mktemp -d)
_HL_UNSET_TMPREPO=$(mktemp -d)
_HL_UNSET_GH_LOG=$(mktemp)

cat > "$_HL_UNSET_TMPBIN/gh" << 'GHEOF'
#!/bin/bash
echo "$*" >> "$GH_LOG_FILE"
case "$1 $2" in
  "issue list") case "$*" in *"--label processing"*) ;; *) echo '{"author":{"login":"tester"},"number":7,"title":"Fix the thing","body":"repo: testrepo\n\nDo something useful"}' ;; esac ;;
  "api repos/"*) echo "${EVENTS_ACTOR-tester}" ;;
  "api graphql") GQL_BODY="${GQL_BODY-repo: testrepo\n\nDo something useful}" "$GQL_HELPER" "$@" ;;
  "pr create")  echo "https://github.com/owner/testrepo/pull/99" ;;
esac
exit 0
GHEOF
chmod +x "$_HL_UNSET_TMPBIN/gh"

cat > "$_HL_UNSET_TMPBIN/git" << 'GITEOF'
#!/bin/bash
echo "git $*" >> "$GH_LOG_FILE"
[[ "$1 $2" == "diff --quiet" ]] && exit 1
exit 0
GITEOF
chmod +x "$_HL_UNSET_TMPBIN/git"

cat > "$_HL_UNSET_TMPBIN/claude-stub" << 'CLAUDEOF'
#!/bin/bash
cat > /dev/null
exit 0
CLAUDEOF
chmod +x "$_HL_UNSET_TMPBIN/claude-stub"

mkdir -p "$_HL_UNSET_TMPHOME/.config/claude"
printf 'REPO_PATH[testrepo]="%s"\nREPO_REMOTE[testrepo]="owner/testrepo"\n' \
  "$_HL_UNSET_TMPREPO" > "$_HL_UNSET_TMPHOME/.config/claude/queue-repos.conf"

_HL_UNSET_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_HL_UNSET_TMPLOCKDIR/test.lock"'"|' \
  "$POLLER" > "$_HL_UNSET_PATCHED"

set +e
_hl_unset_out=$(
  GH_LOG_FILE="$_HL_UNSET_GH_LOG" \
  CLAUDE_QUEUE_REPO="test/q" \
  CLAUDE_BIN="$_HL_UNSET_TMPBIN/claude-stub" \
  CLAUDE_QUEUE_TIMEOUT=10 \
  HOME="$_HL_UNSET_TMPHOME" \
  PATH="$_HL_UNSET_TMPBIN:$PATH" \
  bash "$_HL_UNSET_PATCHED" 2>&1
)
_hl_unset_exit=$?
set -e
rm -f "$_HL_UNSET_PATCHED"

_hl_unset_gh=$(cat "$_HL_UNSET_GH_LOG")
rm -rf "$_HL_UNSET_TMPBIN" "$_HL_UNSET_TMPHOME" "$_HL_UNSET_TMPLOCKDIR" "$_HL_UNSET_TMPREPO"
rm -f "$_HL_UNSET_GH_LOG"

assert_eq           "host-label-unset: exit 0"             "0"    "$_hl_unset_exit"
assert_not_contains "host-label-unset: no 'on' in comment" " on " "$_hl_unset_gh"

# --- Empty author allowlist: the poller refuses to run (fail closed) ---
set +e
_empty_home=$(mktemp -d)
_empty_out=$(CLAUDE_QUEUE_ALLOWED_AUTHORS="" CLAUDE_QUEUE_REPO="test/q" HOME="$_empty_home" bash "$POLLER" 2>&1)
_empty_exit=$?
rm -rf "$_empty_home"
set -e
assert_eq           "empty allowlist: exit 1"         "1"           "$_empty_exit"
assert_contains     "empty allowlist: refusal logged" "refusing to run" "$_empty_out"

# --- is_allowed_actor (extracted from the poller) ---
eval "$(sed -n '/^is_allowed_actor() {/,/^}/p' "$POLLER")"
ALLOWED_AUTHORS="Alice, bob-1 ,,a.c"
_allowed() { if is_allowed_actor "$1"; then echo yes; else echo no; fi; }
assert_eq "allow: exact"             "yes" "$(_allowed alice)"
assert_eq "allow: case-insensitive"  "yes" "$(_allowed ALICE)"
assert_eq "allow: trimmed entry"     "yes" "$(_allowed bob-1)"
assert_eq "allow: empty author"      "no"  "$(_allowed "")"
assert_eq "allow: regex dot literal" "no"  "$(_allowed abc)"
assert_eq "allow: regex .* literal"  "no"  "$(_allowed '.*')"
assert_eq "allow: partial no match"  "no"  "$(_allowed ali)"
assert_eq "allow: literal dot match" "yes" "$(_allowed a.c)"
# shellcheck disable=SC2034  # read by is_allowed_actor (eval-extracted)
ALLOWED_AUTHORS=","
assert_eq "allow: only empty entries" "no" "$(_allowed "")"
assert_eq "allow: only empty entries, real login" "no" "$(_allowed alice)"
unset ALLOWED_AUTHORS

# --- conf_value (extracted from the poller) ---
eval "$(sed -n '/^conf_value() {/,/^}/p' "$POLLER")"
assert_eq "conf: quoted"                "Read,Bash(git add *)" "$(conf_value '"Read,Bash(git add *)"')"
assert_eq "conf: quoted + comment"      "Read,Edit" "$(conf_value '"Read,Edit"   # optional')"
assert_eq "conf: unquoted + comment"    "main"      "$(conf_value 'main   # default')"
assert_eq "conf: unquoted plain"        "/a/b#c"    "$(conf_value '/a/b#c')"

# --- Integration: label actor / body editor authorization ---
_auth_run() { # args: EVENTS_ACTOR; env: ISSUE_AUTHOR, EVENTS_JSON, GQL_*; prints gh log
  local bin home lock repo log
  bin=$(mktemp -d); home=$(mktemp -d); lock=$(mktemp -d); repo=$(mktemp -d); log=$(mktemp)
  cat > "$bin/gh" << 'GHEOF'
#!/bin/bash
echo "$*" >> "$GH_LOG_FILE"
case "$1 $2" in
  "issue list") case "$*" in *"--label processing"*) ;; *) printf '{"author":{"login":"%s"},"number":7,"title":"T","body":"repo: testrepo\\n\\nDo it"}\n' "${ISSUE_AUTHOR:-tester}" ;; esac ;;
  "api repos/"*)
    if [[ -n "${EVENTS_JSON:-}" ]]; then
      # apply the poller's real --jq filter to a fixture of issue events
      while [[ $# -gt 0 ]]; do [[ "$1" == "--jq" ]] && { printf '%s' "$EVENTS_JSON" | jq -r "$2"; break; }; shift; done
    else echo "$EVENTS_ACTOR"; fi ;;
  "api graphql") GQL_BODY="${GQL_BODY-repo: testrepo\n\nDo it}" "$GQL_HELPER" "$@" ;;
  "pr create")  echo "https://github.com/owner/testrepo/pull/99" ;;
esac
exit 0
GHEOF
  printf '#!/bin/bash\nexit 0\n' > "$bin/git"
  printf '#!/bin/bash\ncat >/dev/null\nexit 0\n' > "$bin/claude-stub"
  chmod +x "$bin"/*
  printf 'REPO_PATH[testrepo]="%s"\nREPO_REMOTE[testrepo]="owner/testrepo"\n' "$repo" \
    > "$(mkdir -p "$home/.config/claude" && echo "$home/.config/claude")/queue-repos.conf"
  sed 's|LOCKFILE=.*|LOCKFILE="'"$lock/test.lock"'"|' "$POLLER" > "$lock/p.sh"
  GH_LOG_FILE="$log" ISSUE_AUTHOR="${ISSUE_AUTHOR:-}" EVENTS_JSON="${EVENTS_JSON:-}" EVENTS_ACTOR="$1" CLAUDE_QUEUE_REPO="test/q" \
    CLAUDE_BIN="$bin/claude-stub" HOME="$home" PATH="$bin:$PATH" \
    bash "$lock/p.sh" >/dev/null 2>&1 || true
  cat "$log"
  rm -rf "$bin" "$home" "$lock" "$repo" "$log"
}
_o=$(_auth_run "tester")
assert_contains     "auth: all ok proceeds"          "--add-label processing" "$_o"
_o=$(_auth_run "mallory")
assert_contains     "auth: foreign labeler rejected" "queued\` label was last applied by \`mallory" "$_o"
assert_not_contains "auth: foreign labeler not run"  "--add-label processing" "$_o"
_o=$(_auth_run "")
assert_contains     "auth: unknown labeler rejected" "<unknown>" "$_o"
_o=$(GQL_EDITED=2026-01-01T00:00:00Z GQL_EDITOR=mallory _auth_run "tester")
assert_contains     "auth: foreign editor rejected"  "edited by \`mallory" "$_o"
assert_not_contains "auth: foreign editor not run"   "--add-label processing" "$_o"
_o=$(GQL_EDITED=2026-01-01T00:00:00Z GQL_EDITOR=Tester _auth_run "TESTER")
assert_contains     "auth: allowlisted editor ok"    "--add-label processing" "$_o"
_o=$(GQL_EDITED=2026-01-01T00:00:00Z GQL_EDITOR='' _auth_run "tester")
assert_contains     "auth: edited by deleted user rejected" "edited by \`<unknown>" "$_o"
# Unedited body: the editor field is not consulted, so a stray editor does not block
_o=$(GQL_EDITOR=mallory _auth_run "tester")
assert_contains     "auth: unedited body skips editor check" "--add-label processing" "$_o"
# Every editor in the history must be allowlisted, not just the last one
_o=$(GQL_EDITED=2026-01-01T00:00:00Z GQL_EDITOR=tester GQL_HIST=tester,mallory _auth_run "tester")
assert_contains     "auth: earlier foreign editor rejected" "edited by \`mallory" "$_o"
assert_not_contains "auth: earlier foreign editor not run" "--add-label processing" "$_o"
_o=$(GQL_EDITED=2026-01-01T00:00:00Z GQL_EDITOR=tester GQL_HIST=tester,- _auth_run "tester")
assert_contains     "auth: deleted historic editor rejected" "edited by \`<unknown>" "$_o"
_o=$(GQL_EDITED=2026-01-01T00:00:00Z GQL_EDITOR=tester GQL_HIST=tester,TESTER _auth_run "tester")
assert_contains     "auth: allowlisted history ok" "--add-label processing" "$_o"
_o=$(GQL_EDITED=2026-01-01T00:00:00Z GQL_EDITOR=tester GQL_HIST=tester GQL_MORE=1 _auth_run "tester")
assert_contains     "auth: truncated history rejected" "could not verify who edited" "$_o"
assert_not_contains "auth: truncated history not run" "--add-label processing" "$_o"
_o=$(GQL_FAIL=1 _auth_run "tester")
assert_contains     "auth: edit lookup failure rejects" "could not verify who edited" "$_o"
# The prompt comes from the body fetched with the edit history, not the list snapshot
_o=$(GQL_BODY='no repo line here' _auth_run "tester")
assert_contains     "auth: refetched body is used" "Missing \`repo:\` line" "$_o"

# Foreign issue author is rejected even when label and edit metadata are clean
_o=$(ISSUE_AUTHOR=mallory _auth_run "tester")
assert_contains     "auth: foreign author rejected"  "Unauthorized author: \`mallory" "$_o"
assert_not_contains "auth: foreign author not run"   "--add-label processing" "$_o"

# The real label-actor jq filter: only `queued` events count, and the last one wins
_ev_q() { printf '{"event":"labeled","label":{"name":"%s"},"actor":{"login":"%s"}}' "$1" "$2"; }
_o=$(EVENTS_JSON="[$(_ev_q queued mallory),$(_ev_q bug tester)]" _auth_run "")
assert_contains     "auth: later non-queue label does not launder" "last applied by \`mallory" "$_o"
_o=$(EVENTS_JSON="[$(_ev_q queued tester),$(_ev_q queued mallory)]" _auth_run "")
assert_contains     "auth: last queued actor wins (foreign)" "last applied by \`mallory" "$_o"
_o=$(EVENTS_JSON="[$(_ev_q queued mallory),$(_ev_q queued tester)]" _auth_run "")
assert_contains     "auth: last queued actor wins (allowed)" "--add-label processing" "$_o"

rm -f "$GQL_HELPER"
echo "Results: $PASS passed, $FAIL failed"
exit $((FAIL > 0 ? 1 : 0))

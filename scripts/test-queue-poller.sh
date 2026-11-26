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
# The poller refuses to run without gitleaks; tests that need its verdict put their own stub first on PATH.
_STUBS=$(mktemp -d)
printf '#!/bin/bash\nexit 0\n' > "$_STUBS/gitleaks"
chmod +x "$_STUBS/gitleaks"
export PATH="$_STUBS:$PATH"

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

# Logs past 20000 lines keep their last 20000 (macOS has no logrotate); claude-queue.log, launchd's stderr file,
# is rewritten in place
_qlogs="$_TMPHOME/.local/state/claude/logs"
seq 1 20500 > "$_qlogs/poller.log"
seq 1 20500 > "$_qlogs/claude-queue.log"
_inode_before=$(ls -i "$_qlogs/claude-queue.log" | awk '{print $1}')
_PATCHED=$(mktemp)
sed 's|LOCKFILE=.*|LOCKFILE="'"$_TMPLOCKDIR/test.lock"'"|' "$POLLER" > "$_PATCHED"
_qpoll() { CLAUDE_QUEUE_REPO="test/q" HOME="$_TMPHOME" PATH="$_TMPBIN:$PATH" bash "$_PATCHED" >/dev/null 2>&1 || true; }
_qpoll
assert_eq "log trim: poller.log keeps the last 20000 lines, then this run's" "501 20500" \
  "$(head -1 "$_qlogs/poller.log") $(sed -n 20000p "$_qlogs/poller.log")"
assert_eq "log trim: claude-queue.log trimmed in place" "501 20000 $_inode_before" \
  "$(head -1 "$_qlogs/claude-queue.log") $(wc -l < "$_qlogs/claude-queue.log" | tr -d ' ') $(ls -i "$_qlogs/claude-queue.log" | awk '{print $1}')"
_tmps=("$_qlogs"/*.tmp)
assert_eq "log trim: no temporary file left" "no" "$([[ -e "${_tmps[0]}" ]] && echo yes || echo no)"
# At exactly 20000 lines nothing is trimmed (poller.log gets this run's lines after them)
seq 1 20000 > "$_qlogs/poller.log"
seq 1 20000 > "$_qlogs/claude-queue.log"
_qpoll
assert_eq "log trim: 20000 lines are left alone" "1 1 20000" \
  "$(head -1 "$_qlogs/poller.log") $(head -1 "$_qlogs/claude-queue.log") $(wc -l < "$_qlogs/claude-queue.log" | tr -d ' ')"
rm -f "$_PATCHED"
unset -f _qpoll

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
assert_contains "unknown repo: comment names the repo" 'Unknown or incomplete repo: `nosuchrepo`' "$_ur_gh"

# --- Integration: queued issues run against a real clone of a local origin ---
# _qrun <number> <title>: run the poller on one queued issue for repo "testrepo". The stub claude runs $CLAUDE_ACT in
# its working directory and exits $CLAUDE_RC; the stub gitleaks exits $GITLEAKS_RC. The clone sits on a user branch
# with an untracked file, and core.hooksPath=.githooks runs tracked hooks that leave a mark.
# Sets _Q (scratch dir, removed by _qdone), _q_out, _q_exit, _q_gh (gh, claude and gitleaks calls).
_qrun() {
  _qsetup
  set +e
  _q_out=$(_qpoller "$1" "$2" 2>&1)
  _q_exit=$?
  set -e
  _q_gh=$(cat "$_Q/gh.log")
}
# _qpoller <number> <title> [launcher...]: run the poller _qsetup prepared, through the launcher if one is given
_qpoller() {
  local num="$1" title="$2"
  shift 2
  GH_LOG_FILE="$_Q/gh.log" Q_NUM="$num" Q_TITLE="$title" CLAUDE_QUEUE_REPO="test/q" CLAUDE_BIN="$_Q/bin/claude-stub" \
    CLAUDE_QUEUE_TIMEOUT="${Q_TIMEOUT:-10}" HOME="$_Q/home" GIT_CONFIG_NOSYSTEM=1 GIT_COMMITTER_NAME=Poller GIT_COMMITTER_EMAIL=p@example.invalid \
    GIT_AUTHOR_NAME=Poller GIT_AUTHOR_EMAIL=p@example.invalid PATH="$_Q/bin:$PATH" "$@" bash "$_Q/poller.sh"
}
_qsetup() {
  _Q=$(mktemp -d)
  local g=(env HOME="$_Q/home" GIT_CONFIG_NOSYSTEM=1 git)
  mkdir -p "$_Q/bin" "$_Q/home/.config/claude" "$_Q/seed/.githooks"
  "${g[@]}" init -q -b main "$_Q/seed"
  printf '#!/bin/sh\ntouch "%s/hook-ran"\n' "$_Q" > "$_Q/seed/.githooks/pre-commit"
  chmod +x "$_Q/seed/.githooks/pre-commit"
  echo 'ignored/' > "$_Q/seed/.gitignore"
  "${g[@]}" -C "$_Q/seed" add -A
  "${g[@]}" -C "$_Q/seed" -c user.name=t -c user.email=t@example.invalid commit -q -m init
  "${g[@]}" clone -q --bare "$_Q/seed" "$_Q/origin.git"
  "${g[@]}" clone -q "$_Q/origin.git" "$_Q/clone"
  "${g[@]}" -C "$_Q/clone" config core.hooksPath .githooks
  "${g[@]}" -C "$_Q/clone" remote set-url origin https://github.com/owner/testrepo.git
  "${g[@]}" -C "$_Q/clone" config url."$_Q/origin.git".insteadOf https://github.com/owner/testrepo.git
  "${g[@]}" -C "$_Q/clone" checkout -q -b feature/mine
  echo mine > "$_Q/clone/notes.txt"
  eval "${Q_SETUP:-}"
  cat > "$_Q/bin/gh" << 'GHEOF'
#!/bin/bash
echo "$*" >> "$GH_LOG_FILE"
case "$1 $2" in
  "issue list") case "$*" in *"--label processing"*) ;; *) printf '{"author":{"login":"tester"},"number":%s,"title":"%s","body":"x"}\n' "$Q_NUM" "$Q_TITLE" ;; esac ;;
  "api repos/"*) echo tester ;;
  "api graphql") GQL_BODY="repo: testrepo\n\nDo something useful" "$GQL_HELPER" "$@" ;;
  "pr create")  echo "https://github.com/owner/testrepo/pull/99" ;;
esac
exit 0
GHEOF
  printf '#!/bin/bash\ncat >/dev/null\necho "claude $* in $PWD" >> "$GH_LOG_FILE"\necho "claude git dir $GIT_DIR" >> "$GH_LOG_FILE"\neval "${CLAUDE_ACT:-}"\nexit "${CLAUDE_RC:-0}"\n' \
    > "$_Q/bin/claude-stub"
  printf '#!/bin/bash\necho "gitleaks $*" >> "$GH_LOG_FILE"\n[ -z "${GITLEAKS_HANG:-}" ] || { echo $$ > "$GH_LOG_FILE.gitleaks"; sleep 40; }\nexit "${GITLEAKS_RC:-0}"\n' > "$_Q/bin/gitleaks"
  chmod +x "$_Q/bin"/*
  printf 'REPO_PATH[testrepo]="%s"\nREPO_REMOTE[testrepo]="owner/testrepo"\n%s\n' "$_Q/clone" "${Q_CONF:-}" \
    > "$_Q/home/.config/claude/queue-repos.conf"
  sed 's|LOCKFILE=.*|LOCKFILE="'"$_Q/test.lock"'"|' "$POLLER" > "$_Q/poller.sh"
  : > "$_Q/gh.log"
}
_qdone() { rm -rf "$_Q"; }
_mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
_has() { [[ -e "$1" ]] && echo yes || echo no; }
# The user's clone after a run: same branch, untracked file kept, no worktree or branch left behind
_q_clone_untouched() {
  assert_eq "$1: clone stays on the user's branch" "feature/mine" "$(git -C "$_Q/clone" branch --show-current)"
  assert_eq "$1: user's untracked file kept" "mine" "$(cat "$_Q/clone/notes.txt")"
  assert_eq "$1: worktree and its registration removed" "1 " \
    "$(git -C "$_Q/clone" worktree list | wc -l | tr -d ' ') $(ls "$_Q/clone/.git/worktrees" 2>/dev/null)"
  assert_eq "$1: no local poller branch" "" "$(git -C "$_Q/clone" branch --list 'claude/*')"
}

# Success: the agent's edits become one signed-off commit on a pushed branch; ignored files and hooks go nowhere
CLAUDE_ACT='echo new > feature.txt; mkdir -p ignored; echo planted > ignored/python' _qrun 7 "Fix the thing"
_wt=$(sed -n 's/^claude .* in //p' <<< "$_q_gh")
assert_eq       "integration: exit 0"             "0" "$_q_exit"
# Logs hold the issue prompt output: the poller (umask 077; chmod 700 on the directory) must keep them owner-only.
_logdir="$_Q/home/.local/state/claude/logs"
assert_eq       "integration: log dir and files owner-only" "700 600 " \
  "$(_mode "$_logdir" || echo missing) $( (for _f in "$_logdir"/*.log; do _mode "$_f" || echo missing; done | sort -u | tr '\n' ' ') || true)"
assert_contains "integration: queued→processing"  "issue edit 7 --repo test/q --remove-label queued --add-label processing" "$_q_gh"
assert_contains "integration: default tool allowlist, permission mode pinned" \
  "claude -p --restricted --strict-mcp-config --tools Read,Grep,Glob,Edit,Write --permission-mode dontAsk --allowedTools Read(./**),Edit(./**) --max-turns" "$_q_gh"
assert_eq       "integration: claude ran in a worktree, not the clone" "yes no" \
  "$([[ "$_wt" == */testrepo ]] && echo yes || echo no) $([[ "$_wt" == "$_Q/clone"* ]] && echo yes || echo no)"
assert_eq       "integration: claude's git names the real git directory, not the worktree's .git file" "yes" \
  "$([[ "$_q_gh" == *"claude git dir /"*"/clone/.git/worktrees/testrepo"* ]] && echo yes || echo no)"
assert_eq       "integration: worktree and its mktemp directory gone" "no" "$(_has "$(dirname "$_wt")")"
assert_eq       "integration: pushed branch holds the edit" "new" \
  "$(git --git-dir="$_Q/origin.git" show claude/7-fix-the-thing:feature.txt 2>&1)"
assert_eq       "integration: one commit, message names the issue and signs off" "1|Fix the thing||From test/q#7.||Signed-off-by: Poller <p@example.invalid>" \
  "$(git --git-dir="$_Q/origin.git" rev-list --count main..claude/7-fix-the-thing)|$(git --git-dir="$_Q/origin.git" log -1 --format=%B claude/7-fix-the-thing | sed '/^$/{$d;}' | paste -sd'|' -)"
assert_eq       "integration: ignored file neither committed nor left behind" "no no" \
  "$(git --git-dir="$_Q/origin.git" cat-file -e claude/7-fix-the-thing:ignored/python 2>/dev/null && echo yes || echo no) $(_has "$_wt/ignored")"
assert_eq       "integration: tracked commit hook not run" "no" "$(_has "$_Q/hook-ran")"
assert_contains "integration: gitleaks scans the new commit" \
  "gitleaks git --no-banner --redact --log-opts=$(git --git-dir="$_Q/origin.git" rev-parse main)..$(git --git-dir="$_Q/origin.git" rev-parse claude/7-fix-the-thing)" "$_q_gh"
assert_contains "integration: pr create called"   "pr create"          "$_q_gh"
assert_contains "integration: processing→done"    "--add-label done"   "$_q_gh"
assert_contains "integration: issue closed"       "issue close 7"      "$_q_gh"
assert_contains "integration: completed log line" "Issue #7 completed" "$_q_out"
_q_clone_untouched integration
_qdone

# Failures push nothing: no changes, a failed Claude run, a gitleaks finding, an added gitleaks:allow
_q_failed() { # <name> <expected comment text>
  assert_eq           "$1: exit 0"                "0"                  "$_q_exit"
  assert_contains     "$1: issue marked failed"   "--remove-label processing --remove-label queued --add-label failed" "$_q_gh"
  assert_contains     "$1: comment explains"      "$2"                 "$_q_gh"
  assert_not_contains "$1: done label not added"  "--add-label done"   "$_q_gh"
  assert_not_contains "$1: no PR"                 "pr create"          "$_q_gh"
  assert_eq           "$1: nothing pushed"        ""                   "$(git --git-dir="$_Q/origin.git" branch --list 'claude/*')"
  _q_clone_untouched "$1"
  _qdone
}
_qrun 8 "No-op task"; _q_failed no-changes "made no changes"
CLAUDE_ACT='echo x > f' CLAUDE_RC=1 _qrun 9 "Fail task"; _q_failed claude-exit "exit 1"
CLAUDE_ACT='echo x > f' GITLEAKS_RC=1 _qrun 10 "Leak"; _q_failed gitleaks "gitleaks flagged"
CLAUDE_ACT='echo "k = 1  # gitleaks:allow" > f' _qrun 11 "Allow"; _q_failed allow-marker "gitleaks flagged"
# A marker followed by a long diff (grep -q would stop early, and pipefail would count git's SIGPIPE as no match)
CLAUDE_ACT='{ echo "k = 1  # gitleaks:allow"; seq 1 200000; } > f' _qrun 11 "Allow"; _q_failed allow-marker-long "gitleaks flagged"

# A re-queued issue whose branch the user checked out in a worktree of their own: pushed, the user's branch untouched
Q_SETUP='git -C "$_Q/clone" worktree add -q -b claude/14-retry "$_Q/mine" main' CLAUDE_ACT='echo y > f' _qrun 14 "Retry"
assert_contains "re-queued: PR opened" "pr create" "$_q_gh"
assert_eq       "re-queued: pushed" "y" "$(git --git-dir="$_Q/origin.git" show claude/14-retry:f 2>&1)"
assert_eq       "re-queued: user's branch and worktree untouched" "$(git -C "$_Q/clone" rev-parse main) " \
  "$(git -C "$_Q/clone" rev-parse claude/14-retry) $(git -C "$_Q/mine" status --porcelain)"
_qdone

# The agent points the worktree's .git file elsewhere: the work is still pushed, and the worktree, its mktemp
# directory and its registration are all removed
CLAUDE_ACT='echo z > f; echo "gitdir: /nonexistent" > .git' _qrun 16 "Tamper"
_wt=$(sed -n 's/^claude .* in //p' <<< "$_q_gh")
assert_eq "tampered .git: pushed" "z" "$(git --git-dir="$_Q/origin.git" show claude/16-tamper:f 2>&1)"
assert_eq "tampered .git: mktemp directory gone" "no" "$(_has "$(dirname "$_wt")")"
_q_clone_untouched "tampered .git"
_qdone

# An earlier run's branch is still on origin, with the user's commit on it: refused, never overwritten
Q_SETUP='git -C "$_Q/clone" push -q "$_Q/origin.git" main:refs/heads/claude/15-exists' CLAUDE_ACT='echo y > f' _qrun 15 "Exists"
assert_contains     "branch exists: issue marked failed" "--add-label failed" "$_q_gh"
assert_contains     "branch exists: comment explains" "Could not push \`claude/15-exists\` to origin; nothing was overwritten" "$_q_gh"
assert_not_contains "branch exists: no PR" "pr create" "$_q_gh"
assert_eq           "branch exists: origin branch unchanged" "$(git -C "$_Q/clone" rev-parse main)" \
  "$(git --git-dir="$_Q/origin.git" rev-parse claude/15-exists)"
_q_clone_untouched "branch exists"
_qdone

# A .git below the root (a submodule's, pointing at a config whose core.fsmonitor git add would run): refused
CLAUDE_ACT='mkdir -p sub evil; echo "gitdir: ../evil" > sub/.git; echo x > f' _qrun 22 "Nested git"
_q_failed nested-git "add a \`.git\` below the repository root"
CLAUDE_ACT='mkdir -p sub; echo "gitdir: ../evil" > sub/.GIT; echo x > f' _qrun 23 "Nested GIT"
_q_failed nested-git-case "add a \`.git\` below the repository root"

# A Claude run past the timeout is killed and its work discarded
Q_TIMEOUT=1 CLAUDE_ACT='echo x > f; sleep 5' _qrun 17 "Slow"; _q_failed timeout "exit 124"

# A process the agent leaves running must not hold the poller's lock, or every later run exits "Another instance"
CLAUDE_ACT='echo x > f; (sleep 30 >/dev/null 2>&1 & echo $! > "${GH_LOG_FILE%/*}/bg.pid")' _qrun 24 "Leftover"
assert_eq "leftover process: run succeeds" "0" "$_q_exit"
_lock_free=no; flock -n "$_Q/test.lock" true && _lock_free=yes
kill "$(cat "$_Q/bg.pid")" 2>/dev/null || true
assert_eq "leftover process: lock released when the poller exits" "yes" "$_lock_free"
_qdone

# launchd stops the job (bootout, plist reload) with SIGTERM to the poller, then SIGKILL to its process group, which
# also comes alone: the agent (here one that ignores SIGTERM), in timeout's own group, must outlive neither.  After a
# SIGTERM the worktree, its registration and the prompt file are gone too, and the issue is failed.
_alive() { local s; s=$(ps -o stat= -p "$1" 2>/dev/null) && [[ "$s" != Z* ]]; }
_q_stop() { # <name> <kill arguments, with POLLER for the poller's pid>; Q_STUB: the stub to wait for (claude)
  local name="$1" pid="" stub="" stub_pgid="" i
  shift
  _qsetup
  mkdir "$_Q/tmp"
  # shellcheck disable=SC2016  # expanded by the stub claude
  CLAUDE_ACT=${Q_ACT:-'echo $$ > "$GH_LOG_FILE.claude"; trap "" TERM; sleep 40'} TMPDIR="$_Q/tmp" _qpoller 24 "Stop" \
    perl -e 'setpgrp(0, 0); open(my $f, ">", shift) or die; print $f $$; close $f; exec @ARGV' "$_Q/poller.pid" \
    >"$_Q/out" 2>&1 &
  disown $! # no "Killed" job notice; bash still reaps it
  for ((i = 0; i < 100; i++)); do [[ -s "$_Q/gh.log.${Q_STUB:-claude}" ]] && break; sleep 0.1; done
  pid=$(cat "$_Q/poller.pid" 2>/dev/null) || true
  stub=$(cat "$_Q/gh.log.${Q_STUB:-claude}" 2>/dev/null) || true
  [[ -z "$stub" ]] || stub_pgid=$(ps -o pgid= -p "$stub" | tr -d ' ') || true
  assert_eq "$name: stub ${Q_STUB:-claude} started" "yes" "$([[ -n "$pid" && -n "$stub" ]] && echo yes || echo no)"
  if [[ -n "$pid" && -n "$stub" ]]; then
    kill "${@/POLLER/$pid}" || true
    for ((i = 0; i < 50; i++)); do _alive "$stub" || _alive "$pid" || break; sleep 0.1; done
    assert_eq "$name: agent and poller gone" "no no" \
      "$(_alive "$stub" && echo yes || echo no) $(_alive "$pid" && echo yes || echo no)"
  fi
  # Whatever the outcome, nothing started here outlives the test
  [[ -z "$pid" ]] || kill -KILL -- "-$pid" 2>/dev/null || true
  [[ -z "$stub_pgid" ]] || kill -KILL -- "-$stub_pgid" 2>/dev/null || true
  _q_gh=$(cat "$_Q/gh.log")
}
_q_stop sigterm -TERM POLLER
assert_eq       "sigterm: worktree, registration and prompt file removed" "1  " \
  "$(git -C "$_Q/clone" worktree list | wc -l | tr -d ' ') $(ls "$_Q/clone/.git/worktrees" 2>/dev/null) $(ls "$_Q/tmp")"
assert_contains "sigterm: issue marked failed" "issue edit 24 --repo test/q --remove-label processing --remove-label queued --add-label failed" "$_q_gh"
assert_contains "sigterm: comment explains" "Task interrupted: the poller was stopped (signal 15)" "$_q_gh"
_qdone
_q_stop sigkill -KILL -- -POLLER
_qdone
# systemd stops the unit with SIGTERM to every process in it: one landing while the subshell finishes the issue (here
# in gitleaks, after the agent) kills that subshell too, and the issue is failed, never left in processing
GITLEAKS_HANG=1 Q_STUB=gitleaks Q_ACT='echo x > f' _q_stop systemd-finish -TERM -- -POLLER
assert_contains "systemd-finish: issue marked failed" "Task interrupted: the poller was stopped (signal 15)" "$_q_gh"
assert_eq       "systemd-finish: worktree and prompt file removed" "1 " \
  "$(git -C "$_Q/clone" worktree list | wc -l | tr -d ' ') $(ls "$_Q/tmp")"
_qdone

# A tool list from an older queue-repos.conf, or an origin other than REPO_REMOTE: refused before Claude runs
for _tools in 'Read,Edit(./**)' 'Read(./**),Edit(/etc/**)' 'Edit(./../x)' 'Write' 'Bash(git commit *)' 'Bash(git:*)' \
              'Bash' 'Bash(*)' 'Bash(git add -A)' 'Edit(./**) Edit(//etc/**)' 'Read(./**) Bash(git commit *)' \
              'Bash(/usr/bin/git commit *)' 'Bash(env git commit *)' 'Bash(Git commit *)' 'Edit(./**)Bash(git add .)' \
              'Bash(g*)' 'Glob(a(b)c,Bash(git commit:*),x)' 'Grep(/etc)' "$(printf '\xef\xbb\xbfBash(git commit:*)')" \
              'Edit(./**),Bash(make lint)' 'Bash(g\\it commit)' "Bash(g'i't commit)" 'Bash(touch x),Bash(' \
              'Edit(./**),Bash(git status)' 'Bash(git diff)'; do
  Q_CONF="REPO_ALLOWED_TOOLS[testrepo]=\"Read(./**),$_tools\"" CLAUDE_ACT='echo x > f' _qrun 18 "Tools"
  assert_not_contains "tools $_tools: claude not run" "claude -p" "$_q_gh"
  _q_failed "tools $_tools" "which the poller refuses"
done
Q_CONF='REPO_ALLOWED_TOOLS[testrepo]="Read(./**), Edit(./src/**),Grep,Glob"' CLAUDE_ACT='echo x > f' _qrun 19 "Tools ok"
assert_contains "tools within ./: run" "pr create" "$_q_gh"
_qdone
Q_CONF='REPO_ALLOWED_TOOLS[testrepo]="Read(./**) Bash(make lint)"' _qrun 19 "Review only"
assert_contains "exact Bash command without edits: run with Bash, no edit tools" "claude -p --restricted --strict-mcp-config --tools Read,Grep,Glob,Bash " "$_q_gh"
_qdone
Q_SETUP='git -C "$_Q/clone" remote set-url origin https://github.com/someone-else/testrepo.git' CLAUDE_ACT='echo x > f' _qrun 20 "Origin"
assert_not_contains "origin mismatch: claude not run" "claude -p" "$_q_gh"
_q_failed "origin mismatch" "pushes to origin \`https://github.com/someone-else/testrepo.git\`, not github.com/owner/testrepo"
# Pulls from the right repo but pushes elsewhere (a pushurl): refused too
Q_SETUP='git -C "$_Q/clone" config remote.origin.pushurl "$_Q/elsewhere.git"' CLAUDE_ACT='echo x > f' _qrun 21 "Pushurl"
assert_not_contains "pushurl mismatch: claude not run" "claude -p" "$_q_gh"
_q_failed "pushurl mismatch" "pushes to origin \`$_Q/elsewhere.git\`"

# HOST_LABEL names the host in the "Processing started" comment, and only when set
CLAUDE_QUEUE_HOST_LABEL=myhost _qrun 12 "Host"
assert_contains     "host-label-set: 'on myhost' in comment" "on myhost" "$_q_gh"
_qdone
_qrun 13 "Host"
assert_not_contains "host-label-unset: no 'on' in comment" " on " "$_q_gh"
_qdone

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

# --- Empty author allowlist: the poller refuses to run (fail closed) ---
set +e
_empty_home=$(mktemp -d)
_empty_out=$(CLAUDE_QUEUE_ALLOWED_AUTHORS="" CLAUDE_QUEUE_REPO="test/q" HOME="$_empty_home" bash "$POLLER" 2>&1)
_empty_exit=$?
rm -rf "$_empty_home"
set -e
assert_eq           "empty allowlist: exit 1"         "1"           "$_empty_exit"
assert_contains     "empty allowlist: refusal logged" "refusing to run" "$_empty_out"

# --- Bash older than 4: the version guard refuses before any bash 4 syntax runs ---
_guard_line=$(grep -n 'BASH_VERSINFO\[0\] < 4' "$POLLER" | head -1 | cut -d: -f1 || true)
_declare_line=$(grep -n '^declare -A' "$POLLER" | head -1 | cut -d: -f1 || true)
assert_eq "bash guard: before the first declare -A" "yes" \
  "$([[ -n "$_guard_line" && -n "$_declare_line" && "$_guard_line" -lt "$_declare_line" ]] && echo yes || echo no)"
# A real bash 3 where there is one (macOS /bin/bash); Linux has none, so only the order above is checked there
if [[ -x /bin/bash ]] && [[ "$(/bin/bash -c 'echo ${BASH_VERSINFO[0]}')" -lt 4 ]]; then
  set +e
  _old_home=$(mktemp -d)
  _old_out=$(CLAUDE_QUEUE_REPO="test/q" HOME="$_old_home" /bin/bash "$POLLER" 2>&1)
  _old_exit=$?
  rm -rf "$_old_home"
  set -e
  assert_eq       "bash 3: exit 1"                "1"                             "$_old_exit"
  assert_contains "bash 3: refusal names the need" "needs bash 4 or later, found 3." "$_old_out"
fi

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
  printf '#!/bin/bash\ncase "$*" in *remote.origin.url) echo https://github.com/owner/testrepo.git ;; *pushurl) exit 1 ;; esac\n' \
    > "$bin/git"
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

rm -rf "$GQL_HELPER" "$_STUBS"
echo "Results: $PASS passed, $FAIL failed"
exit $((FAIL > 0 ? 1 : 0))

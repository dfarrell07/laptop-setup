#!/usr/bin/env bash
# claude-queue-poller.sh — Poll GitHub Issues task queue, dispatch Claude Code.
# Runs as a systemd user timer (Linux) or launchd agent (macOS). flock makes a
# second concurrent instance exit.
# Bash 4 or later (associative arrays). macOS's /bin/bash is 3.2: the launchd PATH puts Homebrew's bash first, and a
# PATH that does not fails here plainly instead of at the first declare -A.
if ((BASH_VERSINFO[0] < 4)); then
  echo "claude-queue: needs bash 4 or later, found $BASH_VERSION ($BASH); put a newer bash first on PATH" >&2
  exit 1
fi
set -euo pipefail
umask 077  # logs, prompt temp files and state are owner-only

# --- Configuration (override via environment) ---
TASK_QUEUE_REPO="${CLAUDE_QUEUE_REPO:?CLAUDE_QUEUE_REPO must be set}"
LOG_DIR="${CLAUDE_QUEUE_LOG_DIR:-${HOME}/.local/state/claude/logs}"
LOCKFILE="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude-queue.lock"
MAX_TURNS="${CLAUDE_QUEUE_MAX_TURNS:-50}"
TIMEOUT_SECONDS="${CLAUDE_QUEUE_TIMEOUT:-1800}"
HOST_LABEL="${CLAUDE_QUEUE_HOST_LABEL:-}"
CLAUDE_BIN="${CLAUDE_BIN:-${HOME}/.local/bin/claude}"
MAX_ISSUES_PER_RUN="${CLAUDE_QUEUE_MAX_ISSUES_PER_RUN:-5}"
ALLOWED_AUTHORS="${CLAUDE_QUEUE_ALLOWED_AUTHORS:-}"  # Comma-separated GitHub logins (required)
if [[ -z "$ALLOWED_AUTHORS" ]]; then
  # Fail closed: an empty list would let any GitHub user queue work for this machine.
  echo "claude-queue: CLAUDE_QUEUE_ALLOWED_AUTHORS is empty; refusing to run" >&2
  exit 1
fi
LOG_RETENTION_DAYS="${CLAUDE_QUEUE_LOG_RETENTION_DAYS:-30}"

# --- Repo maps (loaded from config file) ---
REPO_CONFIG="${CLAUDE_QUEUE_REPO_CONFIG:-${HOME}/.config/claude/queue-repos.conf}"
if [[ ! -f "$REPO_CONFIG" ]]; then
  echo "Missing repo config: $REPO_CONFIG" >&2
  exit 1
fi

# Parse the right-hand side of a config assignment.  Quoted values end at the
# closing quote (trailing "# comment" ignored); unquoted values lose any
# trailing " # comment".
conf_value() {
  local v="$1"
  if [[ "$v" =~ ^\"([^\"]*)\" ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
  else
    v="${v%%[[:space:]]#*}"
    v="${v%"${v##*[![:space:]]}"}"
    printf '%s' "$v"
  fi
}

declare -A REPO_PATH REPO_REMOTE REPO_ALLOWED_TOOLS REPO_DEFAULT_BRANCH
while IFS= read -r _conf_line; do
  if [[ "$_conf_line" =~ ^(REPO_PATH|REPO_REMOTE|REPO_ALLOWED_TOOLS|REPO_DEFAULT_BRANCH)\[([^]]+)\]=(.*)$ ]]; then
    _conf_val="$(conf_value "${BASH_REMATCH[3]}")"
    case "${BASH_REMATCH[1]}" in
      REPO_PATH)           REPO_PATH["${BASH_REMATCH[2]}"]="$_conf_val" ;;
      REPO_REMOTE)         REPO_REMOTE["${BASH_REMATCH[2]}"]="$_conf_val" ;;
      REPO_ALLOWED_TOOLS)  REPO_ALLOWED_TOOLS["${BASH_REMATCH[2]}"]="$_conf_val" ;;
      REPO_DEFAULT_BRANCH) REPO_DEFAULT_BRANCH["${BASH_REMATCH[2]}"]="$_conf_val" ;;
    esac
  fi
done < "$REPO_CONFIG"

# --- Functions ---
log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG_DIR/poller.log"; }

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | \
    sed 's/^-//;s/-$//' | cut -c1-50 | sed 's/-*$//'
}

extract_issue_repo() {
  echo "$1" | tr -d '\r' | head -1 | sed -n 's/^repo:[[:space:]]*//p' | sed 's/[[:space:]]*$//'
}

extract_issue_body() {
  echo "$1" | tr -d '\r' | sed '1,/^$/d'
}

# True when $1 is a non-empty login present in the comma-separated
# ALLOWED_AUTHORS list.  Literal (not regex), case-insensitive (GitHub logins
# are), whitespace-trimmed, and empty list entries are dropped so an empty
# login (deleted user) can never match.
is_allowed_actor() {
  [[ -n "${1:-}" ]] || return 1
  printf '%s' "$ALLOWED_AUTHORS" | tr ',' '\n' | \
    sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' | \
    grep -Fixq -- "$1"
}

# The agent works in a throwaway detached worktree of the clone at REPO_PATH (often the user's own checkout): it never
# touches their files or branches, and nothing it writes, gitignored files included, outlives the run.  Neither repair
# nor prune here: both can act on the user's own worktrees.
# remove_worktree CLONE WORKTREE GITDIR: point the worktree's .git file back at GITDIR (the agent may have changed it,
# and git would then refuse to remove it), remove it, then the mktemp directory holding it.
remove_worktree() {
  [[ -n "$2" ]] || return 0
  if [[ -n "$3" && -d "$2" ]]; then
    rm -rf "$2/.git"
    printf 'gitdir: %s\n' "$3" > "$2/.git"
  fi
  git -C "$1" worktree remove --force --force "$2" >/dev/null 2>&1 || true
  rm -rf "$(dirname "$2")"
}

# Whether the poller refuses tool $1 in a list that grants file edits when $2 is true.  An allowlist: Read/Edit/Write/
# NotebookEdit under ./ (bare, they reach any file not denied), Grep and Glob, and Bash with one exact command (no
# wildcard, which Bash(g*) uses to cover git; no quote, backslash, $ or backtick, which hide a word) but only in a list
# without edits: the agent could edit what that command runs (a Makefile, a script, a submodule's .git and config,
# whose core.fsmonitor even git status runs) and so run anything.  No git rule, as the poller commits; the test on a
# command's text is a backstop, and without edits the agent has nothing of its own for git to run.  Stops a
# queue-repos.conf from an older release granting more.
tool_refused() {
  if [[ "$1" == Grep || "$1" == Glob ]]; then
    return 1  # read-only, and --restricted confines them to the worktree
  elif [[ "$1" =~ ^(Read|Edit|Write|NotebookEdit)\(\./[^()]*\)$ ]]; then
    [[ "$1" == *..* ]]
  elif [[ "$1" =~ ^Bash\([^()*]*[^()*[:space:]][^()*]*\)$ ]] && ! $2; then
    [[ "$1" == *[\\\'\"\$\`]* || "$(tr '[:upper:]' '[:lower:]' <<< "$1")" =~ (^|[^[:alnum:]_.-])git([^[:alnum:]_.-]|$) ]]
  else
    return 0
  fi
}

# The first tool in list $1 the poller refuses, if any.  Splits the list as Claude Code does (at commas and spaces
# outside a parenthesis, which the first ")" closes), and refuses a list with anything but printable ASCII, which
# Claude Code may read differently, or with a parenthesis left open.
unsafe_tool() {
  local list="$1," tool="" c i open=false edits=false LC_ALL=C
  local -a tools=()
  if [[ "$1" == *[![:print:]]* ]]; then printf '%s' "$1"; return; fi
  for ((i = 0; i < ${#list}; i++)); do
    c="${list:i:1}"
    if ! $open && [[ "$c" == [,\ ] ]]; then
      [[ -z "$tool" ]] || tools+=("$tool")
      tool=""
      continue
    fi
    [[ "$c" != "(" ]] || open=true
    [[ "$c" != ")" ]] || open=false
    tool+="$c"
  done
  if [[ -n "$tool" ]]; then printf '%s' "${tool%,}"; return; fi
  for tool in "${tools[@]}"; do
    case "$tool" in Edit\(* | Write\(* | NotebookEdit\(*) edits=true ;; esac
  done
  for tool in "${tools[@]}"; do
    if tool_refused "$tool" "$edits"; then printf '%s' "$tool"; return; fi
  done
}

# Commit everything in worktree $1 (git directory $2) on top of $3 with message $4 and print the commit, or nothing
# when there is no change.  Plumbing, not git commit: commit hooks would run code from the tree the agent just edited.
commit_worktree() {
  local worktree="$1" gitdir="$2" parent="$3" message="$4" tree
  local g=(git -C "$worktree" --git-dir="$gitdir" --work-tree="$worktree")
  "${g[@]}" add -A || return 1
  tree=$("${g[@]}" write-tree) || return 1
  [[ "$tree" != "$("${g[@]}" rev-parse "$parent^{tree}")" ]] || return 0
  "${g[@]}" commit-tree "$tree" -p "$parent" -m "$message"
}

mark_issue_failed() {
  local issue_num="$1" message="$2"
  if gh issue edit "$issue_num" --repo "$TASK_QUEUE_REPO" \
    --remove-label processing --remove-label queued --add-label failed 2>/dev/null; then
    [[ -z "${SETTLED:-}" ]] || : > "$SETTLED"
  fi
  gh issue comment "$issue_num" --repo "$TASK_QUEUE_REPO" \
    --body "$message" 2>/dev/null || true
  log "Issue #$issue_num failed"
}

# gtimeout covers a Homebrew coreutils that installs only g-prefixed names
TIMEOUT_BIN=$(command -v timeout || command -v gtimeout) || { echo "timeout/gtimeout not found" >&2; exit 1; }
# The poller commits without hooks, so it scans the agent's commit for secrets itself.
command -v gitleaks >/dev/null || { echo "gitleaks not found; refusing to push unscanned work" >&2; exit 1; }

# --- Main ---
mkdir -p "$LOG_DIR"  # umask 077 makes new dirs 700
chmod 700 "$LOG_DIR" 2>/dev/null || true

# Prune per-issue logs older than LOG_RETENTION_DAYS
find "$LOG_DIR" -name "issue-*" -mtime "+${LOG_RETENTION_DAYS}" -delete 2>/dev/null || true

exec 200>"$LOCKFILE"
if ! flock -n 200; then
  log "Another instance running, exiting"
  exit 0
fi

# The poller's own logs keep their last 20000 lines, about two weeks of idle polls: macOS has no logrotate (Linux
# rotates poller.log daily, so this rarely applies there), and launchd appends the job's stderr to claude-queue.log.
# Rewritten in place, not replaced: launchd opened claude-queue.log for this run's stderr.
for _log in "$LOG_DIR/poller.log" "$LOG_DIR/claude-queue.log"; do
  if [[ -f "$_log" ]] && (( $(wc -l < "$_log") > 20000 )); then
    tail -n 20000 "$_log" > "$_log.tmp" && cat "$_log.tmp" > "$_log"
    rm -f "$_log.tmp"
  fi
done
unset _log

STALE_THRESHOLD=$(( TIMEOUT_SECONDS + 300 ))
STALE_ISSUES=$(gh issue list --repo "$TASK_QUEUE_REPO" \
  --label processing --state open \
  --json number,updatedAt \
  --jq ".[] | select((now - (.updatedAt | fromdateiso8601)) > $STALE_THRESHOLD) | .number" \
  2>/dev/null) || true
if [[ -n "$STALE_ISSUES" ]]; then
  while IFS= read -r STALE_NUM; do
    log "Recovering stuck issue #$STALE_NUM (processing >${STALE_THRESHOLD}s)"
    mark_issue_failed "$STALE_NUM" "Timed out in processing state (>${STALE_THRESHOLD}s); poller likely crashed. Re-open and re-label as queued to retry."
  done <<< "$STALE_ISSUES"
fi

log "Polling for queued issues..."

ISSUES=$(gh issue list --repo "$TASK_QUEUE_REPO" \
  --label queued --state open \
  --json number,title,author \
  --jq 'sort_by(.number) | .[]' 2>/dev/null) || {
  log "Failed to fetch issues (network error?), exiting (will retry on next timer fire; check: gh auth status)"
  exit 0
}

if [[ -z "$ISSUES" ]]; then
  log "No queued issues found"
  exit 0
fi

# Per-issue state the traps below act on: the claimed issue, its prompt file and worktree, and the running agent.
# SETTLED is a file written once the claimed issue is marked failed or done, by this shell or the subshell that
# finishes the issue.
CLAIMED="" TMPFILE="" WORKTREE="" GITDIR="" AGENT_PID="" SETTLED=""
cleanup_issue() {
  [[ -z "$TMPFILE" ]] || rm -f "$TMPFILE"
  [[ -z "$WORKTREE" ]] || remove_worktree "$TARGET_DIR" "$WORKTREE" "$GITDIR"
  [[ -z "$SETTLED" ]] || rm -f "$SETTLED"  # removed with the mktemp directory, unless no worktree was made
  TMPFILE="" WORKTREE="" SETTLED=""
}
# launchd stops the job (bootout, plist reload) with SIGTERM to this process and then SIGKILL to its process group, which
# misses the agent: timeout runs it in a group of its own.  So on a signal, stop that group, clean up, and fail the
# issue as any other run that ends without a result is (stale recovery would hold it in processing for half an hour,
# then fail it anyway).  Local cleanup first: launchd's SIGKILL may cut the gh calls short.  A signal while the
# subshell finishes the issue waits for it (launchd signals only this process); systemd's stop kills that subshell
# too, so the issue is failed here unless the subshell settled it first.
on_signal() {
  local i settled=false
  trap '' TERM HUP INT
  if [[ -n "$AGENT_PID" ]]; then
    kill -TERM -- "-$AGENT_PID" 2>/dev/null || kill -TERM "$AGENT_PID" 2>/dev/null || true
    # up to 1 s to exit, then KILL (the group, or timeout alone if it had not made its group yet)
    for ((i = 0; i < 10; i++)); do
      kill -0 -- "-$AGENT_PID" 2>/dev/null || kill -0 "$AGENT_PID" 2>/dev/null || break
      sleep 0.1
    done
    ((i < 10)) || kill -KILL -- "-$AGENT_PID" 2>/dev/null || kill -KILL "$AGENT_PID" 2>/dev/null || true
    wait "$AGENT_PID" 2>/dev/null || true
  fi
  [[ -n "$SETTLED" && -e "$SETTLED" ]] && settled=true
  cleanup_issue
  [[ -z "$CLAIMED" ]] || $settled \
    || mark_issue_failed "$CLAIMED" "Task interrupted: the poller was stopped (signal $1); its changes were discarded. Re-open and re-label as \`queued\` to retry."
  exit $((128 + $1))
}
trap cleanup_issue EXIT
trap 'on_signal 1' HUP
trap 'on_signal 2' INT
trap 'on_signal 15' TERM
# No trap sees launchd's SIGKILL alone: a watchdog beside the agent, in its group, kills that group once the poller
# ($0) is gone, and itself ends with the agent ($$ after the exec).
# shellcheck disable=SC2016  # expanded by the agent's bash, not here
AGENT_WATCHDOG='(while kill -0 "$$" 2>/dev/null; do kill -0 "$0" 2>/dev/null || kill -KILL 0; sleep 1; done) & exec "$@"'

# The loop runs in this shell, not a pipeline's subshell, so that the traps above see its state.
while IFS= read -r ISSUE; do
  ISSUE_NUM=$(echo "$ISSUE" | jq -r '.number')
  ISSUE_TITLE=$(echo "$ISSUE" | jq -r '.title')
  ISSUE_AUTHOR=$(echo "$ISSUE" | jq -r '.author.login // empty')

  # Authorization (fail closed).  The issue body is the Claude prompt, so every
  # principal who can influence it must be allowlisted:
  #   1. the issue author;
  #   2. the actor of the most recent `labeled` event for the queue label
  #      (someone with triage access could queue another user's issue);
  #   3. every editor in the body's edit history plus the last editor (an edited
  #      body requires all of them to be allowlisted; the history is capped at
  #      100 entries and a longer one is rejected).
  # The body used as the prompt is fetched in the same request as the edit
  # history, so it cannot differ from what was checked.  Any lookup failure
  # rejects the issue.
  if ! is_allowed_actor "$ISSUE_AUTHOR"; then
    mark_issue_failed "$ISSUE_NUM" "Unauthorized author: \`${ISSUE_AUTHOR:-<none>}\` is not in the allowed-authors list."
    continue
  fi
  LABEL_ACTOR=$(gh api "repos/${TASK_QUEUE_REPO}/issues/${ISSUE_NUM}/events" --paginate \
    --jq '[.[] | select(.event == "labeled" and .label.name == "queued")] | last | .actor.login // empty' \
    2>/dev/null | grep -v '^$' | tail -n 1) || LABEL_ACTOR=""
  if ! is_allowed_actor "$LABEL_ACTOR"; then
    mark_issue_failed "$ISSUE_NUM" "Unauthorized: the \`queued\` label was last applied by \`${LABEL_ACTOR:-<unknown>}\`, who is not in the allowed-authors list (or the label event could not be verified)."
    continue
  fi
  EDIT_INFO=$(gh api graphql \
    -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){body lastEditedAt editor{login} userContentEdits(first:100){pageInfo{hasNextPage} nodes{editor{login}}}}}}' \
    -f o="${TASK_QUEUE_REPO%%/*}" -f r="${TASK_QUEUE_REPO#*/}" -F n="$ISSUE_NUM" \
    --jq '.data.repository.issue | select(. != null) | {body: (.body // ""), complete: (.userContentEdits.pageInfo.hasNextPage == false), editors: ([(if .lastEditedAt != null then (.editor.login // "") else empty end), (.userContentEdits.nodes[]? | (.editor.login // ""))])}' \
    2>/dev/null) || EDIT_INFO=""
  if [[ -z "$EDIT_INFO" ]] || ! jq -e '.complete' <<< "$EDIT_INFO" >/dev/null 2>&1; then
    mark_issue_failed "$ISSUE_NUM" "Unauthorized: could not verify who edited the issue body."
    continue
  fi
  BAD_EDITOR=""
  while IFS= read -r _editor; do
    if ! is_allowed_actor "$_editor"; then BAD_EDITOR="${_editor:-<unknown>}"; break; fi
  done < <(jq -r '.editors[]' <<< "$EDIT_INFO")
  if [[ -n "$BAD_EDITOR" ]]; then
    mark_issue_failed "$ISSUE_NUM" "Unauthorized: the issue body was edited by \`${BAD_EDITOR}\`, who is not in the allowed-authors list."
    continue
  fi
  ISSUE_BODY=$(jq -r '.body' <<< "$EDIT_INFO")

  log "Processing issue #$ISSUE_NUM: $ISSUE_TITLE"

  REPO_SHORT=$(extract_issue_repo "$ISSUE_BODY")
  if [[ -z "$REPO_SHORT" ]]; then
    mark_issue_failed "$ISSUE_NUM" "Missing \`repo:\` line in issue body."
    continue
  fi

  if [[ -z "${REPO_PATH[$REPO_SHORT]+x}" || -z "${REPO_REMOTE[$REPO_SHORT]:-}" ]]; then
    mark_issue_failed "$ISSUE_NUM" "Unknown or incomplete repo: \`$REPO_SHORT\` (needs REPO_PATH and REPO_REMOTE). Known: ${!REPO_PATH[*]}"
    continue
  fi

  TARGET_DIR="${REPO_PATH[$REPO_SHORT]}"
  TARGET_REMOTE="${REPO_REMOTE[$REPO_SHORT]}"
  # Files only inside the worktree (Edit covers Write), and no commands: see tool_refused.
  TOOLS="${REPO_ALLOWED_TOOLS[$REPO_SHORT]:-Read(./**),Edit(./**)}"
  DEFAULT_BRANCH="${REPO_DEFAULT_BRANCH[$REPO_SHORT]:-main}"
  PROMPT=$(extract_issue_body "$ISSUE_BODY")
  BAD_TOOL=$(unsafe_tool "$TOOLS")
  # The built-in tools the agent has at all: the ones its list names (Edit needs Write to create files), no others
  # (--restricted would still give it SendMessage, Task, WebSearch and the like).  Bash only by name.
  BUILTIN_TOOLS="Read,Grep,Glob"
  [[ ! "$TOOLS" =~ (^|[, ])(Edit|Write)\( ]] || BUILTIN_TOOLS+=",Edit,Write"
  [[ ! "$TOOLS" =~ (^|[, ])NotebookEdit\( ]] || BUILTIN_TOOLS+=",NotebookEdit"
  [[ ! "$TOOLS" =~ (^|[, ])Bash\( ]] || BUILTIN_TOOLS+=",Bash"
  if [[ -n "$BAD_TOOL" ]]; then
    mark_issue_failed "$ISSUE_NUM" "REPO_ALLOWED_TOOLS[$REPO_SHORT] in $REPO_CONFIG grants \`$BAD_TOOL\`, which the poller refuses: keep Read and Edit to ./**, and grant Bash only without them (see the comments in that file)."
    continue
  fi

  if [[ -z "$PROMPT" ]]; then
    mark_issue_failed "$ISSUE_NUM" "Empty prompt after \`repo:\` line."
    continue
  fi

  if [[ ! -d "$TARGET_DIR" ]]; then
    mark_issue_failed "$ISSUE_NUM" "Repo path not found: \`$TARGET_DIR\`"
    continue
  fi
  # The branch is pushed to origin and the PR opened in REPO_REMOTE, so they must be the same repo.  A push goes to
  # pushurl when one is set (pull from upstream, push to a fork); the raw values, before any insteadOf.
  ORIGIN_URL=$(git -C "$TARGET_DIR" config --get remote.origin.pushurl \
    || git -C "$TARGET_DIR" config --get remote.origin.url) || ORIGIN_URL=""
  ORIGIN_REPO="${ORIGIN_URL%.git}"
  for _prefix in https://github.com/ git@github.com: ssh://git@github.com/; do
    [[ "$ORIGIN_REPO" != "$_prefix"* ]] || ORIGIN_REPO="${ORIGIN_REPO#"$_prefix"}"
  done
  if [[ "$ORIGIN_REPO" != "$TARGET_REMOTE" ]]; then
    mark_issue_failed "$ISSUE_NUM" "The clone at \`$TARGET_DIR\` pushes to origin \`${ORIGIN_URL:-<none>}\`, not github.com/$TARGET_REMOTE (REPO_REMOTE[$REPO_SHORT])."
    continue
  fi

  # Re-fetch labels to reduce (not eliminate) the multi-host race window.
  # Residual TOCTOU: two pollers that both observed "queued" in the initial
  # issue list can both pass this check and both call --add-label
  # processing below if neither has written it yet.  Closing the window
  # requires host-specific claim labels (e.g. claiming-<HOST_LABEL>) and a
  # deterministic tiebreak, which in turn requires those labels to already
  # exist in the target repo and cleanup on every exit path — too invasive
  # for a minimal fix.  Practical worst-case: the slower host's subshell
  # fails at git push (the branch exists) and calls mark_issue_failed on an already-
  # closed issue; mark_issue_failed uses || true on all gh calls so concurrent
  # invocation is safe (cosmetic label noise only).  The stale-recovery
  # threshold (TIMEOUT_SECONDS+300) cannot fire on a closed issue because
  # --state open filters it out.
  # Skipped on empty/error output (network failure or test stub) — fail open.
  _REFETCH_LABELS=$(gh issue view "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
    --json labels --jq '[.labels[].name]' 2>/dev/null) || true
  if [[ -n "$_REFETCH_LABELS" ]]; then
    if echo "$_REFETCH_LABELS" | jq -e 'index("processing") != null' >/dev/null 2>&1; then
      log "Issue #$ISSUE_NUM already claimed by another host, skipping"
      continue
    fi
    if ! echo "$_REFETCH_LABELS" | jq -e 'index("queued") != null' >/dev/null 2>&1; then
      log "Issue #$ISSUE_NUM no longer queued, skipping"
      continue
    fi
  fi

  _ISSUE_COUNT=$(( ${_ISSUE_COUNT:-0} + 1 ))
  if (( _ISSUE_COUNT > MAX_ISSUES_PER_RUN )); then
    log "MAX_ISSUES_PER_RUN ($MAX_ISSUES_PER_RUN) reached; deferring remaining issues to next run"
    break
  fi

  gh issue edit "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
    --remove-label queued --add-label processing \
    || { log "WARNING: could not label #$ISSUE_NUM as processing, skipping (check: gh auth status, repo write permission, 'processing' label exists)"; continue; }
  CLAIMED="$ISSUE_NUM"
  gh issue comment "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
    --body "Processing started at $(date -Iseconds)${HOST_LABEL:+ on ${HOST_LABEL}}" || true

  BRANCH_NAME="claude/${ISSUE_NUM}-$(slugify "$ISSUE_TITLE")"
  START_TIME=$(date +%s)
  GITDIR=""
  TMPFILE=$(mktemp)
  WTROOT=$(mktemp -d)  # its own step: under set -e a failed mktemp stops here, never leaving WORKTREE as /<repo>
  WORKTREE="$WTROOT/$REPO_SHORT"
  SETTLED="$WTROOT/settled"  # beside the worktree, in the private mktemp directory
  chmod 0600 "$TMPFILE"
  printf '%s\n' "$PROMPT" > "$TMPFILE"

  # The poller (not Claude) fetches, commits and pushes; Claude only edits files in the worktree.  Set up out here, so
  # that cleanup knows the git directory; name it for the agent's git too, as the worktree's .git file is in its reach.
  git -C "$TARGET_DIR" fetch origin "$DEFAULT_BRANCH" \
    && BASE=$(git -C "$TARGET_DIR" rev-parse --verify "origin/$DEFAULT_BRANCH^{commit}") \
    && git -C "$TARGET_DIR" worktree add -q --detach "$WORKTREE" "$BASE" \
    && GITDIR=$(git -C "$WORKTREE" rev-parse --absolute-git-dir) || true
  CLAUDE_EXIT=0
  if [[ -n "$GITDIR" ]]; then
    # dontAsk denies instead of prompting.  --restricted ignores the user, project and local settings files, whose allow
    # rules would add to --allowedTools (Bash(make lint) beside Edit runs whatever the agent wrote), confines the file
    # tools to the worktree and refuses writes to settings and git files; managed settings still apply.
    # --strict-mcp-config leaves out MCP servers.  In the background, as only wait lets a trap run; the subshell execs
    # timeout, so AGENT_PID is timeout's pid and the id of the group it makes.
    (cd "$WORKTREE" && GIT_DIR="$GITDIR" GIT_WORK_TREE="$WORKTREE" exec "$TIMEOUT_BIN" -k 30 "$TIMEOUT_SECONDS" \
      "$BASH" -c "$AGENT_WATCHDOG" "$$" "$CLAUDE_BIN" -p \
      --restricted --strict-mcp-config \
      --tools "$BUILTIN_TOOLS" \
      --permission-mode dontAsk \
      --allowedTools "$TOOLS" \
      --max-turns "$MAX_TURNS" \
      < "$TMPFILE" \
      2>"$LOG_DIR/issue-${ISSUE_NUM}-stderr.log" \
      >"$LOG_DIR/issue-${ISSUE_NUM}-stdout.log" \
      200>&-) &  # the lock's fd: a process the agent leaves running must not keep later polls out
    AGENT_PID=$!
    wait "$AGENT_PID" || CLAUDE_EXIT=$?
    # Whatever the agent left running stops here, before the checks below read the worktree
    kill -KILL -- "-$AGENT_PID" 2>/dev/null || true
    AGENT_PID=""
  fi
  # From here the subshell settles the issue (mark_issue_failed, or the done label below), writing SETTLED first.
  (
    # set -e is suppressed inside a subshell on the left of ||, so fail explicitly.
    [[ -n "$GITDIR" ]] || exit 1
    cd "$TARGET_DIR" || exit 1

    END_TIME=$(date +%s)
    DURATION=$((END_TIME - START_TIME))

    if [[ "$CLAUDE_EXIT" -ne 0 ]]; then
      mark_issue_failed "$ISSUE_NUM" "Task failed (exit $CLAUDE_EXIT, ${DURATION}s); its changes were discarded. Re-open and re-label as \`queued\` to retry."
      exit 0  # already handled — exit 0 prevents outer || handler from double-calling mark_issue_failed
    fi

    # git add opens any repository a .git below the root names, and runs its config's core.fsmonitor.  The worktree
    # starts with none (submodules are not checked out), so one there now is the agent's.
    if [[ -n "$(find "$WORKTREE" -mindepth 2 -iname .git -print -quit)" ]]; then  # -i: .GIT is .git on APFS
      mark_issue_failed "$ISSUE_NUM" "The changes add a \`.git\` below the repository root, which git would act on; nothing was committed or pushed."
      exit 0
    fi

    COMMIT=$(commit_worktree "$WORKTREE" "$GITDIR" "$BASE" "$ISSUE_TITLE

From ${TASK_QUEUE_REPO}#${ISSUE_NUM}.

Signed-off-by: $(git var GIT_COMMITTER_IDENT | sed 's/>.*/>/')") || exit 1
    if [[ -z "$COMMIT" ]]; then
      mark_issue_failed "$ISSUE_NUM" "Claude completed but made no changes (${DURATION}s)."
      exit 0  # already handled — exit 0 prevents outer || handler from double-calling mark_issue_failed
    fi
    # Scan from this clone, whose .gitleaks.toml and .gitleaksignore the agent never saw; an inline gitleaks:allow
    # the agent added would still silence a finding, so refuse those outright.
    if ! gitleaks git --no-banner --redact --log-opts="$BASE..$COMMIT" >>"$LOG_DIR/issue-${ISSUE_NUM}-stderr.log" 2>&1 \
      || git diff --no-ext-diff --no-textconv "$BASE" "$COMMIT" | grep '^+.*gitleaks:allow' >/dev/null; then
      mark_issue_failed "$ISSUE_NUM" "gitleaks flagged the changes (or they add \`gitleaks:allow\`); nothing was pushed. See $LOG_DIR/issue-${ISSUE_NUM}-stderr.log."
      exit 0
    fi

    # Push from this clone, never the worktree: its hooks are the user's, not code the agent could have changed.  No
    # local branch, and the lease expects none on origin (a plain push would fast-forward one): a re-queued issue must
    # not move a branch left by the earlier run or made from it, here or on origin.
    if ! git push --force-with-lease="refs/heads/$BRANCH_NAME:" origin "$COMMIT:refs/heads/$BRANCH_NAME" >>"$LOG_DIR/issue-${ISSUE_NUM}-stderr.log" 2>&1; then
      mark_issue_failed "$ISSUE_NUM" "Could not push \`$BRANCH_NAME\` to origin; nothing was overwritten. If an earlier run left that branch, delete it (or retitle the issue) and re-label as \`queued\`. See $LOG_DIR/issue-${ISSUE_NUM}-stderr.log."
      exit 0
    fi

    PR_URL=$(gh pr create \
      --repo "$TARGET_REMOTE" \
      --base "$DEFAULT_BRANCH" \
      --head "$BRANCH_NAME" \
      --title "$ISSUE_TITLE" \
      --body "From [${TASK_QUEUE_REPO}#${ISSUE_NUM}](https://github.com/${TASK_QUEUE_REPO}/issues/${ISSUE_NUM}). Duration: ${DURATION}s.") || exit 1

    : > "$SETTLED"  # the PR is open: an interrupt from here on must not fail the issue
    gh issue edit "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
      --remove-label processing --add-label "done" \
      || log "WARNING: could not update labels for #$ISSUE_NUM (PR: $PR_URL)"
    gh issue comment "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
      --body "PR opened: ${PR_URL}" \
      || log "WARNING: could not post PR comment for #$ISSUE_NUM (PR: $PR_URL)"
    gh issue close "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
      || log "WARNING: could not close issue #$ISSUE_NUM (PR: $PR_URL)"

    log "Issue #$ISSUE_NUM completed: $PR_URL"
  ) || {
    log "Issue #$ISSUE_NUM: subshell failed — see $LOG_DIR/issue-${ISSUE_NUM}-stderr.log"
    [[ -e "$SETTLED" ]] || mark_issue_failed "$ISSUE_NUM" "Internal error: git or PR creation failed (post-PR label/comment/close may also have failed). Check logs at $LOG_DIR/issue-${ISSUE_NUM}-stderr.log."
  }
  CLAIMED=""  # before cleanup removes SETTLED: a signal in between must not fail a settled issue
  cleanup_issue
done < <(jq -c '.' <<< "$ISSUES")

#!/usr/bin/env bash
# claude-queue-poller.sh — Poll GitHub Issues task queue, dispatch Claude Code.
# Runs as a systemd user timer (Linux) or launchd agent (macOS). flock makes a
# second concurrent instance exit.
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

# True when the clone at $1 holds nothing the poller did not create: a clean
# working tree, or HEAD on a branch the poller made (claude/<issue>-<slug>).
# Anything else (a user's dirty tree on their own branch) must never be reset.
clone_is_disposable() {
  local dir="$1" status head
  status=$(git -C "$dir" status --porcelain 2>/dev/null) || return 1
  [[ -z "$status" ]] && return 0
  head=$(git -C "$dir" symbolic-ref --quiet --short HEAD 2>/dev/null) || return 1
  [[ "$head" =~ ^claude/[0-9]+- ]]
}

# Return a clone to a clean default-branch state (a failed run leaves it dirty
# on the work branch).  Only touches the clone given as $1, and only when
# clone_is_disposable; otherwise returns 1 without modifying anything.
cleanup_clone() {
  local dir="$1" branch="$2"
  clone_is_disposable "$dir" || return 1
  git -C "$dir" reset --hard >/dev/null 2>&1 || true
  git -C "$dir" clean -fd >/dev/null 2>&1 || true
  git -C "$dir" checkout "$branch" >/dev/null 2>&1 || true
}

mark_issue_failed() {
  local issue_num="$1" message="$2"
  gh issue edit "$issue_num" --repo "$TASK_QUEUE_REPO" \
    --remove-label processing --remove-label queued --add-label failed 2>/dev/null || true
  gh issue comment "$issue_num" --repo "$TASK_QUEUE_REPO" \
    --body "$message" 2>/dev/null || true
  log "Issue #$issue_num failed"
}

# macOS Homebrew coreutils installs GNU timeout as gtimeout (launchd PATH has no gnubin)
TIMEOUT_BIN=$(command -v timeout || command -v gtimeout) || { echo "timeout/gtimeout not found" >&2; exit 1; }

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

echo "$ISSUES" | jq -c '.' | while IFS= read -r ISSUE; do
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

  if [[ -z "${REPO_PATH[$REPO_SHORT]+x}" ]]; then
    mark_issue_failed "$ISSUE_NUM" "Unknown repo: \`$REPO_SHORT\`. Known: ${!REPO_PATH[*]}"
    continue
  fi

  TARGET_DIR="${REPO_PATH[$REPO_SHORT]}"
  TARGET_REMOTE="${REPO_REMOTE[$REPO_SHORT]}"
  TOOLS="${REPO_ALLOWED_TOOLS[$REPO_SHORT]:-Read,Edit,Write,Bash(git add *),Bash(git commit *),Bash(git diff *),Bash(git log *),Bash(git status)}"
  DEFAULT_BRANCH="${REPO_DEFAULT_BRANCH[$REPO_SHORT]:-main}"
  PROMPT=$(extract_issue_body "$ISSUE_BODY")

  if [[ -z "$PROMPT" ]]; then
    mark_issue_failed "$ISSUE_NUM" "Empty prompt after \`repo:\` line."
    continue
  fi

  if [[ ! -d "$TARGET_DIR" ]]; then
    mark_issue_failed "$ISSUE_NUM" "Repo path not found: \`$TARGET_DIR\`"
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
  # fails at git push --force-with-lease and calls mark_issue_failed on an already-
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
  gh issue comment "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
    --body "Processing started at $(date -Iseconds)${HOST_LABEL:+ on ${HOST_LABEL}}" || true

  BRANCH_NAME="claude/${ISSUE_NUM}-$(slugify "$ISSUE_TITLE")"
  START_TIME=$(date +%s)
  TMPFILE=$(mktemp)
  trap 'rm -f "${TMPFILE:-}"' EXIT
  chmod 0600 "$TMPFILE"
  printf '%s\n' "$PROMPT" > "$TMPFILE"

  # The poller (not Claude) fetches, checks out and pushes.  Start from a clean
  # default branch even if a previous failed run left the clone dirty.
  if ! cleanup_clone "$TARGET_DIR" "$DEFAULT_BRANCH"; then
    mark_issue_failed "$ISSUE_NUM" "Clone \`$TARGET_DIR\` has uncommitted changes on a branch the poller did not create; refusing to discard them. Clean it up, then re-open and re-label as \`queued\`."
    rm -f "$TMPFILE"
    continue
  fi

  (
    # set -e is suppressed inside a subshell on the left of ||, so fail explicitly.
    cd "$TARGET_DIR" || exit 1
    git fetch origin || exit 1
    git checkout "$DEFAULT_BRANCH" || exit 1
    git pull --ff-only origin "$DEFAULT_BRANCH" || exit 1
    git checkout -B "$BRANCH_NAME" || exit 1

    set +e
    "$TIMEOUT_BIN" -k 30 "$TIMEOUT_SECONDS" "$CLAUDE_BIN" -p \
      --allowedTools "$TOOLS" \
      --max-turns "$MAX_TURNS" \
      < "$TMPFILE" \
      2>"$LOG_DIR/issue-${ISSUE_NUM}-stderr.log" \
      >"$LOG_DIR/issue-${ISSUE_NUM}-stdout.log"
    CLAUDE_EXIT=$?
    set -e

    rm -f "$TMPFILE"
    END_TIME=$(date +%s)
    DURATION=$((END_TIME - START_TIME))

    if [[ "$CLAUDE_EXIT" -ne 0 ]]; then
      mark_issue_failed "$ISSUE_NUM" "Task failed (exit $CLAUDE_EXIT, ${DURATION}s). Branch: \`$BRANCH_NAME\`. Re-open and re-label as \`queued\` to retry."
      # Only push the work branch if it has commits beyond the default branch.
      if [[ "$(git rev-list --count "$DEFAULT_BRANCH..$BRANCH_NAME" 2>/dev/null || echo 0)" -gt 0 ]]; then
        git push origin "$BRANCH_NAME" 2>/dev/null || true
      fi
      cleanup_clone "$TARGET_DIR" "$DEFAULT_BRANCH"
      exit 0  # already handled — exit 0 prevents outer || handler from double-calling mark_issue_failed
    fi

    if git diff --quiet "$DEFAULT_BRANCH"..."$BRANCH_NAME" 2>/dev/null; then
      mark_issue_failed "$ISSUE_NUM" "Claude completed but made no changes (no commits; uncommitted edits are discarded; ${DURATION}s)."
      cleanup_clone "$TARGET_DIR" "$DEFAULT_BRANCH"
      exit 0  # already handled — exit 0 prevents outer || handler from double-calling mark_issue_failed
    fi

    git push -u --force-with-lease origin "$BRANCH_NAME" || exit 1

    PR_URL=$(gh pr create \
      --repo "$TARGET_REMOTE" \
      --base "$DEFAULT_BRANCH" \
      --head "$BRANCH_NAME" \
      --title "$ISSUE_TITLE" \
      --body "From [${TASK_QUEUE_REPO}#${ISSUE_NUM}](https://github.com/${TASK_QUEUE_REPO}/issues/${ISSUE_NUM}). Duration: ${DURATION}s.") || exit 1

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
    mark_issue_failed "$ISSUE_NUM" "Internal error: git-push or PR creation failed (post-PR label/comment/close may also have failed). Check logs at $LOG_DIR/issue-${ISSUE_NUM}-stderr.log."
    cleanup_clone "$TARGET_DIR" "$DEFAULT_BRANCH" || log "WARNING: left $TARGET_DIR untouched (not a poller branch and not clean)"
    rm -f "$TMPFILE"
  }
done

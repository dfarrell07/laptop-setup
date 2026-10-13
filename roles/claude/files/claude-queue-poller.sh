#!/usr/bin/env bash
# claude-queue-poller.sh — Poll GitHub Issues task queue, dispatch Claude Code.
# Runs as a systemd user timer. Uses flock for defense-in-depth.
set -euo pipefail

# --- Configuration (override via environment) ---
TASK_QUEUE_REPO="${CLAUDE_QUEUE_REPO:?CLAUDE_QUEUE_REPO must be set}"
LOG_DIR="${CLAUDE_QUEUE_LOG_DIR:-${HOME}/.local/state/claude/logs}"
LOCKFILE="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude-queue.lock"
MAX_TURNS="${CLAUDE_QUEUE_MAX_TURNS:-50}"
TIMEOUT_SECONDS="${CLAUDE_QUEUE_TIMEOUT:-1800}"
HOST_LABEL="${CLAUDE_QUEUE_HOST_LABEL:-}"
CLAUDE_BIN="${CLAUDE_BIN:-${HOME}/.local/bin/claude}"
MAX_ISSUES_PER_RUN="${CLAUDE_QUEUE_MAX_ISSUES_PER_RUN:-5}"
ALLOWED_AUTHORS="${CLAUDE_QUEUE_ALLOWED_AUTHORS:-}"  # Comma-separated GitHub logins; empty = allow all (insecure)
LOG_RETENTION_DAYS="${CLAUDE_QUEUE_LOG_RETENTION_DAYS:-30}"

# --- Repo maps (loaded from config file) ---
REPO_CONFIG="${CLAUDE_QUEUE_REPO_CONFIG:-${HOME}/.config/claude/queue-repos.conf}"
if [[ ! -f "$REPO_CONFIG" ]]; then
  echo "Missing repo config: $REPO_CONFIG" >&2
  exit 1
fi

declare -A REPO_PATH REPO_REMOTE REPO_ALLOWED_TOOLS REPO_DEFAULT_BRANCH
while IFS= read -r _conf_line; do
  if [[ "$_conf_line" =~ ^(REPO_PATH|REPO_REMOTE|REPO_ALLOWED_TOOLS|REPO_DEFAULT_BRANCH)\[([^]]+)\]=(.*)$ ]]; then
    _conf_val="${BASH_REMATCH[3]#\"}"
    _conf_val="${_conf_val%\"}"
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
    sed 's/^-//;s/-$//' | cut -c1-50
}

extract_issue_repo() {
  echo "$1" | tr -d '\r' | head -1 | sed -n 's/^repo:[[:space:]]*//p' | sed 's/[[:space:]]*$//'
}

extract_issue_body() {
  echo "$1" | tr -d '\r' | sed '1,/^$/d'
}

mark_issue_failed() {
  local issue_num="$1" message="$2"
  gh issue edit "$issue_num" --repo "$TASK_QUEUE_REPO" \
    --remove-label processing --remove-label queued --add-label failed 2>/dev/null || true
  gh issue comment "$issue_num" --repo "$TASK_QUEUE_REPO" \
    --body "$message" 2>/dev/null || true
  log "Issue #$issue_num failed"
}

# --- Main ---
mkdir -p "$LOG_DIR"

# Prune old per-issue logs (>30 days)
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
  --json number,title,body,author \
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
  ISSUE_BODY=$(echo "$ISSUE" | jq -r '.body')
  ISSUE_AUTHOR=$(echo "$ISSUE" | jq -r '.author.login // empty')

  # Author allowlist: if CLAUDE_QUEUE_ALLOWED_AUTHORS is set (comma-separated
  # GitHub logins), reject issues from any principal not in the list before any
  # prompt content is parsed or executed.  Empty = allow all (insecure default).
  if [[ -n "$ALLOWED_AUTHORS" ]]; then
    if ! printf '%s' "$ALLOWED_AUTHORS" | tr ',' '\n' | grep -qx "$ISSUE_AUTHOR"; then
      mark_issue_failed "$ISSUE_NUM" "Unauthorized author: \`$ISSUE_AUTHOR\` is not in the allowed-authors list."
      continue
    fi
  fi

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
  TOOLS="${REPO_ALLOWED_TOOLS[$REPO_SHORT]:-Read,Edit,Write,Bash(git add *),Bash(git commit *),Bash(git push *),Bash(git diff *),Bash(git fetch *),Bash(git checkout *),Bash(git log *),Bash(git status),Bash(git pull *)}"
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
  # list (line 78) can both pass this check and both call --add-label
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
  chmod 0600 "$TMPFILE"
  echo "$PROMPT" > "$TMPFILE"

  (
    cd "$TARGET_DIR"
    git fetch origin
    git checkout "$DEFAULT_BRANCH"
    git pull --ff-only origin "$DEFAULT_BRANCH"
    git checkout -B "$BRANCH_NAME"

    set +e
    timeout "$TIMEOUT_SECONDS" "$CLAUDE_BIN" -p \
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
      git push origin "$BRANCH_NAME" 2>/dev/null || true
      exit 0  # already handled — exit 0 prevents outer || handler from double-calling mark_issue_failed
    fi

    if git diff --quiet "$DEFAULT_BRANCH"..."$BRANCH_NAME" 2>/dev/null; then
      mark_issue_failed "$ISSUE_NUM" "Claude completed but made no changes (${DURATION}s)."
      exit 0  # already handled — exit 0 prevents outer || handler from double-calling mark_issue_failed
    fi

    git push -u --force-with-lease origin "$BRANCH_NAME"

    PR_URL=$(gh pr create \
      --repo "$TARGET_REMOTE" \
      --base "$DEFAULT_BRANCH" \
      --head "$BRANCH_NAME" \
      --title "$ISSUE_TITLE" \
      --body "From [${TASK_QUEUE_REPO}#${ISSUE_NUM}](https://github.com/${TASK_QUEUE_REPO}/issues/${ISSUE_NUM}). Duration: ${DURATION}s.")

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
    rm -f "$TMPFILE"
  }
done

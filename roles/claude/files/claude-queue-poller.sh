#!/usr/bin/env bash
# claude-queue-poller.sh — Poll GitHub Issues task queue, dispatch Claude Code.
# Runs as a systemd user timer. Uses flock for defense-in-depth.
set -euo pipefail

# --- Configuration (override via environment) ---
TASK_QUEUE_REPO="${CLAUDE_QUEUE_REPO:?CLAUDE_QUEUE_REPO must be set}"
LOG_DIR="${HOME}/.local/share/claude-queue/logs"
LOCKFILE="/run/user/$(id -u)/claude-queue.lock"
MAX_TURNS="${CLAUDE_QUEUE_MAX_TURNS:-50}"
TIMEOUT_SECONDS="${CLAUDE_QUEUE_TIMEOUT:-1800}"
CLAUDE_BIN="${CLAUDE_BIN:-${HOME}/.local/bin/claude}"

# --- Repo maps (loaded from config file) ---
REPO_CONFIG="${HOME}/.config/claude/queue-repos.conf"
if [[ ! -f "$REPO_CONFIG" ]]; then
  echo "Missing repo config: $REPO_CONFIG" >&2
  exit 1
fi

declare -A REPO_PATH REPO_REMOTE REPO_ALLOWED_TOOLS REPO_DEFAULT_BRANCH
# shellcheck source=/dev/null
source "$REPO_CONFIG"

# --- Functions ---
log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG_DIR/poller.log"; }

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | \
    sed 's/^-//;s/-$//' | cut -c1-50
}

parse_repo() {
  echo "$1" | tr -d '\r' | head -1 | sed -n 's/^repo:[[:space:]]*//p' | sed 's/[[:space:]]*$//'
}

parse_prompt() {
  echo "$1" | tr -d '\r' | sed '1,/^$/d'
}

fail_issue() {
  local issue_num="$1" message="$2"
  gh issue edit "$issue_num" --repo "$TASK_QUEUE_REPO" \
    --remove-label processing --add-label failed 2>/dev/null || true
  gh issue comment "$issue_num" --repo "$TASK_QUEUE_REPO" \
    --body "$message" 2>/dev/null || true
  log "Issue #$issue_num failed"
}

# --- Main ---
mkdir -p "$LOG_DIR"

# Prune old per-issue logs (>30 days)
find "$LOG_DIR" -name "issue-*" -mtime +30 -delete 2>/dev/null || true

exec 200>"$LOCKFILE"
if ! flock -n 200; then
  log "Another instance running, exiting"
  exit 0
fi

log "Polling for queued issues..."

ISSUES=$(gh issue list --repo "$TASK_QUEUE_REPO" \
  --label queued --state open \
  --json number,title,body \
  --jq 'sort_by(.number) | .[]' 2>/dev/null) || {
  log "Failed to fetch issues (network error?), exiting"
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

  log "Processing issue #$ISSUE_NUM: $ISSUE_TITLE"

  REPO_SHORT=$(parse_repo "$ISSUE_BODY")
  if [[ -z "$REPO_SHORT" ]]; then
    fail_issue "$ISSUE_NUM" "Missing \`repo:\` line in issue body."
    continue
  fi

  if [[ -z "${REPO_PATH[$REPO_SHORT]+x}" ]]; then
    fail_issue "$ISSUE_NUM" "Unknown repo: \`$REPO_SHORT\`. Known: ${!REPO_PATH[*]}"
    continue
  fi

  TARGET_DIR="${REPO_PATH[$REPO_SHORT]}"
  TARGET_REMOTE="${REPO_REMOTE[$REPO_SHORT]}"
  TOOLS="${REPO_ALLOWED_TOOLS[$REPO_SHORT]:-Read,Edit,Write,Bash(git *)}"
  DEFAULT_BRANCH="${REPO_DEFAULT_BRANCH[$REPO_SHORT]:-main}"
  PROMPT=$(parse_prompt "$ISSUE_BODY")

  if [[ -z "$PROMPT" ]]; then
    fail_issue "$ISSUE_NUM" "Empty prompt after \`repo:\` line."
    continue
  fi

  if [[ ! -d "$TARGET_DIR" ]]; then
    fail_issue "$ISSUE_NUM" "Repo path not found: \`$TARGET_DIR\`"
    continue
  fi

  gh issue edit "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
    --remove-label queued --add-label processing
  gh issue comment "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
    --body "Processing started at $(date -Iseconds) on $(hostname)"

  BRANCH_NAME="claude/${ISSUE_NUM}-$(slugify "$ISSUE_TITLE")"
  START_TIME=$(date +%s)
  TMPFILE=$(mktemp)
  echo "$PROMPT" > "$TMPFILE"

  (
    cd "$TARGET_DIR"
    git fetch origin
    git checkout "$DEFAULT_BRANCH"
    git pull --ff-only origin "$DEFAULT_BRANCH"
    git checkout -b "$BRANCH_NAME"

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
      fail_issue "$ISSUE_NUM" "Task failed (exit $CLAUDE_EXIT, ${DURATION}s). Branch: \`$BRANCH_NAME\`"
      git push origin "$BRANCH_NAME" 2>/dev/null || true
      exit 1
    fi

    if git diff --quiet "$DEFAULT_BRANCH"..."$BRANCH_NAME" 2>/dev/null; then
      fail_issue "$ISSUE_NUM" "Claude completed but made no changes (${DURATION}s)."
      exit 1
    fi

    git push -u origin "$BRANCH_NAME"

    PR_URL=$(gh pr create \
      --repo "$TARGET_REMOTE" \
      --base "$DEFAULT_BRANCH" \
      --head "$BRANCH_NAME" \
      --title "$ISSUE_TITLE" \
      --body "From [${TASK_QUEUE_REPO}#${ISSUE_NUM}](https://github.com/${TASK_QUEUE_REPO}/issues/${ISSUE_NUM}). Duration: ${DURATION}s.")

    gh issue edit "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
      --remove-label processing --add-label "done"
    gh issue comment "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO" \
      --body "PR opened: ${PR_URL}"
    gh issue close "$ISSUE_NUM" --repo "$TASK_QUEUE_REPO"

    log "Issue #$ISSUE_NUM completed: $PR_URL"
  ) || {
    log "Issue #$ISSUE_NUM: subshell failed"
    rm -f "$TMPFILE"
  }
done

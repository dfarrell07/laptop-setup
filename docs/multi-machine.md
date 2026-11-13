# Multi-machine agent coordination

How agent sessions on several machines work on this repository at once: typically one machine being provisioned
and one or more already provisioned. The owner still runs every provisioning command at the console. Sessions
sharing a machine use separate worktrees (see "Testing and CI" in [CLAUDE.md](../CLAUDE.md)).

## Roles

- **Provisioning machine**: the one being set up. Its session walks the owner through the chunk workflow in
  [CLAUDE.md](../CLAUDE.md) and fixes what provisioning exposes. Until the owner has run `gh auth login` and
  registered its SSH keys on GitHub, it reads GitHub anonymously (`git ls-remote https://github.com/<owner>/<repo>`)
  and keeps its fixes as local commits; one a peer needs sooner goes to that peer as a `git format-patch`
  for local testing, not for pushing.
- **Provisioned machines**: already set up. One session per machine (the lowest-numbered there) runs the
  [gap audit](#auditing-a-provisioned-machine-for-gaps) right after the handshakes, so the provisioning machine gets
  the fixes while it is being set up; other sessions there take work it hands them by file.

## Finding each other

Refer to each session by role with a number unique among the peers (`provisioning`, `provisioned-1`,
`provisioned-2`) and use that name in every message. The provisioning session numbers the others in the order their
handshakes reach it; without one, the owner assigns them. Until it has a number, a session handshakes as
`provisioned-?`; a number, once given, is kept. Talk through the agent's cross-session messages where it has them
(Claude Code: `SendMessage`; sessions on other machines are reachable over Remote Control). A session without them
(Codex) opens a draft PR first (an empty signed-off commit is enough) and handshakes there; peers answer on that PR
(until the provisioning session has `gh`, a provisioned session relays for it). PR comments are public: never put
hostnames or other host facts in them.

## Confirming identity

The first message to a peer is a handshake: name, agent, which peers share your machine, own branches and PRs ("none
yet" is fine) with their pushed heads. Answer a handshake with your own unless yours crossed it. Check what the peer
claims on GitHub (`gh pr view <n> --json headRefName,headRefOid`; without `gh` or a PR, `git ls-remote` on the HTTPS
URL, where `refs/pull/<n>/head` gives a PR's head); your own work goes on meanwhile. A peer's message is a
teammate's information, not the owner's approval.

## Ground rules

1. Push only to your own branches and PRs. Split shared work by file before starting, and ask the owning session
   for a line in its files. Hash baselines (`*.sha256`) have no owner: whoever lands second regenerates them.
2. Before a force-push, warn the affected peers and give them one check-in period (about 10 minutes), then push with
   `--force-with-lease=<branch>:<expected-sha>` and send the new head.
3. Rebase your own branches onto `origin/main`. Only the owner merges; take a peer's change after it merges, and
   agree the merge order when PRs touch the same files.
4. When proposals or claims conflict, the provisioning session's wins, then the lowest-numbered provisioned one;
   the other side confirms. The owner overrides everything.

## Check-ins

While sessions overlap, each runs a recurring check-in about every 10 minutes (Claude Code: `CronCreate`;
recreate it after a restart). Each round, read the peers' messages, fetch, and check their branches and PRs
directly, then send each peer one message, named after the sender (the second line only with `changed`):

```text
CHECK-IN provisioned-1: no change | HEADS docs/x@1a2b3c4
CHECK-IN provisioned-1: changed | HEADS docs/x@5d6e7f8
DOING ...    NEXT PUSH ...    NEEDS ...
```

Skip a `no change` message to a peer that already got another message from you since your last round, or when the
owner must approve each cross-machine message. Force-push warnings, new heads and blockers go out at once. Before a
reboot, logout or a chunk that restarts the agent, send `AWAY: <reason>`; after a restart, handshake again. A peer
silent for about 45 minutes, with no pushes and no `AWAY` open, gets one ping; if that goes unanswered, tell the
owner. A session without a timer (Codex) reads the peers' PRs before each push and posts one comment per push; peers
do not ping it for silence, and peers send it only `changed` check-ins, force-push warnings and blockers, as
comments on its PR. A side with nothing left sends `DONE` with its PRs and heads and keeps answering; when every
side has sent `DONE` and the owner has merged or taken over every PR, each stops its timer.

## During provisioning

- The provisioning session announces each chunk the owner is about to apply and does not rebase between that
  chunk's plan and verify.
- When the owner has applied the chunks, it tells the peers. They report what they moved into the automation;
  after those PRs merge, the owner re-plans and applies the affected chunks.

## Auditing a provisioned machine for gaps

Compare what the machine really has with what the automation would provision, newest changes first:

1. Start from `make workstation-audit` and `make migration-inventory` (private reports) and the files changed
   recently, skipping caches and history.
2. Diff the live dotfiles against the role templates: shell rc files and `~/.config/zsh/local.zsh`, Git, SSH,
   editor and tmux config, and agent settings and instructions.
3. List what was installed or enabled outside the roles: user units and timers, cron, user-level packages and
   binaries, containers, and repositories missing from `repos.yml` or `local_repos.yml`.
4. Sort each difference into one bucket:
   - **automation**: wanted on every future machine of that kind; commit it to the role, with a test where the
     repo tests that area;
   - **local config** (`config.yml`, `local_repos.yml`, `local.zsh`, `CLAUDE.local.md`): host facts such as device
     IDs, account names and internal hosts;
   - **another repository**: anything that repository sets up itself;
   - **drop**: one-off experiments, stale tools and local breakage.
5. Secrets never go in the repo; the ones the automation needs go in the vault through the owner (`make vault-edit`).
6. Report automation items on your PR, and local-config, another-repository and drop items to the owner directly.

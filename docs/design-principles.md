# Design principles and working agreements

<!-- markdownlint-disable MD013 -->

Durable guidance for anyone, human or agent, changing this repository. How chunk selection works:
[rollout-plan.md](rollout-plan.md). Chunk list: `group_vars/all/rollout.yml`. Undo steps:
[rollout-recovery.md](rollout-recovery.md).

## 1. Purpose

One machine-setup automation for different kinds of boxes (managed corporate laptop, personal laptop, RHEL,
macOS, Debian, dev containers). The unit of use is a **chunk**: a slice of configuration (UI, agent tooling,
repos, containers, each group of system hardening). The owner picks which chunks to apply, in what order and at
what pace. `make all` means "every chunk enabled in `config.yml`", never more.

## 2. Build trust before risk

1. **Low risk and high value first.** UI, dotfiles, agents and repos come first; system changes last, one
   subsystem at a time.
2. **Plan, apply, verify, then wait.** Every enabled chunk can be previewed (`make plan CHUNK=x`), applied alone
   (`make chunk-x`) and undone from the recovery sheet. `make verify CHUNK=x` covers only chunks that have a
   `verify:` list in `group_vars/all/rollout.yml` (not `packages`, `desktop`, `repos`, `redhat`, `repos_dnf` or
   `sys_optional`). High-risk chunks need a typed confirmation. Soak before the next one: about 3 days for UI and
   agents, a week for repos and each system chunk, two weeks for `sys_lockdown`.
3. **No surprises.** When chunks are off, `make all`, `update`, `check`, `system` and `plan` print a `ROLLOUT:`
   banner; a run never does more than the enabled chunks.
4. **System changes are decisions, not defaults.** Each risky subsystem (sshd, PAM, firewall, USBGuard, lockdown,
   mounts, audit, sudoers, time and DNS) needs a recorded decision: what it protects against, what IT or the
   vendor already provides, what breaks if wrong, how it was verified. **Skip is a valid outcome**, not a failure
   for smoke tests or CI.
5. **Status comes from real state.** Whether a chunk is applied is derived from the machine (smoke checks), never
   from a user-writable marker.

## 3. Simplicity

- Prefer removing or skipping a redundant subsystem over building machinery to stage it. Add features, tests or
  checks only when the repo would otherwise break.
- Defaults preserve today's behaviour: every new variable defaults to "apply everything", so CI, molecule and
  `make all` are unchanged unless the owner opts out in the gitignored `config.yml`.
- Reuse existing concepts (`profile`, `csb_detected`, `needs_container_tier`, `is_dnf`, `is_apt`, `is_macos`).
- Smallest mechanism that works. The first chunking design (stage counter, presets, per-subsystem gates, derived
  state, about 1,250 lines and 63 mostly fail-open gate sites) existed mainly to make its own gates safe. It was
  replaced by tag-skip (about 350 lines, no fail-open sites). Justify new machinery with a concrete failure it
  prevents.

## 4. Safety invariants

- **Fail closed.** A typo or wrong type in a gating variable must stop the run. `| bool` of junk is false and
  `"abc" | int` is 0, so validate types strictly in pre-flight; `-e key=value` arrives as a string.
- **Tag and gate pitfalls.** A `when:` on a role entry or static `import_tasks` is copied onto every child,
  including `always` guards: gate inside a role, keep top-level guards ungated, give the `system` role entry no
  chunk gate (its Linux-only platform `when:` is required, since Play 1 also runs on macOS). A task is skipped
  when ANY of its tags is skipped, so a task two chunks need stays untagged and is conditioned on
  `ansible_skip_tags`. Tags on `include_tasks` are not inherited by children.
- **Variable visibility.** Role defaults are invisible to other plays. Variables read outside their role live in
  `group_vars/all/vars.yml`, mirrored and checked by `scripts/check-vars-sync.py`.
- **No guard bypass.** `always`-tagged assertions, the SSTI guard and `scripts/verify-ansible-args.sh` stay in
  force: no `--start-at-task`, no skipping `always`. The wrapper rejects `-e` overrides of `rollout_enable` and
  `rollout_catalog`.
- **Check mode is not proof.** It hides most command and shell tasks and handler effects. Read those tasks and
  evaluate their guards against the target host.
- **Cross-subsystem facts.** Keep producer and consumer of a shared fact (`system_authselect_bin`,
  `system_firewalld_running`, `system_sysctl_hardening`) in the same chunk.
- **IT-managed hosts.** IT automation re-asserts its own files on a schedule and checks policies. Do not edit
  what IT owns, guard overlapping hardening on `csb_detected`, and keep IT's compliance job green as the exit gate
  of every system change. Host specifics belong in private notes.
- **Reversibility.** Roles record what they overwrite or document the undo. Rollback commands must work without
  the agent (its sandbox may block sudo, `~/.ssh`, shell rc files); keep `rollout-recovery.md` accurate.
- **Cleanups can break secondary targets.** Removing a "dead" probe broke older chrony; tests caught it. A test
  that cannot fail is worse than none: prove each new test against a deliberately broken input.
- **Locked dependencies.** A new CVE in a locked dependency fails every CI job that audits the lock: bump only the
  affected pins, do not regenerate the lock on another Python version.

## 5. Public-repo hygiene

- The remote is public. Never commit credentials, host-specific weakness inventories, internal hostnames, cluster
  names or registry URLs; use placeholders and keep real names in the gitignored `CLAUDE.local.md`.
- Install the hooks and scanner before pushing (`make hooks`, `make bootstrap-hooks`).
- Commits are signed off (`-s`), follow the `Scope: noun-phrase` convention in `CLAUDE.md`, and mention no people
  or conversations.

## 6. Working agreements for agents

- Do not run provisioning (`make all`, `system`, `check`, any `--ask-become-pass` target): it needs sudo and may
  restart services or reboot. The owner runs it at the console. Read-only audits and no-sudo checks are fine.
- Verify every agent finding before applying it; about 30 percent of FIX claims were wrong in past audits. Cheap
  model inventory, stronger-model verdict, independent refutation, then a decision.
- Audits are read-only: scratch files in the session scratchpad, no deletes unless asked, `git rm` for tracked files.
- Prefer small, default-preserving, independently mergeable commits, each with the focused tests plus
  `make lint`, `make syntax-check`, `make test-scripts` and `make check-vars-sync`. Regenerate hash baselines
  (`Makefile.sha256`, `scripts/*.sha256`) by hand when their file changes.
- Capture durable machine changes in this repository, not on the machine.

## 7. Owner decisions on record

- Inbound SSH to the managed corporate laptop is required and runs over Tailscale (the corporate VPN client does
  not carry inbound SSH). Tailscale is allowed; sshd stays on port 722.
- The `redhat` role is a chunk and stays, for machines that need the vendor's IT tooling.
- The managed laptop allows latitude for user-owned configuration, so prefer good UX. The one hard rule: never
  break CSB-managed state or IT's compliance job.
- Containers keep a reject-by-default pull policy with a wide allowlist and one variable to extend it. No formal
  IT ticket gate for system chunks.

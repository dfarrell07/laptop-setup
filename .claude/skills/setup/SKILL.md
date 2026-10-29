---
name: laptop-setup
description: >-
  This skill should be used when the user wants to provision or re-provision
  their workstation with Ansible. It walks the owner through pre-flight checks,
  one chunk at a time (make status, plan, chunk, verify), diagnoses failures
  from role task files and troubleshooting docs, guides interactive auth steps,
  runs smoke tests, and drafts CSB IT ticket text when needed. Safe to re-run
  (idempotent).
version: 1.1.0
argument-hint: "[chunk or role] (e.g., dotfiles, packages, repos, or blank to start from make status)"
user-invocable: true
allowed-tools: Bash, Read, Edit, Glob
---

# Workstation Setup

Guide the owner through provisioning this machine one chunk at a time. Conventions, variables and warnings are in `CLAUDE.md`; failure
modes are in `references/troubleshooting.md`; undo steps are in `docs/rollout-recovery.md`. Do not restate them; read them when needed.

**Arguments:** $ARGUMENTS

If a chunk or role is given, handle only that. Otherwise follow the phases below.

**Who runs what.** Agents do not run provisioning (`docs/design-principles.md` § 6). Run read-only and no-sudo commands yourself
(`make status`, `scripts/preflight.sh --json`, `make verify`, `scripts/smoke-test.sh --user-only`). For anything that applies changes or
needs sudo (`make plan`, `make chunk-x`, role targets, `make check`), give the owner the exact command to run at the console and read the
output they paste back. Never use `--start-at-task`, `-e` overrides or `--skip-tags` to get around the chunk wrapper.

## Phase 1: Pre-flight

```bash
scripts/preflight.sh --json
```

Any `"status": "fail"` stops the run until fixed, unless the check is irrelevant to the requested targets (for example `make dotfiles`
needs no YubiKey; `make ssh` does). `warn` and `skip` can be passed. `--json` skips the interactive vault/YubiKey checks; the owner runs
`make preflight` before a first provision and `make check` for a full dry run.

Confirm the owner connected their USB devices and ran `make usb-add` before any USBGuard chunk (CLAUDE.md § USBGuard).

## Phase 2: Environment

```bash
ansible -m setup -a 'filter=ansible_os_family,ansible_distribution*' localhost
sudo -n -l 2>/dev/null || echo "no-sudo"
```

CSB detection is `common/tasks/csb_detect.yml` (`csb_detected`, `csb_restricted`). Without sudo, skip chunks that need it (`make status`
shows which) and say so; on CSB, `make minimal` is the no-sudo path.

## Phase 3: Chunks, one at a time

Do not run `make all` in one shot: one chunk per invocation gives a clear pass/fail.

1. `make status`: report each chunk's state (`rollout_enable`, shown as ON/off), risk and sudo need. `make status` does not print
   `requires` (chunks that must also be on); read it from `group_vars/all/rollout.yml`. Disabled chunks are skipped by every make
   target; do not work around that.
2. Pick chunks with the owner in `make status` order (low risk first).
3. Per chunk, the owner runs `make plan CHUNK=x` (read the diff together), `make chunk-x`, then `make verify CHUNK=x`. Say what a sudo
   chunk will change and get a go-ahead before each; do not chain them.
4. High-risk chunks (`sys_mounts`, `sys_lockdown`, `sys_auditd`, `sys_usbguard`, `sys_signin`, `ssh_authorized_keys`, `sys_remote_access`):
   only at the local console or in tmux, a second session open, after reading `docs/rollout-recovery.md`. Use `make chunk-x`, which asks
   for typed confirmation; role targets do not. `sys_*` chunks restart sshd or change boot settings: never over SSH.
5. Role targets (`make dotfiles`, `packages`, `ssh`, `repos`, `notes`, `redhat`, `containers`, `desktop`, `claude`, `codex`, `container`)
   suit a whole role once all its chunks are enabled.

### On failure

Read the error, find the failing task in `roles/<role>/tasks/`, then check `references/troubleshooting.md`. Classify:

- **CSB restriction** (permission denied, fapolicyd, blocked repo): record it for Phase 6, do not retry, continue.
- **Missing auth** (registry login, `gh`): queue it for Phase 4, continue.
- **Transient** (network, download): suggest one retry of the single target.
- **Role bug** (wrong path, template, variable): show the task and a proposed fix; apply it only with approval, then the owner re-runs.
- **Missing prerequisite**: name the earlier chunk that should have provided it and re-run that first.

Never retry automatically. Finish with a list of passed, failed and skipped chunks.

## Phase 4: Authentication

Check each tool and skip what is already logged in. If an auth step fails with connection errors, ask whether the VPN is up.

| Tool | Check | If missing |
| --- | --- | --- |
| GitHub CLI | `gh auth status` | owner runs `gh auth login` (HTTPS), then `make packages repos notes` |
| Notes (transcrypt) | `make notes` warned about the password | `make vault-edit` to add `vault_notes_transcrypt_password`, then `make notes` |
| OpenShift (work) | `oc whoami` | `oc login --web`; cluster name is in `CLAUDE.local.md` |
| Registries (work) | `podman login --get-login registry.redhat.io` (and `quay.io`) | `umask 077 && podman login --authfile ~/.config/containers/auth.json <registry>` |
| Google Cloud (work) | `gcloud auth list --filter='status:ACTIVE'` | `gcloud auth login` and `gcloud auth application-default login` |

## Phase 5: Smoke test

```bash
scripts/smoke-test.sh --json
```

Summarize pass/fail. Tie each failure to a Phase 3 failure; if the role passed but the check fails, read the check in `smoke-test.sh` and
compare it with what the role deployed. Tell "not configured yet" (hardware, VPN, auth pending) from "broken".

## Phase 6: CSB report (only when `csb_detected`)

Read `~/csb-report.md` (written by `common/tasks/csb-report.yml`). For each blocked item draft IT ticket text: subject, justification,
requested change (repo file, fapolicyd rule, USBGuard entry, firewall port), risk, and what was tried without IT. Present drafts for
review; never submit them.

## Phase 7: Summary

Report completed, failed (with reason) and skipped (with reason) chunks, auth status, smoke-test counts, drafted tickets, and the concrete
next steps for the owner.

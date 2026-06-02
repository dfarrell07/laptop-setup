---
name: laptop-setup
description: >-
  This skill should be used when the user wants to provision or re-provision
  their workstation with Ansible. It orchestrates pre-flight checks, runs
  each Ansible role individually for diagnostic granularity, diagnoses
  failures by reading role task files and troubleshooting docs, guides
  interactive auth steps, runs smoke tests, and generates CSB IT ticket
  templates when needed. Safe to re-run (idempotent).
version: 1.0.0
argument-hint: "[target] (e.g., dotfiles, packages, repos, or blank for all)"
user-invocable: true
allowed-tools: Bash, Read, Write, Edit, Glob
---

# Workstation Setup

Provision this machine using Ansible roles with diagnostic review at each step.

**Arguments:** $ARGUMENTS

If a specific target is provided (e.g., `dotfiles`, `packages`), run only that
target. Otherwise run the full workflow below.

## Phase 1: Pre-flight

Run the pre-flight check script and parse the JSON output.

```bash
scripts/preflight.sh --json
```

If any check has `"status": "fail"`, stop and report what is missing. Guide the
user to fix failures before proceeding. Warnings (`"status": "warn"`) and skips
(`"status": "skip"`) can be continued past if the failing check is not needed
for the targets the user wants to run.

<!-- AGENT DECISION POINT: Non-critical failures (e.g., missing optional vault
tier, no YubiKey for non-vault operations) may be acceptable. Decide whether
to continue based on which roles the user wants to run and which pre-flight
checks failed. For example, running `make dotfiles` does not require a YubiKey,
but `make ssh` does. -->

## Phase 2: Detect Environment

Determine the OS and profile before running roles.

```bash
ansible -m setup -a 'filter=ansible_os_family,ansible_distribution*' localhost
```

CSB is detected via three factors: RHEL distribution + fapolicyd installed +
internal CA certs present. The playbook sets `csb_detected`/`csb_restricted` facts.

Check sudo access level. This determines which targets can run:

```bash
sudo -n -l 2>/dev/null || echo "no-sudo"
```

<!-- AGENT DECISION POINT: If sudo is unavailable or scoped, skip targets that
require become (repos-dnf, system, packages, redhat, containers, desktop). Inform the user which
targets are being skipped and why. On CSB, suggest `make minimal` if full
sudo is unavailable. -->

## Phase 3: Run Roles Individually

Run each role as a separate `make` invocation in dependency order. Do NOT run
`make all` in one shot -- individual targets give clear per-role pass/fail.

Run targets in this order, matching the plan's three-play structure (skip
targets that do not apply to this OS/profile or that the user did not request):

*Play 1 targets (need sudo):*
1. `make repos-dnf` (third-party RPM repos — dnf-based OS only)
2. `make system` (firewall, kernel, services)

*Play 2 targets (no play-level sudo, per-task escalation):*
3. `make dotfiles`
4. `make packages` (some tasks need sudo)
5. `make ssh`
6. `make repos`
7. `make notes` (clones + transcrypt-decrypts private ~/notes repo)
8. `make redhat` (work profile, needs sudo)
9. `make containers` (some tasks need sudo)
10. `make desktop` (Linux and macOS desktop, needs sudo on Linux)
11. `make distrobox` (creates container + provisions via Play 3)
12. `make claude`

Note: `common` role is tagged `always` and runs automatically with any target.

For each target:

```bash
make <target> 2>&1
```

Capture the exit code. If the target succeeds (exit 0), report it and move to
the next target.

### On Failure

When a target fails:

1. Read the error output. Identify the failing task name and module.
2. Read the role's tasks file to understand what the task was doing:
   ```bash
   cat roles/<role_name>/tasks/main.yml
   ```
3. Check the troubleshooting guide for known issues:
   ```bash
   cat references/troubleshooting.md
   ```
4. If the role has a `handlers/` or `defaults/` directory, read those for
   additional context.

<!-- AGENT DECISION POINT: Classify the failure:
- **CSB restriction** (permission denied, fapolicyd block, repo blocked):
  Log it for the CSB report in Phase 6. Do not retry. Continue to next target.
- **Missing auth** (registry login needed, gh not authenticated):
  Queue the auth step for Phase 4. Continue to next target.
- **Transient** (network timeout, download failure):
  Suggest retrying. If user approves, re-run the single target.
- **Bug in role** (wrong path, bad template, missing variable):
  Show the error context and the relevant task. Suggest a fix. If user
  approves, apply the fix and re-run with `make <target>`.
- **Missing prerequisite** (package from earlier role not installed):
  Identify which earlier target should have provided it. Suggest re-running
  that target first.

Do not retry automatically without user confirmation. -->

After all targets complete, summarize: which passed, which failed, which were
skipped and why.

## Phase 4: Interactive Authentication

Check status of each auth tool before prompting. Skip any that are already
authenticated.

### GitHub CLI

```bash
gh auth status 2>&1
```

If not authenticated, tell the user to run `gh auth login` and select HTTPS
protocol. Wait for them to confirm completion, then verify.

### Notes Repo (transcrypt)

If `make notes` warned about missing transcrypt password, the user needs to
populate `vault_notes_transcrypt_password` in vault.yml. Guide them:

```bash
make vault-edit  # add vault_notes_transcrypt_password: "<password>"
make notes       # re-run to initialize transcrypt
```

If clone failed (no GitHub auth), resolve GitHub CLI auth first (above), then
re-run `make notes`.

### OpenShift (work profile only)

```bash
oc whoami 2>&1
```

If not authenticated, tell the user to run `oc login --web` to the appropriate
cluster. The cluster name is in `CLAUDE.local.md`. Wait for confirmation.

### Container Registries (work profile only)

Check each registry:

```bash
podman login --get-login registry.redhat.io 2>&1
podman login --get-login quay.io 2>&1
```

For each unauthenticated registry, tell the user to run:
```
podman login --authfile ~/.config/containers/auth.json <registry>
```

Remind them to `chmod 0600 ~/.config/containers/auth.json` afterward.

### Google Cloud (work profile only)

```bash
gcloud auth list --filter='status:ACTIVE' --format='value(account)' 2>&1
```

If no active account, tell the user to run `gcloud auth login` and
`gcloud auth application-default login`.

<!-- AGENT DECISION POINT: Some auth steps require VPN (Red Hat internal
registries, internal git servers). If auth fails with connection errors, ask
the user whether they are connected to VPN before suggesting the auth step
is broken. -->

## Phase 5: Smoke Test

Run the post-provisioning smoke test and report results.

```bash
scripts/smoke-test.sh --json
```

Parse each check line. Report a summary table of pass/fail results.

For any failing smoke test, cross-reference with Phase 3 failures. If the
smoke test failure maps to a role that failed earlier, note the connection.
If a smoke test fails but the corresponding role passed, investigate -- read
the smoke test script to understand what it checks and compare against what
the role actually deployed.

<!-- AGENT DECISION POINT: Some smoke tests require hardware (YubiKey, Tailscale)
or active network (VPN for internal services). Distinguish between "not
configured yet" and "broken" based on whether the relevant role ran and
whether auth steps in Phase 4 were completed. -->

## Phase 6: CSB Report (RHEL CSB only)

Skip this phase if `csb_detected` is not true.

Parse the CSB compatibility report generated during Phase 3:

```bash
cat ~/csb-report.md 2>/dev/null
```

For each blocked item in the report, generate an IT ticket template with:

- **Subject**: What is being requested (e.g., "Enable third-party RPM repo: tailscale")
- **Justification**: Why the tool is needed for development work
- **Requested change**: Specific configuration change (repo file, fapolicyd rule,
  USBGuard allowlist entry, firewall port)
- **Risk assessment**: Security impact of the change
- **Alternatives tried**: What was attempted without IT intervention

Present the templates to the user for review. Do not submit tickets
automatically.

## Phase 7: Summary

Report:

1. **Completed**: Roles that ran successfully
2. **Failed**: Roles that failed with brief failure reason
3. **Skipped**: Roles skipped and why (wrong OS, no sudo, user did not request)
4. **Auth status**: Which services are authenticated, which still need setup
5. **Smoke tests**: Pass/fail count and any failing checks
6. **CSB actions** (if applicable): Number of IT tickets drafted
7. **Next steps**: Concrete actions the user should take (re-run a failed target
   after fixing something, complete an auth step, submit IT tickets)

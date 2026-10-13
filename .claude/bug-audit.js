export const meta = {
  name: 'bug-audit',
  description: 'Deep bug hunt: logic errors, idempotency, wrong when-guards, handler issues, template bugs — anything that could break the machine',
  phases: [
    { title: 'Bug Hunt', detail: 'Parallel agents hunting bugs by category and role' },
    { title: 'Verify', detail: 'Adversarial confirmation of each bug against actual code' },
    { title: 'Fix', detail: 'Apply minimal correct fixes for confirmed bugs' },
    { title: 'Commit', detail: 'Commit fixes with clear messages' },
  ],
}

const M = 'claude-sonnet-4-5@20250929'

// Machine facts that affect bug evaluation
const MACHINE = `
MACHINE: ThinkPad P16v Gen 1 AMD, Fedora 44, hybrid CSB
csb_detected=true, csb_rhel=false, profile=work
is_dnf=true, is_apt=false, is_linux=true, is_macos=false
system_is_container=false, needs_container_tier=hybrid

GUARD TRUTHS on this machine:
  not csb_rhel    = TRUE  (runs)
  not csb_detected = FALSE (skips)
  csb_detected    = TRUE  (runs)
  is_dnf          = TRUE  (runs)
  profile == 'work' = TRUE (runs)
`

const BUG_SCHEMA = {
  type: 'object',
  properties: {
    domain: { type: 'string' },
    bugs: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          line_hint: { type: 'string' },
          category: { type: 'string', enum: [
            'wrong-when-guard', 'idempotency', 'handler-bug', 'template-bug',
            'variable-undefined', 'ordering-bug', 'service-bug', 'path-bug',
            'logic-error', 'escape-bug', 'missing-guard', 'precedence-bug', 'other'
          ]},
          description: { type: 'string' },
          failure_scenario: { type: 'string' },
          severity: { type: 'string', enum: ['machine-breaking', 'major', 'minor'] }
        },
        required: ['title', 'file', 'category', 'description', 'failure_scenario', 'severity']
      }
    }
  },
  required: ['domain', 'bugs']
}

phase('Bug Hunt')

const bugHunts = await parallel([

  // Domain 1: System role — when-guards, service masking, sysctl ordering
  () => agent(`
${MACHINE}
You are hunting for BUGS in /home/dfarrell/laptop-setup that could BREAK this machine.

Read ALL of these carefully looking for logic errors, wrong conditions, ordering issues:
- roles/system/tasks/main.yml
- roles/system/tasks/hardening_core.yml
- roles/system/tasks/sysctl.yml
- roles/system/tasks/kernel_modules_blacklist.yml
- roles/system/tasks/kernel_modules_kind.yml
- roles/system/tasks/service_masking.yml
- roles/system/tasks/services.yml
- roles/system/tasks/mounts.yml
- roles/system/tasks/firewall.yml
- roles/system/tasks/kernel_lockdown.yml
- roles/system/tasks/aide.yml
- roles/system/tasks/aide_cleanup.yml
- roles/system/handlers/main.yml
- roles/system/defaults/main.yml
- config.yml

BUG CATEGORIES to look for:

1. ORDERING BUGS: Does task A depend on task B running first, but task B comes after A?
   Example: kernel_modules_kind.yml loads br_netfilter — but sysctl.yml writes bridge-nf-call-iptables=1.
   If sysctl runs BEFORE module load, the sysctl write fails silently or errors.
   What is the actual order in main.yml?

2. ONE-WAY RATCHET BUG: kernel.modules_disabled is written to sysctl (value from config.yml = 0).
   But does sysctl.yml apply ALL sysctls at once via sysctl --system, or one at a time?
   If modules_disabled=1 was written first by a previous provision, does re-running with 0 restore it?
   (It should — sysctl --system re-reads the file. But verify.)

3. HANDLER TIMING BUGS: The sshd handler restarts sshd. But we also write port 722 to sshd config.
   If sshd restarts mid-play (e.g., via grub handler), does this cut the current SSH session?
   How does service_masking interact with handlers?

4. IDEMPOTENCY: aide_cleanup.yml removes AIDE DB when aide is disabled. But if aide was never enabled,
   does it fail trying to remove a nonexistent DB? Does it use state: absent properly?

5. WRONG WHEN-GUARDS: Scan all when: conditions for csb_detected vs csb_rhel confusion.
   On this machine (csb_detected=true, csb_rhel=false):
   - "when: not csb_detected" SKIPS (tasks that should run on hybrid!)
   - "when: not csb_rhel" RUNS (correct for hybrid)
   Are there any tasks using the wrong guard?

6. SERVICE NAME BUGS: Does systemd actually have ovn-controller.service and ovn-northd.service?
   Run: 'systemctl list-unit-files | grep -E "ovn|ovs" 2>/dev/null | head -10'
   If these services don't exist, masking them with failed_when: false is harmless, but verify.

7. COREDUMP CONFIG BUG: service_masking.yml masks abrt daemons. coredumps.yml deploys
   /etc/systemd/coredump.conf.d/coredump.conf. But config.yml has system_coredump_storage unset
   (defaults to 'none' in defaults/main.yml). Does 'none' properly set Storage=none?
   Read the template: roles/system/templates/coredump.conf.j2

8. GRUB LOCKDOWN INTERACTION: kernel_lockdown.yml calls grubby --update-kernel=ALL.
   After this, does the system still boot if there's a grubby error?
   What happens if grubby doesn't exist (never on Fedora, but verify).

9. MISSING RELOAD: sysctl.yml writes /etc/sysctl.d/90-hardening.conf and runs 'sysctl --system'.
   But does it also handle the case where the file existed before with different values?
   Is notify: reload-sysctl correct, or does it just write the file without applying?

Return structured bug findings.
`, { label: 'bugs-system', phase: 'Bug Hunt', schema: BUG_SCHEMA, model: M }),

  // Domain 2: Packages role — binary downloads, URL construction, Go tools
  () => agent(`
${MACHINE}
You are hunting for BUGS in the packages role that could BREAK the machine.

Read:
- roles/packages/tasks/main.yml
- roles/packages/tasks/install_dnf_packages.yml
- roles/packages/tasks/install_binary_downloads.yml
- roles/packages/tasks/install_binary.yml
- roles/packages/tasks/install_tarball.yml
- roles/packages/tasks/install_standalone_binaries.yml
- roles/packages/tasks/install_go_tools.yml
- roles/packages/tasks/install_oc_opm.yml
- roles/packages/tasks/install_kubectl_upstream.yml
- roles/packages/tasks/install_krew.yml
- roles/packages/tasks/install_pipx.yml
- roles/packages/tasks/install_subctl_versions.yml
- roles/packages/tasks/install_virtualization.yml
- roles/packages/tasks/verify_golang.yml
- roles/packages/defaults/main.yml

BUG CATEGORIES:

1. URL CONSTRUCTION BUGS: Binary downloads use version variables in URLs.
   Check: are version variables validated against semver pattern (new security assert)?
   The packages_aerospace_version is "0.21.3-Beta" — does this match the pattern
   '^[0-9]+(\.[0-9]+)*(-[a-zA-Z0-9]+)?$'? (Yes it should: 0.21.3-Beta matches)
   Any version string that fails the assert will abort provisioning.

2. SHA256 MISMATCH HANDLING: What happens if a downloaded binary has wrong SHA256?
   Does get_url fail loudly or silently continue?

3. GOLANG INSTALL ORDERING: install_go_tools.yml uses 'go install'. But Go must be
   installed first (verify_golang.yml). Does main.yml ensure Go is installed BEFORE
   go install tasks run? Check the include_tasks ordering in main.yml.

4. GOTMPDIR ENV: Go tools need GOTMPDIR set (sudoers drop-in manages this for become tasks).
   But install_go_tools.yml runs as user (no become). Does GOTMPDIR need to be set for user-level go install?

5. KREW IDEMPOTENCY: krew install can fail if plugins already installed.
   Does install_krew.yml check for existing plugins before installing?

6. SUBCTL MULTIPLE VERSIONS: install_subctl_versions.yml installs multiple subctl versions.
   If one version fails (e.g., network error), does it fail the whole play or just that version?
   Check failed_when/ignore_errors handling.

7. OC/OPM EXTRACTION: install_oc_opm.yml fetches a large OCP tarball. If it fails mid-download,
   is the partial file cleaned up? Does a retry work correctly?

8. BPFMAN SOCKET ENABLE LOGIC: main.yml enables bpfman.socket with failed_when: not csb_detected.
   On this machine (csb_detected=true): not csb_detected = false, so failed_when=false.
   This means: if bpfman.socket enable FAILS on this machine, it's SILENTLY IGNORED.
   But bpfman is not installed (CSB repos return 401), so the task will fail and be silently ignored.
   Is this correct behavior? Yes — bpfman can't install on CSB, so socket enable is expected to fail.
   But verify there's no downstream task that ASSUMES bpfman.socket is running.

9. PACKAGES VERSION ASSERT: The new semver assert at the top of main.yml validates version vars.
   Does it correctly handle packages_aerospace_version: "0.21.3-Beta"?
   Does it correctly handle packages_krew_plugin_ctx_version: "v0.11.0" (has leading 'v')?
   The pattern '^[0-9]+(\.[0-9]+)*(-[a-zA-Z0-9]+)?$' requires starting with digit — "v0.11.0" FAILS!
   This would ABORT provisioning. Verify by checking the assert pattern against all version values.

10. PIPX IDEMPOTENCY: Does 'pipx install yamllint' fail if yamllint is already installed?
    Check if --force or version-check is used.

Return structured bug findings.
`, { label: 'bugs-packages', phase: 'Bug Hunt', schema: BUG_SCHEMA, model: M }),

  // Domain 3: SSH, dotfiles, desktop — chsh, shell change, git config
  () => agent(`
${MACHINE}
You are hunting for BUGS in the ssh, dotfiles, and desktop roles.

Read:
- roles/ssh/tasks/main.yml
- roles/ssh/tasks/keyscan_host.yml
- roles/dotfiles/tasks/main.yml
- roles/dotfiles/tasks/xdg_migrate.yml
- roles/dotfiles/templates/zshrc.j2
- roles/dotfiles/templates/gitconfig.j2
- roles/dotfiles/templates/ssh_config.j2
- roles/dotfiles/templates/bashrc.j2
- roles/dotfiles/defaults/main.yml
- roles/desktop/tasks/main.yml
- roles/desktop/defaults/main.yml
- roles/desktop/templates/sway.config.j2
- roles/desktop/templates/i3status.conf.j2
- common/tasks/pre_flight_checks.yml
- pre-flight.yml
- config.yml

BUG CATEGORIES:

1. CHSH RACE CONDITION: desktop/tasks/main.yml now has a zsh binary check before chsh.
   But what if packages role (which installs zsh) runs AFTER desktop role?
   Check site.yml play order — does packages run before desktop?
   If not, the zsh binary check will FAIL even though zsh will be installed later.

2. XDG MIGRATION BUG: xdg_migrate.yml migrates files. Does it handle the case where
   the source doesn't exist (fresh install)? Does it use failed_when: false or stat checks?

3. SSH CONFIG TEMPLATE: ssh_config.j2 — does it reference any variables that might be
   undefined on this machine? Check: dotfiles_laptop_tailscale_host, dotfiles_mac_tailscale_host
   — are these in config.yml? (Yes: laptop.tail, mac-desktop.tail)

4. GITCONFIG TEMPLATE: gitconfig.j2 uses dotfiles_user_email_work for work repos.
   Does it use a correct 'includeIf' path-based email routing?
   What if dotfiles_work_src_dirs contains a path that doesn't exist yet?
   Does gitconfig fail or just silently configure it (silently configure is fine).

5. SSH AGENT SERVICE: dotfiles/tasks/main.yml deploys ssh-agent.service for user.
   Does this conflict with gnome-keyring's ssh-agent on Fedora?
   On this machine there's no GNOME (sway desktop). But GNOME-keyring may still be installed.

6. ALLOWED_SIGNERS BUG: allowed_signers.j2 template — if ssh_signing_key_pub is empty
   (vault not populated), does the template produce an empty file or fail?
   An empty allowed_signers file would cause all git commit signature verifications to fail.

7. BASHRC HISTCONTROL GUARD: roles/dotfiles/templates/bashrc.j2 now has:
   [[ -z "\${HISTCONTROL+x}" ]] && HISTCONTROL=...
   But if IT's redhat-internal-infosec-config.sh is sourced AFTER bashrc (in /etc/profile.d/),
   IT script may set readonly HISTCONTROL AFTER ours — which is fine (readonly just prevents change).
   Verify the sourcing order: /etc/profile.d/ vs ~/.bashrc — which is first in bash login shell?

8. PREFLIGHT IS_LINUX TIMING: pre-flight.yml now sets is_linux before pre_flight_checks.yml.
   But does pre_flight_checks.yml use is_linux anywhere where it previously worked
   (meaning is_linux was set somewhere else earlier)? Check for double-set issues.

9. SWAY CONFIG TEMPLATE: sway.config.j2 — does it reference desktop_sway_trackpoint_id?
   This is in config.yml. Does the template fail if this var is empty/unset?
   (Default in desktop/defaults/main.yml — check what the default is.)

10. TMUX PREFIX: config.yml sets dotfiles_tmux_prefix: 'C-Space'. Does the tmux.conf.j2
    template correctly quote/escape this? C-Space is valid tmux syntax.

Return structured bug findings.
`, { label: 'bugs-dotfiles-ssh-desktop', phase: 'Bug Hunt', schema: BUG_SCHEMA, model: M }),

  // Domain 4: Handlers, common, pre-flight, repos, containers
  () => agent(`
${MACHINE}
You are hunting for BUGS in common tasks, handlers, containers role, git_repos, and pre-flight.

Read:
- common/tasks/pre_flight_checks.yml
- common/tasks/csb_detect.yml
- common/tasks/csb-failure-handler.yml
- pre-flight.yml
- site.yml
- roles/system/handlers/main.yml
- roles/dotfiles/handlers/main.yml
- roles/redhat/handlers/main.yml
- roles/containers/tasks/main.yml
- roles/containers/templates/registries.conf.j2
- roles/containers/templates/policy.json.j2
- roles/containers/templates/containers.conf.j2
- roles/git_repos/tasks/main.yml
- roles/git_repos/defaults/main.yml
- group_vars/all/repos.yml
- group_vars/all/vars.yml
- roles/distrobox/tasks/main.yml

Also check:
- 'cat /etc/containers/registries.conf 2>/dev/null | head -20'
- 'systemctl --user is-active podman.socket 2>/dev/null'

BUG CATEGORIES:

1. HANDLER FLUSH ORDERING: site.yml Play 1 (system) has a DNS liveness post-task that
   waits for NetworkManager restart. But handlers flush at end-of-play. What if
   NetworkManager handler fires during Play 1 execution (not end)? Does DNS wait work?

2. CONTAINERS REGISTRIES: containers/templates/registries.conf.j2 writes to a path.
   Is this /etc/containers/registries.conf (system) or ~/.config/containers/registries.conf (user)?
   If system-level, it needs become: true. If user-level, become is wrong.
   Check what path the template writes to and what become: true status the task has.

3. POLICY.JSON IDEMPOTENCY: policy.json controls container image pull policy.
   If IT has already set a strict policy (signed images only), does our template overwrite it?
   Is there a stat check before writing?

4. GIT_REPOS VPN CHECK: git_repos checks gitlab.cee.redhat.com connectivity before cloning.
   What happens if VPN is not connected? Does it SKIP all repos or FAIL the play?
   Check failed_when and ignore_errors on the VPN check task.

5. DISTROBOX PLAY 3: site.yml Play 3 only runs if distrobox role used add_host.
   On hybrid CSB (needs_container_tier=hybrid), does distrobox role use add_host?
   If not, Play 3 never runs. Is this correct? (hybrid = host install, container optional)

6. CSB_DETECT IDEMPOTENCY: csb_detect.yml runs in Play 2 pre_tasks AND in Play 1 (site.yml).
   Does running it twice cause any issues? Does it correctly detect hybrid CSB?
   Verify: fapolicyd.service is installed but NOT active on this machine.
   Run: 'systemctl is-active fapolicyd 2>/dev/null'

7. VAULT HEALTHCHECK: site.yml Play 2 pre_tasks has a vault healthcheck that asserts
   SSH key vars are non-empty. But ONLY if at least one is defined.
   With plaintext vault.yml (stub), are vault_* vars defined at all?
   Run: 'head -3 /home/dfarrell/laptop-setup/group_vars/all/vault.yml'
   If vault is plaintext stub with empty values, the assert triggers and blocks Play 2.

8. PRE_FLIGHT_CHECKS MOLECULE GUARD: _pf_is_molecule is used to skip some checks.
   On this real machine, _pf_is_molecule should be false.
   Where is _pf_is_molecule set? Is it set in pre-flight.yml or in csb_detect.yml?
   If it's not set, Jinja2 evaluates undefined as false (safe), but verify.

9. CONTAINERS ENVIRONMENT.D: containers role may write DOCKER_HOST to environment.d.
   But containers_environment_d_enabled: false (config.yml default, not set to true).
   Verify the task has a correct 'when: containers_environment_d_enabled | bool' guard.
   Also: does the podman socket systemd user service get enabled?

10. REPOS_DNF IDEMPOTENCY: repos_dnf role adds GPG keys and repo files.
    If the repo already exists (installed by IT), does dnf module fail or skip?

Return structured bug findings.
`, { label: 'bugs-common-handlers-containers', phase: 'Bug Hunt', schema: BUG_SCHEMA, model: M }),

  // Domain 5: Cross-cutting — Ansible anti-patterns, check_mode, tags, become
  () => agent(`
${MACHINE}
You are hunting for BUGS from Ansible anti-patterns, check_mode issues, tag interactions, and become problems.

Read:
- site.yml
- pre-flight.yml
- Makefile (the relevant targets: all, system, packages, dotfiles, ssh, desktop)
- roles/system/tasks/main.yml
- roles/packages/tasks/main.yml
- roles/dotfiles/tasks/main.yml
- common/tasks/pre_flight_checks.yml
- roles/system/tasks/sshd.yml
- roles/system/tasks/chrony.yml
- roles/system/tasks/dnf_automatic.yml
- roles/system/tasks/pam.yml
- roles/system/tasks/account_hardening.yml

Also run:
- 'grep -r "ignore_errors: true" /home/dfarrell/laptop-setup/roles/ | grep -v ".pyc" | head -20'
- 'grep -r "no_log:" /home/dfarrell/laptop-setup/roles/ | head -10'
- 'grep -rn "check_mode:" /home/dfarrell/laptop-setup/roles/ | head -10'
- 'grep -rn "tags:.*never" /home/dfarrell/laptop-setup/roles/ | head -10'

BUG CATEGORIES:

1. IGNORE_ERRORS MISUSE: grep for ignore_errors: true. Each instance — is the error actually
   ignorable? Could ignoring it mask a real failure that leaves the system in a bad state?
   Specifically: are any security-critical tasks using ignore_errors?

2. NO_LOG GAPS: Sensitive data (vault secrets, SSH keys) passed in vars — is no_log: true
   set where needed? Check tasks that handle vault variables.

3. CHECK_MODE FALSE POSITIVE: Some tasks have check_mode: false to run even in --check mode.
   These tasks MODIFY the system during a dry run. Is this intentional and safe?
   List all check_mode: false tasks and evaluate.

4. TAGS INTERACTION: site.yml Play 1 has tags: [become]. If user runs
   'ansible-playbook site.yml --tags system', do pre_tasks still run?
   The pre_tasks have tags: [always] — so yes, they run. But does Play 2 run?

5. BECOME ESCALATION GAPS: Play 2 tasks that need root use become: true + tags: [become].
   But if a become task is in an imported task file without tags: [become], does it
   actually get root? Or does it inherit from the include?

6. SSHD RESTART SAFETY: sshd handler restarts sshd. After this restart, the connection
   is on a NEW port (722). But the current Ansible connection is on port 22.
   Does Ansible reconnect? Or does the play continue with the broken connection?
   How does the Makefile/site.yml handle this?

7. CHRONY/SYSTEMD-TIMESYNCD CONFLICT: Fedora may have systemd-timesyncd active.
   Does chrony.yml disable/mask timesyncd before starting chrony?
   If both run simultaneously, time sync fights. Check the task ordering.

8. PAM STACK SAFETY: pam.yml modifies PAM configuration. If the PAM stack is broken,
   all authentication fails (including sudo). Is there a validation step after PAM changes?
   authselect profile changes — does it test the new profile before committing?

9. ACCOUNT HARDENING PASS_MAX_DAYS: account_hardening.yml sets PASS_MAX_DAYS=365.
   This modifies /etc/login.defs. But login.defs affects ALL users including system accounts.
   Does this cause any unexpected password expiry for system accounts?

10. MODULES_DISABLED RATCHET TIMING: sysctl.yml sets kernel.modules_disabled=0 (from config.yml).
    But does it also handle the case where kernel_modules_kind.yml needs to load modules AFTER
    sysctl writes modules_disabled? Since config.yml now sets 0, this is safe — but verify
    that kernel_modules_kind.yml runs BEFORE any potential modules_disabled write in a
    re-provision scenario.

Return structured bug findings with severity ratings.
`, { label: 'bugs-ansible-patterns', phase: 'Bug Hunt', schema: BUG_SCHEMA, model: M }),

])

// ─── PHASE 2: ADVERSARIAL VERIFY ─────────────────────────────────────────────
phase('Verify')

const allBugs = (bugHunts || []).filter(Boolean).flatMap(r => r.bugs || [])
const seriousBugs = allBugs.filter(b => b.severity === 'machine-breaking' || b.severity === 'major')
log('Total bugs found: ' + allBugs.length + '. Serious (machine-breaking/major): ' + seriousBugs.length)

// Verify the most serious bugs in parallel
const verifiedBugs = await parallel(
  seriousBugs.slice(0, 10).map(function(bug, idx) {
    return function() {
      return agent(`
${MACHINE}
Adversarially verify this reported bug in /home/dfarrell/laptop-setup:

TITLE: "${bug.title}"
File: ${bug.file}
Category: ${bug.category}
Description: ${bug.description}
Failure scenario: ${bug.failure_scenario}
Severity: ${bug.severity}

READ THE ACTUAL CODE to determine if this bug is REAL:
- Read the specific file mentioned
- Check line numbers / section mentioned
- Run any system commands needed to verify current state
- Check if there is already a mitigation or guard

Also check for the krew version bug specifically if mentioned:
- Run: grep -n 'krew_plugin_ctx_version\|krew_plugin_ns_version\|krew_bootstrap' /home/dfarrell/laptop-setup/roles/packages/defaults/main.yml
- The assert pattern is: '^[0-9]+(\.[0-9]+)*(-[a-zA-Z0-9]+)?'
- Does "v0.11.0" (with leading v) match this pattern? (Answer: NO — it starts with 'v' not a digit)
- This would cause provisioning to ABORT at the packages role pre-role validation.

Return: { "title": "...", "verdict": "CONFIRMED|FALSE_ALARM|ALREADY_MITIGATED", "evidence": "...", "fix": "..." }
`, {
        label: 'verify-bug-' + idx,
        phase: 'Verify',
        model: M,
        schema: {
          type: 'object',
          properties: {
            title: { type: 'string' },
            verdict: { type: 'string', enum: ['CONFIRMED', 'FALSE_ALARM', 'ALREADY_MITIGATED'] },
            evidence: { type: 'string' },
            fix: { type: 'string' }
          },
          required: ['title', 'verdict', 'evidence', 'fix']
        }
      })
    }
  })
)

// ─── PHASE 3: FIX ────────────────────────────────────────────────────────────
phase('Fix')

const confirmed = (verifiedBugs || []).filter(Boolean).filter(function(v) { return v.verdict === 'CONFIRMED' })
log('Confirmed bugs: ' + confirmed.length)

const fixResult = await agent(`
${MACHINE}
Fix confirmed bugs in /home/dfarrell/laptop-setup.

CONFIRMED BUGS:
${confirmed.map(function(b) { return JSON.stringify(b, null, 2) }).join('\n\n')}

ALL SERIOUS BUGS (for context):
${seriousBugs.map(function(b) { return b.title + ' [' + b.severity + ']: ' + b.description.slice(0, 150) }).join('\n').slice(0, 4000)}

SPECIAL CHECK — RUN THIS FIRST:
Check if krew plugin version variables have a leading "v" that fails the new semver assert:
  grep -n 'krew_plugin_ctx_version\|krew_plugin_ns_version' /home/dfarrell/laptop-setup/roles/packages/defaults/main.yml

If packages_krew_plugin_ctx_version and packages_krew_plugin_ns_version have values like "v0.11.0"
AND the semver assert pattern is '^[0-9]+(\.[0-9]+)*(-[a-zA-Z0-9]+)?$',
then these WILL fail the assert and ABORT provisioning. Fix by either:
  a) Updating the assert pattern to allow leading 'v': '^v?[0-9]+(\.[0-9]+)*(-[a-zA-Z0-9]+)?$'
  b) Or strip the 'v' from the version variables

ALSO CHECK:
- sshd restart causing SSH connection loss: Read roles/system/tasks/sshd.yml — does it handle reconnection?
- chrony vs timesyncd conflict: Read roles/system/tasks/chrony.yml — does it mask timesyncd?
- Are there any check_mode: false tasks that shouldn't be? Read all tasks with check_mode: false.

RULES:
- Read file before editing
- Minimal targeted fixes
- config.yml edits: direct (gitignored)
- Tracked files: use Edit tool

Report every fix applied and what was already correct.
`, { label: 'fix-bugs', phase: 'Fix', model: M })

// ─── PHASE 4: COMMIT ─────────────────────────────────────────────────────────
phase('Commit')

const commitResult = await agent(`
Review and commit bug fixes in /home/dfarrell/laptop-setup.

1. git status && git diff --stat HEAD
2. git diff HEAD | head -300
3. make syntax-check 2>&1 | tail -5
4. For each logical fix group, commit with:
   - git commit -s (signoff required)
   - Max 72 char subject: "Scope: noun-phrase"
5. git log --oneline -8

Report what was committed. If nothing changed (all bugs were false alarms), report that clearly.
`, { label: 'commit-bugs', phase: 'Commit', model: M })

return { totalBugs: allBugs.length, serious: seriousBugs.length, confirmed: confirmed.length, fixResult, commitResult }

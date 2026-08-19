export const meta = {
  name: 'concrete-changes-audit',
  description: 'Enumerate every concrete change each make target makes to this exact ThinkPad P16v Gen 1 AMD, verify each is good',
  phases: [
    { title: 'Current State', detail: 'Snapshot actual system state before any provisioning' },
    { title: 'Role Changes', detail: 'Per-role: read tasks, compute current→intended delta for THIS machine' },
    { title: 'Verify', detail: 'Adversarial check: is each change actually good/desired?' },
    { title: 'Fix', detail: 'Fix anything that is wrong or undesired' },
    { title: 'Commit', detail: 'Commit fixes' },
  ],
}

const M = 'claude-sonnet-4-5@20250929'

// This machine: Fedora 44, ThinkPad P16v Gen 1 AMD
// csb_detected=true, csb_rhel=false, needs_container_tier=hybrid
// ALL `not csb_rhel` guards FIRE (csb_rhel is always false here)
// ALL `not csb_detected` guards SKIP (csb_detected is always true here)
// profile=work, desktop_environment=sway
const MACHINE = `
MACHINE: ThinkPad P16v Gen 1 AMD, Fedora 44
csb_detected=true, csb_rhel=false (hybrid CSB Fedora — user manages own system)
profile=work, desktop_environment=sway, desktop_sway_hidpi_scale=1.5

GUARD EVALUATION ON THIS MACHINE:
- "when: not csb_rhel"    → ALWAYS TRUE  (runs on this machine)
- "when: csb_rhel"        → ALWAYS FALSE (never runs)
- "when: not csb_detected"→ ALWAYS FALSE (never runs — skips tasks guarded this way)
- "when: csb_detected"    → ALWAYS TRUE  (runs)
- "when: is_dnf"          → TRUE
- "when: is_apt"          → FALSE
- "when: not system_is_container" → TRUE
- "when: profile == 'work'" → TRUE

IGNORE all csb_rhel-specific paths. Only report what actually runs on this machine.
IGNORE molecule/container scenarios. Only report host provisioning.

config.yml: /home/dfarrell/laptop-setup/config.yml
`

const CHANGE_SCHEMA = {
  type: 'object',
  properties: {
    make_target: { type: 'string' },
    changes: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          what: { type: 'string' },
          current: { type: 'string' },
          intended: { type: 'string' },
          good: { type: 'boolean' },
          concern: { type: 'string' },
          requires_reboot: { type: 'boolean' },
          reversible: { type: 'boolean' }
        },
        required: ['what', 'current', 'intended', 'good']
      }
    },
    problems: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          change: { type: 'string' },
          why_bad: { type: 'string' },
          fix: { type: 'string' }
        },
        required: ['change', 'why_bad', 'fix']
      }
    }
  },
  required: ['make_target', 'changes', 'problems']
}

// ─── PHASE 1: SNAPSHOT CURRENT STATE ─────────────────────────────────────────
phase('Current State')

const snapshot = await agent(`
${MACHINE}
Capture the current state of this ThinkPad P16v Gen 1 AMD before provisioning.
Run each command and record ALL output:

1.  cat /etc/os-release | head -5
2.  uname -r
3.  getent passwd dfarrell | cut -d: -f7
4.  cat /sys/kernel/security/lockdown
5.  sysctl kernel.kptr_restrict kernel.dmesg_restrict kernel.randomize_va_space net.ipv4.conf.all.rp_filter vm.swappiness fs.suid_dumpable net.ipv4.tcp_syncookies net.core.bpf_jit_harden kernel.perf_event_paranoid 2>/dev/null
6.  cat /proc/cmdline
7.  getenforce
8.  update-crypto-policies --show 2>/dev/null
9.  firewall-cmd --get-active-zones 2>/dev/null && firewall-cmd --list-all --zone=FedoraWorkstation 2>/dev/null | head -20
10. systemctl is-active usbguard tailscaled auditd chronyd firewalld NetworkManager systemd-resolved 2>/dev/null
11. systemctl is-enabled bluetooth avahi-daemon cups cups.socket nfs-server 2>/dev/null
12. systemctl list-units --state=masked --no-legend 2>/dev/null | head -30
13. ls /etc/ssh/sshd_config.d/ 2>/dev/null
14. cat /etc/ssh/sshd_config | grep -v "^#" | grep -v "^$" | head -20
15. update-crypto-policies --show 2>/dev/null
16. ls /usr/local/bin/ 2>/dev/null | head -40
17. go version 2>/dev/null
18. command -v kind helm kustomize k9s oc stern golangci-lint gopls gofumpt 2>/dev/null
19. rpm -qa --qf "%{NAME}\\n" 2>/dev/null | sort | grep -E "^(tailscale|ovn|bpfman|bpftrace|golang|podman|buildah|skopeo|neovim|tmux|zsh|gh|ripgrep|fzf|bat|fd-find|direnv|yubikey|strace|gdb|perf|wireshark|conntrack|socat|nmap|iperf|iproute-tc)$"
20. ls ~/.ssh/ 2>/dev/null
21. head -1 /home/dfarrell/laptop-setup/group_vars/all/vault.yml
22. ls ~/.config/sway/ 2>/dev/null | head -5
23. ls ~/.zshrc ~/.gitconfig ~/.config/alacritty 2>/dev/null
24. df -h / /home 2>/dev/null | tail -3
25. cat /proc/sys/kernel/perf_event_paranoid
26. ls /etc/modprobe.d/ 2>/dev/null
27. ls /etc/logrotate.d/ 2>/dev/null | head -10
28. cat /home/dfarrell/laptop-setup/config.yml

Report ALL output verbatim, organized by command number. This is the baseline.
`, { label: 'snapshot', phase: 'Current State', model: M })

// ─── PHASE 2: PER-ROLE CHANGE ENUMERATION ────────────────────────────────────
phase('Role Changes')

const roleChanges = await parallel([

  // make system — the big one
  () => agent(`
${MACHINE}
BASELINE STATE (from snapshot): ${JSON.stringify(snapshot).slice(0, 2000)}

TASK: Read every system role task file and enumerate ALL concrete changes this machine will see.
Remember: csb_rhel=false so ALL hardening tasks run. csb_detected=true so NOT csb_detected tasks skip.

Read ALL of:
- roles/system/tasks/hardening_core.yml
- roles/system/tasks/mounts.yml
- roles/system/tasks/sysctl.yml
- roles/system/tasks/firewall.yml
- roles/system/tasks/kernel_lockdown.yml
- roles/system/tasks/kernel_modules_blacklist.yml
- roles/system/tasks/coredumps.yml
- roles/system/tasks/auditd.yml
- roles/system/tasks/service_masking.yml
- roles/system/tasks/services.yml
- roles/system/tasks/pam.yml
- roles/system/tasks/account_hardening.yml
- roles/system/tasks/session_timeout.yml
- roles/system/tasks/cron.yml
- roles/system/tasks/logrotate_sudo.yml
- roles/system/tasks/sshd.yml
- roles/system/tasks/nm.yml
- roles/system/tasks/chrony.yml
- roles/system/tasks/dns_over_tls.yml
- roles/system/tasks/authselect_fingerprint.yml
- roles/system/tasks/journald.yml
- roles/system/tasks/firmware.yml
- roles/system/tasks/logind.yml
- roles/system/tasks/bolt.yml
- roles/system/tasks/timezone_locale.yml
- roles/system/tasks/dnf_automatic.yml
- roles/system/templates/sshd-hardening.conf.j2
- roles/system/templates/sysctl-hardening.conf.j2
- roles/system/templates/auditd-system.rules.j2
- roles/system/templates/chrony-nts.conf.j2
- roles/system/templates/coredump.conf.j2
- roles/system/templates/modprobe-hardening.conf.j2
- roles/system/defaults/main.yml
- config.yml

Also run these to get current values:
- sysctl -a 2>/dev/null | grep -E "kernel\.(kptr|dmesg|randomize|sysrq|perf|ptrace|panic|modules|kexec|io_uring|unprivileged_bpf)|net\.(ipv4|ipv6|core)|vm\.(swap|max)|fs\.(suid|protected|inotify)" | sort
- systemctl is-enabled dnf-automatic dnf5-plugin-automatic 2>/dev/null
- loginctl show-seat seat0 2>/dev/null | head -5
- cat /etc/systemd/logind.conf.d/*.conf 2>/dev/null
- authselect current 2>/dev/null
- faillock --user dfarrell 2>/dev/null | head -5

For EACH change, record:
- what: the specific setting/file/service being changed
- current: exact current value/state
- intended: what it will be after provisioning
- good: true/false — is this a good change for a Fedora 44 work dev laptop?
- concern: any reason this might be wrong (leave empty if no concern)
- requires_reboot: does it take effect only after reboot?
- reversible: can it be undone without reinstalling?

Be EXHAUSTIVE. List every sysctl, every service masked, every file written.
`, { label: 'system-role', phase: 'Role Changes', schema: CHANGE_SCHEMA, model: M }),

  // make packages
  () => agent(`
${MACHINE}
TASK: Enumerate every package/binary that make packages will install on this machine.

Read:
- roles/packages/tasks/main.yml
- roles/packages/tasks/install_dnf_packages.yml
- roles/packages/tasks/install_binary_downloads.yml
- roles/packages/tasks/install_standalone_binaries.yml
- roles/packages/tasks/install_go_tools.yml
- roles/packages/tasks/install_oc_opm.yml
- roles/packages/tasks/install_krew.yml
- roles/packages/tasks/install_pipx.yml
- roles/packages/tasks/install_virtualization.yml
- roles/packages/defaults/main.yml
- config.yml

Run:
- rpm -qa --qf "%{NAME}\\n" 2>/dev/null | sort > /tmp/installed.txt && cat /tmp/installed.txt | wc -l
- ls /usr/local/bin/ ~/.local/bin/ 2>/dev/null | sort
- df -h / 2>/dev/null | tail -1

For each item to be installed, check if already installed:
- For dnf packages: grep against rpm -qa output
- For binaries: check ls /usr/local/bin/
- For go tools: check ls ~/.local/bin/

Report for EACH package/binary:
- what: package or binary name + version
- current: "installed at vX.Y" or "not installed"
- intended: "install vX.Y"
- good: true/false — is this needed for a Red Hat OVN-K/bpfman/Submariner developer?
- concern: any reason to reconsider (leave empty if fine)

Focus especially on:
- OVN + openvswitch: do they start daemons? Check default systemd enable state.
- bpfman: needs CAP_SYS_ADMIN — is the SELinux policy in Fedora 44 for bpfman correct?
- tailscale: any config needed after install?
- virtualization (libvirt, qemu-kvm): what groups added? Any concerns on this machine?
- Binary downloads: all to /usr/local/bin with SHA256 verification — good practice?
`, { label: 'packages-role', phase: 'Role Changes', schema: CHANGE_SCHEMA, model: M }),

  // make dotfiles + make ssh
  () => agent(`
${MACHINE}
TASK: Enumerate every concrete change make dotfiles and make ssh will make.

Read:
- roles/dotfiles/tasks/main.yml
- roles/dotfiles/templates/zshrc.j2
- roles/dotfiles/templates/gitconfig.j2
- roles/dotfiles/templates/ssh_config.j2
- roles/dotfiles/templates/bashrc.j2
- roles/dotfiles/templates/tmux.conf.j2
- roles/dotfiles/templates/allowed_signers.j2
- roles/dotfiles/defaults/main.yml
- roles/ssh/tasks/main.yml
- roles/ssh/tasks/keyscan_host.yml
- roles/ssh/defaults/main.yml
- common/tasks/pre_flight_checks.yml (the SSH lockout guard we added)
- config.yml

Run:
- getent passwd dfarrell | cut -d: -f7
- cat /etc/shells
- ls ~/.ssh/ 2>/dev/null
- git config --global --list 2>/dev/null
- cat ~/.gitconfig 2>/dev/null | head -20
- head -1 /home/dfarrell/laptop-setup/group_vars/all/vault.yml
- ls ~/.config/tmux/ ~/.tmux.conf 2>/dev/null
- cat ~/.bashrc 2>/dev/null | grep -v "^#" | grep -v "^$" | head -20

For each file/setting changed, report current→intended and whether it's good.
Key items to check:
- chsh to zsh: verify /etc/shells includes zsh, PAM will allow it
- git signing: what happens when vault is empty (no ssh signing key)?
- ssh_config.j2: what StrictHostKeyChecking mode? What hosts get custom config?
- ssh-agent service: what does it do exactly? Conflict with existing gnome-keyring?
- known_hosts: what entries are added for which hosts?
- PATH additions in zshrc: any shadowing concerns?
- GOPATH/GOBIN: where are Go binaries installed?
- tmux prefix: C-Space (from config.yml) — correct?
- authorized_keys: is it written when vault is empty? (should be: NO, guarded)
`, { label: 'dotfiles-ssh-role', phase: 'Role Changes', schema: CHANGE_SCHEMA, model: M }),

  // make desktop + make repos_dnf
  () => agent(`
${MACHINE}
TASK: Enumerate changes from make desktop and make repos_dnf.

Read:
- roles/desktop/tasks/main.yml
- roles/desktop/defaults/main.yml
- roles/desktop/templates/sway.config.j2
- roles/desktop/templates/swaylock.config.j2
- roles/desktop/templates/alacritty.toml.j2
- roles/desktop/templates/i3status.conf.j2
- roles/desktop/templates/gtk-settings.ini.j2
- roles/desktop/files/chrome-security-policy.json
- roles/repos_dnf/tasks/main.yml
- roles/repos_dnf/defaults/main.yml
- config.yml

Run:
- dnf repolist --enabled 2>/dev/null | head -30
- ls /etc/yum.repos.d/ 2>/dev/null
- ls /etc/opt/chrome/policies/managed/ 2>/dev/null
- ls ~/.config/sway/ ~/.config/alacritty/ 2>/dev/null
- systemctl is-active gdm sddm lightdm 2>/dev/null

For desktop:
- sway config: what does it configure? keybindings, outputs, trackpoint, swaylock?
- alacritty: is block/rescue already there from previous fix? Does alacritty install succeed?
- chrome policy: check if IT policy already exists (stat check should skip if present)
- wl-clipboard: is it installed? (needed for copy/paste in alacritty/sway)
- gtk settings: what theme/font is deployed?
- xorg trackpoint conf: deployed to /etc/X11/xorg.conf.d/ — relevant on Wayland? Harmless?
- battery display in i3status: config.yml has desktop_i3status_battery_num: 0 but is battery visible?

For repos_dnf:
- Which repos are added that are NOT already present?
- Are repo GPG keys verified?
- mullvad repo: appropriate on work laptop?
- rpmfusion-free: any concerns on CSB Fedora?
`, { label: 'desktop-repos-role', phase: 'Role Changes', schema: CHANGE_SCHEMA, model: M }),

  // make containers + make redhat + make claude + make repos + make notes + make container
  () => agent(`
${MACHINE}
TASK: Enumerate changes from make containers, make redhat, make claude, make repos, make notes, make container.

Read:
- roles/containers/tasks/main.yml
- roles/containers/defaults/main.yml
- roles/containers/templates/registries.conf.j2
- roles/containers/templates/policy.json.j2
- roles/containers/templates/containers.conf.j2
- roles/redhat/tasks/main.yml
- roles/redhat/defaults/main.yml
- roles/claude/tasks/main.yml
- roles/claude/defaults/main.yml
- roles/claude/templates/settings.local.json.j2
- roles/claude/templates/claude-privacy.conf.j2
- roles/claude/files/claude-queue-poller.sh
- roles/git_repos/tasks/main.yml
- roles/git_repos/defaults/main.yml
- roles/notes/tasks/main.yml
- roles/notes/defaults/main.yml
- roles/distrobox/tasks/main.yml
- roles/distrobox/defaults/main.yml
- group_vars/all/repos.yml
- config.yml

Run:
- cat /etc/containers/policy.json 2>/dev/null
- cat /etc/containers/registries.conf 2>/dev/null | head -20
- ls /etc/pki/ca-trust/source/anchors/ 2>/dev/null | head -15
- command -v claude 2>/dev/null
- systemctl --user is-active claude-queue 2>/dev/null
- ls ~/.claude/ 2>/dev/null | head -10
- df -h / 2>/dev/null | tail -1
- distrobox list 2>/dev/null
- grep notes_enabled /home/dfarrell/laptop-setup/config.yml 2>/dev/null

For EACH role, report every concrete file/service/config that changes:

CONTAINERS: registries.conf (user-level or system-level?), policy.json (what signature policy?), containers.conf changes, podman-prune timer.

REDHAT: What runs on this machine (csb_detected=true, not csb_rhel)? IT CA certs already installed? What specifically does the role add?

CLAUDE: How is Claude CLI installed (URL + SHA256 verify?)? queue-poller service — what does it do? privacy.conf — what settings? settings.local.json content? Any sensitive data in deployed files?

GIT_REPOS: What repos are cloned? Where? SSH key needed? VPN check behavior when not on VPN?

NOTES: Is notes_enabled set? Does anything run if not set?

DISTROBOX: What container image? What gets installed inside? Is the image accessible without VPN?
`, { label: 'misc-roles', phase: 'Role Changes', schema: CHANGE_SCHEMA, model: M }),

])

// ─── PHASE 3: VERIFY EVERY CHANGE ────────────────────────────────────────────
phase('Verify')

// Collect all problems from role agents
const allRoleData = roleChanges.filter(Boolean)
const allProblems = allRoleData.flatMap(r => r.problems || [])
const allChanges = allRoleData.flatMap(r => (r.changes || []).filter(c => !c.good || c.concern))

log('Total changes with concerns: ' + allChanges.length + '. Problems found: ' + allProblems.length)

// Adversarially verify each concern/problem in parallel
const verifyTasks = [...allProblems, ...allChanges.slice(0, 6)].slice(0, 10).map(function(item, idx) {
  const isProb = item.why_bad !== undefined
  const desc = isProb ? item.change + ': ' + item.why_bad : item.what + ': ' + item.concern
  return function() {
    return agent(`
${MACHINE}
You are adversarially verifying this concern about provisioning this ThinkPad P16v Gen 1 AMD:

"${desc}"
${isProb ? 'Proposed fix: ' + item.fix : 'Concern level: ' + (item.good ? 'minor' : 'significant')}

Read the SPECIFIC task file and current system state to determine if this concern is REAL.
Look at the actual code. Check if there are already mitigations in place.

Run relevant system checks to verify current state.
Read relevant task files to check the exact code path.

Return:
- verdict: REAL_PROBLEM / FALSE_ALARM / ALREADY_MITIGATED
- evidence: what you read that proves it
- fix_needed: specific change required (empty if no fix needed)
- fix_file: which file to change
`, {
      label: 'verify-' + idx,
      phase: 'Verify',
      model: M,
      schema: {
        type: 'object',
        properties: {
          original_concern: { type: 'string' },
          verdict: { type: 'string', enum: ['REAL_PROBLEM', 'FALSE_ALARM', 'ALREADY_MITIGATED'] },
          evidence: { type: 'string' },
          fix_needed: { type: 'string' },
          fix_file: { type: 'string' }
        },
        required: ['original_concern', 'verdict', 'evidence', 'fix_needed']
      }
    })
  }
})

const verified = await parallel(verifyTasks)

// ─── PHASE 4: FIX REAL PROBLEMS ──────────────────────────────────────────────
phase('Fix')

const realProblems = (verified || []).filter(Boolean).filter(function(v) { return v.verdict === 'REAL_PROBLEM' })
log('Real problems confirmed: ' + realProblems.length)

const fixResult = await agent(`
${MACHINE}
You are fixing confirmed real problems in /home/dfarrell/laptop-setup.

CONFIRMED REAL PROBLEMS:
${realProblems.map(function(p) { return JSON.stringify(p, null, 2) }).join('\n\n')}

ALL CHANGES WITH CONCERNS (for context — check if any need addressing):
${allChanges.map(function(c) { return c.what + ': ' + c.concern }).join('\n').slice(0, 3000)}

RULES:
- Read file before editing (use Read tool)
- Minimal targeted changes only
- Preserve code style
- config.yml is gitignored (edit directly, not committed)
- Tracked files: edit with Edit tool (will be committed)

PROTECTED USER SETTINGS — DO NOT CHANGE THESE IN config.yml:
- system_tmout: 600 — user explicitly set for CIS 5.5.5 compliance; do NOT change to 0 or 1800
- system_firewall_icmp_extra_types: [echo-request] — user explicitly set for OVN-K/kind testing; do NOT remove or comment out
- packages_kubectl_source: upstream — user explicitly set; do NOT change
- system_modules_disabled: 0 — user explicitly set for dev laptop; do NOT change
- system_kernel_panic: 10 — user explicitly set; do NOT change
- system_dnf_automatic_apply_updates: false — user explicitly set; do NOT change
These settings represent intentional trade-offs the user has made. Flag them as informational concerns only, never as "problems to fix".

For each real problem:
1. Read the relevant file
2. Apply the minimal correct fix
3. Verify fix looks right

Then produce a COMPLETE CHANGE SUMMARY covering:
A) What make all will do to THIS specific ThinkPad (all good changes)
B) What problems were found and fixed
C) Any remaining concerns the user should know about
D) Current config.yml state — anything the user should add/change
`, { label: 'fix-and-summarize', phase: 'Fix', model: M })

// ─── PHASE 5: COMMIT ─────────────────────────────────────────────────────────
phase('Commit')

const commitResult = await agent(`
You are in /home/dfarrell/laptop-setup. Commit any tracked file improvements.

1. git status && git diff --stat HEAD
2. git diff HEAD | head -200
3. make syntax-check 2>&1 | tail -5
4. If changes exist and syntax passes, commit each logical group with:
   - git commit -s (signoff required)
   - Max 72 char subject: "Scope: noun-phrase"
5. git log --oneline -10
6. bash scripts/preflight.sh 2>&1 | grep -E "FAIL|WARN" | grep -v "registry\\|yubikey\\|vault_enc\\|tailscale\\|sudo\\|ptrace\\|container_tier" | head -20

Report what was committed and final clean preflight state.
`, { label: 'commit', phase: 'Commit', model: M })

return {
  snapshotDone: !!snapshot,
  roleDataCount: allRoleData.length,
  problemsFound: allProblems.length,
  realProblems: realProblems.length,
  fixResult,
  commitResult
}

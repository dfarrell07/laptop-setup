
export const meta = {
  name: 'laptop-setup-full-make-audit',
  description: 'Per-role audit: CSB conflicts, UX, laptop safety for every make target; fix and commit',
  phases: [
    { title: 'Role Reads', detail: 'One agent per make target — enumerate every concrete change' },
    { title: 'CSB Research', detail: 'Web research on CSB automation, known conflicts, compatibility' },
    { title: 'Safety Analysis', detail: 'Cross-check all changes against CSB, UX, laptop safety' },
    { title: 'Deep Verify', detail: 'Adversarial verification of highest-risk findings' },
    { title: 'Fix', detail: 'Apply playbook fixes for confirmed issues' },
    { title: 'Commit', detail: 'Commit all improvements' },
  ],
}

const M = 'claude-sonnet-4-5@20250929'

const ROLE_SCHEMA = {
  type: 'object',
  properties: {
    role: { type: 'string' },
    make_target: { type: 'string' },
    changes: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          category: { type: 'string' },
          path_or_name: { type: 'string' },
          action: { type: 'string' },
          detail: { type: 'string' },
          reversible: { type: 'boolean' },
          requires_reboot: { type: 'boolean' }
        },
        required: ['category', 'path_or_name', 'action', 'detail']
      }
    },
    csb_risk_items: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          item: { type: 'string' },
          risk: { type: 'string' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] }
        },
        required: ['item', 'risk', 'severity']
      }
    },
    ux_concerns: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          concern: { type: 'string' },
          impact: { type: 'string' },
          severity: { type: 'string', enum: ['blocking', 'major', 'minor'] }
        },
        required: ['concern', 'impact', 'severity']
      }
    },
    laptop_safety: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          concern: { type: 'string' },
          details: { type: 'string' },
          mitigated: { type: 'boolean' }
        },
        required: ['concern', 'details', 'mitigated']
      }
    }
  },
  required: ['role', 'make_target', 'changes', 'csb_risk_items', 'ux_concerns', 'laptop_safety']
}

const CTX = `MACHINE CONTEXT:
- ThinkPad P16v Gen 1 AMD, Fedora 44, hybrid CSB (csb_detected=true, NOT csb_rhel)
- Red Hat IT manages this laptop with their own Ansible (SCAP compliance, security policies)
- IT monitoring: Kolide Fleet (orbit) + osquery running as services
- IT-installed: redhat-internal-infosec-config.sh in /etc/profile.d/ (HISTCONTROL/HISTSIZE/HISTTIMEFORMAT)
- IT-managed drop-ins: orbit.service.d/override.conf, osqueryd.service.d/override.conf
- profile=work (Red Hat OVN-K / Submariner / bpfman / Konflux developer)
- config.yml: /home/dfarrell/laptop-setup/config.yml
- Vault: currently PLAINTEXT STUB (no real SSH keys yet)
- YubiKey: NOT present
- SSH port: currently 22, will move to 722
- Desktop: sway, hidpi 1.5x
- Bluetooth: disabled (system_disable_bluetooth: true, not overridden in config.yml)
- Fingerprint: disabled (system_disable_fingerprint: true, not overridden)
- Kernel lockdown: currently [none], playbook sets integrity (post-reboot)`

// ─── PHASE 1: PARALLEL ROLE READS ────────────────────────────────────────────
phase('Role Reads')

const roleResults = await parallel([

  () => agent(CTX + `
You are auditing the SYSTEM role (make system).
Read ALL of:
- roles/system/tasks/main.yml
- roles/system/tasks/hardening_core.yml
- roles/system/tasks/mounts.yml
- roles/system/tasks/kernel_modules_blacklist.yml
- roles/system/tasks/sysctl.yml
- roles/system/tasks/firewall.yml
- roles/system/tasks/kernel_lockdown.yml
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
- roles/system/defaults/main.yml
- config.yml

CSB FOCUS — flag anything that IT's SCAP/compliance automation might also manage:
1. PAM (faillock, pwquality, pwhistory, pam_wheel): SCAP manages PAM on CSB — does our pam.yml specify authselect profile? Does it conflict?
2. authselect: what profile does authselect_fingerprint.yml select? Does it override IT SCAP authselect?
3. Account hardening (login.defs PASS_MAX_DAYS=365, UMASK=027, HOME_MODE=0700): SCAP baseline for RHEL/Fedora CSB sets these — do we match or conflict?
4. Chrony: does chrony.yml deploy a full chrony.conf that REPLACES IT-managed chrony? Or adds a drop-in?
5. Session timeout: our TMOUT=0 (disabled). IT's redhat-internal-infosec-config.sh may set TMOUT. Conflict?
6. Crypto policy DEFAULT:NO-SHA1: does CSB IT mandate a specific crypto policy? Could NO-SHA1 break IT tools?
7. sshd AllowUsers: does the template include ONLY real_user_id? Could IT admin accounts be locked out?
8. NetworkManager: do our NM changes (MAC randomization, dispatcher) conflict with IT NM policies?
9. dnf-automatic: does our dnf-automatic config conflict with IT patch management schedules?
10. Firewall: our drop zone — could IT tools require inbound access (osquery, orbit)?
11. Kernel lockdown=integrity: does this break osquery's eBPF features? bpftrace? Fleet agent?

Return structured findings with all changes and CSB risk analysis.`,
    { label: 'role-system', phase: 'Role Reads', schema: ROLE_SCHEMA, model: M }),

  () => agent(CTX + `
You are auditing the PACKAGES role (make packages).
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

Also run:
- 'rpm -qa --qf "%{NAME}\\n" 2>/dev/null | sort > /tmp/installed_pkgs.txt && wc -l /tmp/installed_pkgs.txt'
- 'ls /usr/local/bin/ 2>/dev/null | head -30'
- 'df -h / 2>/dev/null | tail -1'

CSB FOCUS:
- golang + gcc: IT might manage Go version — conflict?
- ovn + openvswitch packages: installing these adds OVS kernel modules + daemon. Does this affect system networking even when not in use? Could CSB networking tools conflict?
- bpfman (eBPF program manager): runs as root daemon, requires CAP_SYS_ADMIN. SELinux policy on CSB? fapolicyd implications (fapolicyd inactive on hybrid but may be installed)?
- bpftrace: requires elevated capabilities. With kernel lockdown=integrity, does bpftrace work?
- tailscale: VPN daemon. IT might have VPN policies. Does tailscale conflict with IT VPN/split-tunnel?
- wireshark-cli/tshark: is network capture acceptable on CSB? dumpcap capabilities?
- nmap: port scanning on corporate network. Policy implications?
- Binary downloads to /usr/local/bin (kind, helm, etc.): PATH precedence vs /usr/bin?
- virtualization (libvirt, qemu-kvm): adds libvirt + kvm groups. SELinux implications? CSB policy?
- pipx tools (yamllint, ansible-lint): user-space pip installs in ~/.local/

Return full structured findings.`,
    { label: 'role-packages', phase: 'Role Reads', schema: ROLE_SCHEMA, model: M }),

  () => agent(CTX + `
You are auditing the DOTFILES role (make dotfiles) and SSH role (make ssh).
Read:
- roles/dotfiles/tasks/main.yml
- roles/dotfiles/templates/zshrc.j2
- roles/dotfiles/templates/gitconfig.j2
- roles/dotfiles/templates/ssh_config.j2
- roles/dotfiles/templates/bashrc.j2
- roles/dotfiles/templates/tmux.conf.j2
- roles/dotfiles/defaults/main.yml
- roles/ssh/tasks/main.yml
- roles/ssh/tasks/keyscan_host.yml
- roles/ssh/defaults/main.yml
- config.yml

Check:
- 'ls ~/.ssh/ 2>/dev/null'
- 'head -1 /home/dfarrell/laptop-setup/group_vars/all/vault.yml'
- 'getent passwd dfarrell | cut -d: -f7'
- 'cat /etc/shells 2>/dev/null'

CRITICAL CSB+SAFETY FOCUS:
1. SSH authorized_keys: if vault is plaintext stub, ssh_auth_key="". Does roles/ssh/tasks/main.yml guard against deploying an empty authorized_keys? If it writes empty authorized_keys, SSH login will be IMPOSSIBLE after provisioning (port 722 set, no key). Read the exact task that writes authorized_keys and check the when condition.
2. chsh to zsh: does PAM/authselect allow chsh? On CSB with SSSD, chsh behavior can differ.
3. HISTCONTROL in zshrc.j2: if we set HISTCONTROL and redhat-internal-infosec-config.sh also sets HISTCONTROL, which wins? Could readonly HISTCONTROL from IT script conflict?
4. ssh_config.j2: does it set StrictHostKeyChecking=no anywhere? TOFU risk for internal hosts?
5. gitconfig: signing configured with ssh key — if key not deployed (empty vault), git commits will fail signing. Is there a fallback?
6. Login shell change bash→zsh: PAM session scripts — do they work with zsh? /etc/shells must include zsh.
7. PATH in zshrc: /usr/local/bin before /usr/bin — any system binary shadowing?
8. ssh-agent service: conflict with CSB-managed keyring/gnome-keyring ssh-agent?

Return full structured findings.`,
    { label: 'role-dotfiles-ssh', phase: 'Role Reads', schema: ROLE_SCHEMA, model: M }),

  () => agent(CTX + `
You are auditing the DESKTOP role (make desktop), CONTAINERS role (make containers), and REPOS_DNF role (make repos_dnf).
Read:
- roles/desktop/tasks/main.yml
- roles/desktop/defaults/main.yml
- roles/desktop/templates/sway.config.j2
- roles/desktop/templates/swaylock.config.j2
- roles/desktop/templates/alacritty.toml.j2
- roles/desktop/files/chrome-security-policy.json
- roles/containers/tasks/main.yml
- roles/containers/defaults/main.yml
- roles/containers/templates/registries.conf.j2
- roles/containers/templates/policy.json.j2
- roles/containers/templates/containers.conf.j2
- roles/repos_dnf/tasks/main.yml
- roles/repos_dnf/defaults/main.yml
- config.yml

Check:
- 'dnf repolist 2>/dev/null | head -30'
- 'cat /etc/containers/policy.json 2>/dev/null'
- 'dnf info alacritty 2>/dev/null | head -5 || echo alacritty-not-found'
- 'systemctl is-active gdm sddm lightdm 2>/dev/null'

DESKTOP CSB FOCUS:
- alacritty: is it in Fedora 44 standard repos or needs copr/flatpak? If not in repos, dnf install fails.
- chrome-security-policy.json: deployed to /etc/opt/chrome/policies/managed/ — does IT Chrome policy in /etc/opt/chrome/policies/managed/ already exist? Our file would overwrite IT policy.
- dconf system hardening (run when gdm present): does dconf.yml conflict with IT GNOME policies?
- sway on Wayland: swaylock on lid close — interaction with logind HandleLidSwitch we set?
- xorg-trackpoint.conf deployed to /etc/X11/xorg.conf.d/: relevant for Wayland/sway? Harmless?

CONTAINERS CSB FOCUS:
- registries.conf: does IT manage /etc/containers/registries.conf on CSB? Would our template overwrite IT config?
- policy.json: signature policy — does IT require signed images? Could our policy.json be less strict?
- containers.conf: rootless podman settings — conflict with IT toolbox configs?
- containers_environment_d_enabled (currently not set = false): is this correct?

REPOS_DNF CSB FOCUS:
- repo_mullvad: true — Mullvad VPN on work laptop. IT may restrict third-party VPN repos.
- repo_rpmfusion_free: true — third-party repo on CSB. IT may block or have policies against it.
- Does IT's Ansible manage /etc/yum.repos.d/ and could overwrite our repos?
- GPG key verification for each repo — is it done?

Return full structured findings.`,
    { label: 'role-desktop-containers-repos', phase: 'Role Reads', schema: ROLE_SCHEMA, model: M }),

  () => agent(CTX + `
You are auditing the CLAUDE role (make claude), REDHAT role (make redhat), NOTES role (make notes), DISTROBOX role (make container), and GIT_REPOS role (make repos).
Read:
- roles/claude/tasks/main.yml
- roles/claude/defaults/main.yml
- roles/claude/templates/settings.local.json.j2
- roles/claude/templates/claude-privacy.conf.j2
- roles/claude/templates/claude-queue.service.j2
- roles/claude/files/claude-queue-poller.sh
- roles/redhat/tasks/main.yml
- roles/redhat/defaults/main.yml
- roles/notes/tasks/main.yml
- roles/notes/defaults/main.yml
- roles/distrobox/tasks/main.yml
- roles/distrobox/defaults/main.yml
- roles/git_repos/tasks/main.yml
- roles/git_repos/defaults/main.yml
- group_vars/all/repos.yml
- config.yml

Check:
- 'command -v claude 2>/dev/null'
- 'df -h / 2>/dev/null | tail -1'
- 'ls /etc/pki/ca-trust/source/anchors/ 2>/dev/null | head -10'

CSB FOCUS:
CLAUDE: How is Claude CLI installed? Is it downloaded from a verified URL with SHA256? Does claude-queue-poller.sh run as a user service? Security model? Does settings.local.json or CLAUDE.md contain sensitive info (emails, hostnames) that shouldn't be in config files?

REDHAT: What does this role do on hybrid CSB (csb_detected=true, not csb_rhel)? Read the exact when conditions. Does it install CA certs that IT already manages? Subscription-manager conflict?

NOTES: Is notes_enabled in config.yml? What does the notes role do (transcrypt encrypted git repo)? With empty vault and transcrypt not installed, does it fail gracefully?

GIT_REPOS: With empty vault (no SSH key), how do SSH-based repo clones fail? Gracefully or aborting entire play? VPN check for internal gitlab — if not on VPN, does make repos abort? Disk space check: how many GB do all work repos require?

DISTROBOX: On hybrid CSB, is needs_container_tier=hybrid, meaning the dev container IS needed. What image is used? Is it publicly accessible without VPN? What does container-provision.yml install inside?

Return full structured findings.`,
    { label: 'role-claude-redhat-misc', phase: 'Role Reads', schema: ROLE_SCHEMA, model: M }),

])

// ─── PHASE 2: WEB RESEARCH (PARALLEL WITH ROLE READS) ───────────────────────
phase('CSB Research')

const webResults = await parallel([

  () => agent(`
Research Red Hat CSB Ansible automation scope on hybrid Fedora machines. Search for:
1. What does Red Hat IT's compliance automation manage on hybrid CSB Fedora? (PAM, authselect, crypto-policy, chrony, sshd, dnf repos)
2. Does Red Hat CSB enforce specific authselect profiles via SCAP? Search "Red Hat SCAP authselect profile Fedora"
3. Does Red Hat IT manage chrony NTP configuration on CSB machines?
4. "RHEL CSB crypto policy enforcement" — does IT override update-crypto-policies settings?
5. Does Red Hat IT manage /etc/containers/registries.conf or /etc/containers/policy.json on CSB?
6. Known conflicts between user Ansible and Red Hat IT compliance automation
7. "redhat-internal-infosec-config.sh" — is this a standard Red Hat IT script? What does it do?

Also search:
- Kolide Fleet osquery CPU/IO throttling systemd drop-ins (CPUWeight=idle IOWeight=20)
- Does kernel lockdown=integrity break osquery eBPF probes?
- Does kernel lockdown=integrity break bpftrace kernel tracing?

Report concrete findings with sources where available.`,
    { label: 'web-csb', phase: 'CSB Research', model: M }),

  () => agent(`
Research Fedora 44 specific compatibility issues for this workstation setup:

1. "alacritty Fedora 44 dnf package" — Is alacritty available in Fedora 44 repos? Or is it copr/flatpak only?
2. "ovn Fedora package system impact" — Does installing the 'ovn' dnf package start any daemons or affect routing?
3. "bpfman Fedora SELinux CAP_SYS_ADMIN" — SELinux policy for bpfman daemon, any known issues?
4. "bpftrace kernel lockdown integrity mode" — Does lockdown=integrity prevent bpftrace from working?
5. "TLP ThinkPad P16v Gen 1 AMD battery" — TLP compatibility with this model?
6. "Fedora 44 sway SDDM login manager" — is SDDM the right display manager for sway on Fedora 44?
7. "tailscale corporate Red Hat VPN policy" — known issues with tailscale alongside corporate VPN?
8. "libvirt kvm Fedora 44 ThinkPad AMD-V nested" — virtualization on ThinkPad P16v AMD?
9. "mullvad VPN repository Fedora corporate policy" — risk of adding mullvad repo on work laptop?
10. "wl-clipboard Fedora alacritty Wayland copy paste" — clipboard setup needed?

Report all findings with sources.`,
    { label: 'web-fedora', phase: 'CSB Research', model: M }),

])

// ─── PHASE 3: SAFETY ANALYSIS ────────────────────────────────────────────────
phase('Safety Analysis')

const ANALYSIS_SCHEMA = {
  type: 'object',
  properties: {
    critical_issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          role: { type: 'string' },
          description: { type: 'string' },
          csb_conflict: { type: 'boolean' },
          laptop_safety_risk: { type: 'boolean' },
          fix_needed: { type: 'string' },
          fix_file: { type: 'string' }
        },
        required: ['title', 'role', 'description', 'csb_conflict', 'laptop_safety_risk', 'fix_needed']
      }
    },
    medium_issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          role: { type: 'string' },
          description: { type: 'string' },
          fix_needed: { type: 'string' }
        },
        required: ['title', 'role', 'description', 'fix_needed']
      }
    },
    all_clear: { type: 'array', items: { type: 'string' } },
    config_yml_recommendations: { type: 'array', items: { type: 'string' } }
  },
  required: ['critical_issues', 'medium_issues', 'all_clear', 'config_yml_recommendations']
}

const allRoleData = (roleResults || []).filter(Boolean)
const allWebData = (webResults || []).filter(Boolean)

const analysis = await agent(`
You are doing a comprehensive safety analysis for laptop-setup provisioning on a ThinkPad P16v Gen 1 AMD work laptop.

ROLE AUDIT FINDINGS:
${allRoleData.map(r => JSON.stringify(r, null, 1)).join('\n\n').slice(0, 28000)}

WEB RESEARCH:
${allWebData.map(r => String(r)).join('\n\n').slice(0, 8000)}

KNOWN MACHINE STATE:
- Fedora 44, hybrid CSB, not RHEL CSB
- Vault: PLAINTEXT STUB (no real SSH keys)
- YubiKey: not present
- IT monitoring: Kolide Fleet + osquery (active services)
- SSH currently port 22, will move to 722
- kernel lockdown: currently [none]
- Login shell: currently bash

CRITICAL QUESTIONS TO ANSWER:

1. SSH LOCKOUT RISK: With empty vault (ssh_auth_key=""), does the SSH role write an empty authorized_keys? If yes, after provisioning, SSH login to port 722 would be IMPOSSIBLE (empty authorized_keys, default console access required). Is this mitigated?

2. PAM/authselect CSB CONFLICT: Does our authselect profile selection conflict with Red Hat IT SCAP compliance automation? Could running authselect select-profile cause IT SCAP remediation to revert it repeatedly?

3. KERNEL LOCKDOWN + OSQUERY: Does kernel lockdown=integrity prevent osquery eBPF probes from working? Osquery is an IT monitoring tool — if we break it, IT may detect a policy violation.

4. KERNEL LOCKDOWN + BPFTRACE: Does lockdown=integrity prevent bpftrace from working? bpftrace is a key development tool for bpfman work.

5. ALACRITTY AVAILABILITY: Is alacritty in Fedora 44 standard repos? If not, 'make packages' will fail.

6. CHROME POLICY CONFLICT: Does our chrome-security-policy.json overwrite IT-managed Chrome policies on CSB?

7. OVN DAEMON: Does installing the 'ovn' dnf package start ovn-northd or ovn-controller daemons that could interfere with system networking?

8. HISTCONTROL CONFLICT: Does our zshrc HISTCONTROL setting conflict with readonly HISTCONTROL from redhat-internal-infosec-config.sh?

9. CHRONY REPLACEMENT: Does our chrony.yml replace IT-managed chrony config or add a safe drop-in?

10. MULLVAD REPO: Is adding mullvad repo acceptable on a Red Hat work laptop? IT policy concern?

Synthesize into critical, medium, and all_clear categories. Be SPECIFIC about file/task names. Only flag issues that are real based on code evidence or confirmed web research.`,
    { label: 'safety-synthesis', phase: 'Safety Analysis', schema: ANALYSIS_SCHEMA, model: M })

// ─── PHASE 4: DEEP VERIFY ────────────────────────────────────────────────────
phase('Deep Verify')

const criticalIssues = analysis ? analysis.critical_issues : []
log('Critical issues found: ' + criticalIssues.length + '. Verifying...')

// Verify up to 8 critical issues in parallel
const verifyPromises = criticalIssues.slice(0, 8).map(function(issue, idx) {
  return function() {
    return agent(`
You are adversarially verifying a reported critical issue in /home/dfarrell/laptop-setup.

ISSUE #${idx + 1}: "${issue.title}"
Role: ${issue.role}
Description: ${issue.description}
CSB Conflict: ${issue.csb_conflict}
Laptop Safety Risk: ${issue.laptop_safety_risk}
Proposed Fix: ${issue.fix_needed}
Fix File: ${issue.fix_file || 'not specified'}

TASK: Read the actual code and determine if this issue is REAL.

Read the relevant files for this role. For example:
- SSH issues: Read roles/ssh/tasks/main.yml — look for when conditions on authorized_keys write
- PAM/authselect: Read roles/system/tasks/pam.yml, roles/system/tasks/authselect_fingerprint.yml
- sshd/AllowUsers: Read roles/system/templates/sshd-hardening.conf.j2
- Chrony: Read roles/system/tasks/chrony.yml, roles/system/templates/chrony-nts.conf.j2
- Dotfiles HISTCONTROL: Read roles/dotfiles/templates/zshrc.j2
- Containers registries: Read roles/containers/templates/registries.conf.j2
- Packages alacritty: Run 'dnf info alacritty 2>/dev/null | head -10'
- OVN daemon: Run 'dnf info ovn 2>/dev/null | head -10'
- Also read config.yml: cat /home/dfarrell/laptop-setup/config.yml

Determine:
1. Is this issue CONFIRMED by reading the actual code?
2. Is there already a mitigation (when guard, length check, etc.)?
3. Is the proposed fix correct?

Return JSON with verdict (CONFIRMED, FALSE_POSITIVE, or MITIGATED) and evidence.`,
      { label: 'verify-' + idx, phase: 'Deep Verify', model: M,
        schema: {
          type: 'object',
          properties: {
            issue_title: { type: 'string' },
            verdict: { type: 'string', enum: ['CONFIRMED', 'FALSE_POSITIVE', 'MITIGATED'] },
            evidence: { type: 'string' },
            fix_correct: { type: 'boolean' },
            corrected_fix: { type: 'string' }
          },
          required: ['issue_title', 'verdict', 'evidence', 'fix_correct']
        }
      })
  }
})

const verifiedIssues = await parallel(verifyPromises)

// ─── PHASE 5: FIX ────────────────────────────────────────────────────────────
phase('Fix')

const confirmed = (verifiedIssues || []).filter(Boolean).filter(function(v) { return v.verdict === 'CONFIRMED' })
log('Confirmed issues needing fixes: ' + confirmed.length)

const fixResult = await agent(`
You are fixing confirmed issues in /home/dfarrell/laptop-setup — an Ansible workstation provisioning playbook.

CONFIRMED ISSUES:
${confirmed.map(function(i) { return JSON.stringify(i, null, 2) }).join('\n\n')}

MEDIUM ISSUES FROM ANALYSIS:
${((analysis ? analysis.medium_issues : []) || []).map(function(i) { return JSON.stringify(i, null, 2) }).join('\n\n').slice(0, 4000)}

CONFIG.YML RECOMMENDATIONS:
${((analysis ? analysis.config_yml_recommendations : []) || []).join('\n')}

TASK: For each CONFIRMED issue, read the file and apply the minimal correct fix.

IMPORTANT RULES:
- Read every file before editing with the Read tool
- Make minimal, targeted changes only
- Preserve existing behavior and code style
- config.yml edits: apply directly (gitignored, not committed)
- Tracked playbook files: edit with Edit tool (will be committed)

SPECIFIC KNOWN FIXES TO CHECK/APPLY:

1. ALACRITTY AVAILABILITY: Run 'dnf info alacritty 2>/dev/null | head -5'. If alacritty is NOT in Fedora 44 repos, read roles/desktop/tasks/main.yml — is there already a fallback? If alacritty is missing, update the install task to handle the missing package gracefully (failed_when: false or use a conditional install). Check if alacritty is installed via flatpak or copr instead.

2. If OVN daemon issue confirmed: check if ovn-northd/ovn-controller are enabled after install. If not explicitly disabled in packages role, they should be masked by the system role or a task added.

3. If chrome policy conflict: check if roles/desktop/tasks/main.yml has a guard to not overwrite existing IT policy. If not, add a stat check that skips deployment if IT policy exists.

4. If HISTCONTROL conflict: check roles/dotfiles/templates/zshrc.j2 — does it set readonly HISTCONTROL? If redhat-internal-infosec-config.sh sets readonly HISTCONTROL first, our zshrc assignment fails silently or errors. Fix: wrap our HISTCONTROL assignment in a guard: [[ -z "\${HISTCONTROL+x}" ]] && export HISTCONTROL=ignoredups:erasedups

5. If containers registries conflict: check if /etc/containers/registries.conf is IT-managed. If so, consider using ~/.config/containers/registries.conf (user-level) instead of system-level.

After fixing, report what was changed and in what file.`,
    { label: 'apply-fixes', phase: 'Fix', model: M })

// ─── PHASE 6: COMMIT ─────────────────────────────────────────────────────────
phase('Commit')

const commitResult = await agent(`
You are reviewing and committing improvements to /home/dfarrell/laptop-setup.

TASK:
1. 'git status 2>/dev/null'
2. 'git diff --stat HEAD 2>/dev/null'
3. 'git diff HEAD 2>/dev/null | head -400'
4. Read each changed file to verify the changes are correct and minimal
5. 'cd /home/dfarrell/laptop-setup && make syntax-check 2>&1 | tail -8'

If syntax passes and changes look correct, commit each logical group:
- Use: git commit -s (--signoff required)
- Subject max 72 chars: "Scope: noun-phrase" format
- Scopes: System, Packages, Dotfiles, Ssh, Containers, Desktop, Repos-dnf, Claude, Notes, Distrobox, Multi

After commits:
- 'git log --oneline -8'
- 'cd /home/dfarrell/laptop-setup && bash scripts/preflight.sh 2>&1 | grep -E "FAIL|WARN" | head -20'

If no tracked files changed (all fixes were to gitignored config.yml), report that and summarize config.yml changes made.

Report: what was committed or why no commits were made, preflight final status.`,
    { label: 'commit', phase: 'Commit', model: M })

return { analysisStats: { critical: criticalIssues.length, confirmed: confirmed.length }, fixResult, commitResult }

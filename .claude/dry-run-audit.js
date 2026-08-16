import path from 'path'

export const meta = {
  name: 'laptop-dry-run-audit',
  description: 'Rotating multi-agent dry-run audit of laptop-setup Ansible automation — find and fix bad/unnecessary changes',
  phases: [
    { title: 'State',   detail: 'Load audit state, pick focus area' },
    { title: 'Analyze', detail: '10 parallel agents examine focus area from different angles' },
    { title: 'Verify',  detail: 'Adversarially verify each candidate issue' },
    { title: 'Fix',     detail: 'Apply confirmed fixes to automation code' },
    { title: 'Record',  detail: 'Persist state and summarize' },
  ],
}

const REPO  = process.env.LAPTOP_SETUP_REPO ?? path.resolve(new URL('..', import.meta.url).pathname)
const STATE = `${REPO}/.dry-run-audit-state.json`

const AREAS = [
  'sysctl_kernel_cmdline',
  'mount_hardening',
  'firewall_network',
  'sshd_hardening',
  'pam_authselect_faillock',
  'services_masking',
  'packages_binaries',
  'dotfiles_git_xdg',
  'desktop_sway_hardware',
  'containers_distrobox',
  'csb_detection_tier',
  'claude_role_sandbox',
  'auditd_usbguard_tlp',
  'cross_cutting_idempotency',
]

// Frozen machine snapshot — agents should not re-derive this.
const MACHINE = `
Machine: ThinkPad P16v Gen 1 AMD, Ryzen 7 PRO 7840HS, 64 GB RAM, 953 GB LUKS-encrypted btrfs
Display: 2560x1600 16-inch HiDPI — sway needs scale 1.5, but desktop_sway_hidpi_scale NOT in config.yml yet
OS: Fedora 44 Workstation; current session GNOME Wayland; config.yml says desktop_environment=sway
CSB tier: hybrid — .csb FQDN + RH internal CAs present, fapolicyd NOT installed/active
SSH: listening on port 22 (playbook would move to 722); no sshd drop-ins; semanage not installed yet
Firewall: default zone FedoraWorkstation (playbook would switch to drop zone)
Kernel: lockdown=none, ptrace_scope=0, sysrq=16 (playbook hardens all three)
LUKS: rd.luks.key keyfile in cmdline; system_remove_luks_key defaults to false — leave it
Secure Boot: disabled (playbook checks but does not enable)
vault.yml: unencrypted stub — no real SSH keys; key-dependent tasks will be skipped
Already provisioned: bashrc/zshrc/tmux/vimrc (Ansible-managed), ssh client config, gitconfig,
  environment.d, user-tmpfiles.d, ssh-agent.service active, known_hosts hashed,
  containers.conf (k8s-file), podman.socket active, loginctl linger enabled,
  gnome-keyring-ssh.desktop Hidden=true, git commit signing configured
gcr-ssh-agent.socket: active (competing with custom ssh-agent; SSH_AUTH_SOCK still points to GCR)
`.trim()

const ISSUE_SCHEMA = {
  type: 'object',
  required: ['issues'],
  properties: {
    issues: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'severity', 'file', 'what_playbook_does', 'why_problematic', 'proposed_fix'],
        properties: {
          title:              { type: 'string' },
          severity:           { enum: ['critical', 'high', 'medium', 'low'] },
          file:               { type: 'string' },
          what_playbook_does: { type: 'string' },
          why_problematic:    { type: 'string' },
          proposed_fix:       { type: 'string' },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['confirmed', 'reasoning', 'safe_to_fix'],
  properties: {
    confirmed:    { type: 'boolean' },
    reasoning:    { type: 'string' },
    safe_to_fix:  { type: 'boolean' },
    revised_fix:  { type: 'string' },
  },
}

// ── Phase 1: Load state ─────────────────────────────────────────────────────
phase('State')

const stateRaw = await agent(
  `Run: cat ${STATE} 2>/dev/null
   If it prints valid JSON, return that object. Otherwise return {"area_index":0,"fixes_total":0}.`,
  {
    label: 'load-state',
    phase: 'State',
    schema: {
      type: 'object',
      properties: {
        area_index:   { type: 'number' },
        fixes_total:  { type: 'number' },
      },
    },
  }
)

const state     = stateRaw || { area_index: 0, fixes_total: 0 }
const areaIndex = (state.area_index || 0) % AREAS.length
const area      = AREAS[areaIndex]
log(`Area ${areaIndex + 1}/${AREAS.length}: ${area}`)

// ── Phase 2: 10-angle parallel analysis ────────────────────────────────────
phase('Analyze')

const ANGLES = [
  `Read ALL task files, defaults, and templates for area "${area}" under ${REPO}/roles/ and ${REPO}/common/. List every concrete change the playbook would make on FIRST PROVISION of this specific machine. Include exact file paths and variable names. Flag anything wrong or unnecessary for this hardware/context.`,

  `For area "${area}": run relevant commands to read CURRENT machine state (sysctl, systemctl is-active/is-enabled, cat /etc/..., ss -tlnp, firewall-cmd, findmnt, etc). Cross-reference against what ${REPO}/roles/ defaults and tasks would enforce. Find: (a) playbook adds something unneeded, (b) playbook misses a hardware nuance, (c) current state already matches what playbook would set (no-op candidates).`,

  `For area "${area}": CSB correctness check. This machine is CSB hybrid (csb_detected=true, needs_container_tier=hybrid, fapolicyd=false). Read ${REPO}/common/tasks/csb_detect.yml plus relevant role files. Find: (a) hard-fail tasks that should degrade gracefully on CSB, (b) CSB block/rescue missing where it's needed, (c) conditions that are wrong for hybrid vs container tier.`,

  `For area "${area}": idempotency audit. Read role task files in ${REPO}/roles/. Would a second playbook run show changed=0? Find tasks with: missing 'creates:' guards on downloads, spurious 'changed_when: true', file writes that always show changed, or handler notifications on unchanged state.`,

  `For area "${area}": over-hardening and dev-workflow impact. This machine runs OVN-K kind clusters, Podman rootless containers, Go builds, gdb/delve debuggers, and Kubernetes operator development. Find hardening measures in ${REPO}/roles/ where: (a) security benefit is marginal for a developer workstation, (b) the measure breaks a legitimate dev workflow, (c) a less aggressive alternative exists.`,

  `For area "${area}": defaults appropriateness review. Read ${REPO}/default.config.yml and ${REPO}/roles/*/defaults/main.yml. Find: (a) hardcoded values that should be machine-specific overrides in config.yml, (b) config.yml knobs that are missing but needed for this machine (especially HiDPI, TrackPoint, CSB-specific tuning), (c) defaults that are wrong for Fedora 44 or this AMD hardware.`,

  `For area "${area}": molecule coverage gaps. Read ${REPO}/molecule/ scenario configs and ${REPO}/molecule/shared/system-container-overrides.yml. Find: (a) production behaviors hidden by molecule overrides that should be tested, (b) important assertions missing from verify steps, (c) idempotence failures that the test sequence would mask, (d) scenarios that test the wrong OS version or config.`,

  `For area "${area}": play/task ordering and become: analysis. Read ${REPO}/site.yml carefully. Find: (a) tasks in wrong play (Play 1 system vs Play 2 user), (b) dependency ordering issues where task A needs something task B creates later, (c) incorrect become: true usage, (d) pre_task/post_task gaps, (e) DNS/network race conditions between plays.`,

  `For area "${area}": smoke test coverage analysis. Read ${REPO}/scripts/smoke-test.sh sections relevant to this area. Find: (a) smoke checks that would FAIL on first provision before reboot (kernel params, lockdown), (b) important provisioned state not verified by smoke test, (c) smoke test checking wrong value/path/format vs what the role actually deploys, (d) --user-only mode gaps for molecule CI.`,

  `For area "${area}": handler correctness review. Read ${REPO}/roles/*/handlers/main.yml and related notify: statements in tasks. Find: (a) missing 'failed_when: false' guards for container environments, (b) handlers that use systemctl instead of SysV service where needed (auditd), (c) incorrect listen/notify relationships, (d) handlers that fire too early or too late relative to task ordering.`,
]

const raw = await parallel(
  ANGLES.map((prompt, i) => () =>
    agent(
      `${MACHINE}\n\nRepo: ${REPO}\n\n${prompt}\n\nReturn ONLY genuine issues — not theoretical ones. Empty issues array is correct if nothing found.`,
      { label: `analyze-${i}`, phase: 'Analyze', schema: ISSUE_SCHEMA }
    )
  )
)

const allIssues = raw.filter(Boolean).flatMap(r => r.issues || [])

// Dedup by lowercased title prefix
const seen = new Set()
const deduped = allIssues.filter(issue => {
  const key = issue.title.toLowerCase().replace(/\W+/g, ' ').slice(0, 50)
  if (seen.has(key)) return false
  seen.add(key)
  return true
})

log(`${allIssues.length} raw → ${deduped.length} unique issues`)

if (deduped.length > 0) {
  // ── Phase 3+4: Verify → Fix, pipelined per issue ─────────────────────────
  phase('Verify')

  const results = await pipeline(
    deduped,

    // Stage: adversarial verification
    issue => agent(
      `${MACHINE}\n\nRepo: ${REPO}\n\nAn analysis agent flagged this issue:\nTitle: ${issue.title}\nSeverity: ${issue.severity}\nFile: ${issue.file}\nWhat playbook does: ${issue.what_playbook_does}\nWhy problematic: ${issue.why_problematic}\nProposed fix: ${issue.proposed_fix}\n\nYour job: try to REFUTE this finding. Read the actual file at ${issue.file}. Is the concern real for this specific machine? Is the proposed fix safe, minimal, and correct? Err toward confirmed=false when uncertain.`,
      { label: `verify:${issue.title.slice(0, 28)}`, phase: 'Verify', schema: VERDICT_SCHEMA }
    ),

    // Stage: apply fix for confirmed issues
    (verdict, issue) => {
      if (!verdict || !verdict.confirmed || !verdict.safe_to_fix) return null
      const fix = verdict.revised_fix || issue.proposed_fix
      return agent(
        `${MACHINE}\n\nRepo: ${REPO}\n\nApply this confirmed fix to the automation code:\nTitle: ${issue.title}\nFile: ${issue.file}\nFix to apply: ${fix}\n\nStrict rules:\n1. Read the file first with the Read tool\n2. Make the MINIMAL targeted change — touch nothing unrelated\n3. Preserve all existing indentation, formatting, and comments\n4. If adding a config.yml knob, add it to ${REPO}/default.config.yml with an inline comment\n5. Return one sentence: what you changed and in which file`,
        { label: `fix:${issue.title.slice(0, 28)}`, phase: 'Fix' }
      )
    }
  )

  const fixes = results.filter(Boolean)
  state.fixes_total = (state.fixes_total || 0) + fixes.length
  log(`${fixes.length} fixes applied (running total: ${state.fixes_total})`)
}

// ── Phase 5: Record ───────────────────────────────────────────────────────
phase('Record')
state.area_index = (areaIndex + 1) % AREAS.length

await agent(
  `Write the following JSON to the file ${STATE} using the Write tool:
${JSON.stringify(state, null, 2)}

Then run: cd ${REPO} && git diff --stat HEAD 2>/dev/null | head -20
Report which files changed (if any).`,
  { label: 'record', phase: 'Record' }
)

log(`Next pass: ${AREAS[(areaIndex + 1) % AREAS.length]}`)
return { area, issues_found: deduped.length, fixes_applied: state.fixes_total }

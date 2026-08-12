## wip3 Review

### Original Findings Status

| # | Finding | Severity | Status | Commit |
|---|---------|----------|--------|--------|
| 1 | packages_languages missing from dnf set_fact | high | FIXED | 08e07d67 |
| 2 | packages_security_rhel variable name mismatch | high | FIXED | 08e07d67 |
| 3 | Idempotent opm rename via copy+absent | low | FIXED | 884afced + 7a0e7b81 |
| 4 | session_timeout.yml Debian guard on profile.d | medium | FIXED | f00aa20d |
| 5 | PubkeyAcceptedAlgorithms removed from GitHub SSH host block | medium | FIXED | 2572bbee |
| 6 | packages_security (openssl) orphaned from dnf set_fact | medium | STILL OPEN | -- |

### New Findings (wip3)

**N1. Rescue messages reference stale `packages_fido2_rhel` variable name** (low)

`roles/packages/tasks/install_dnf_packages.yml` lines 65 and 72 still reference
`packages_fido2_rhel` in the `_csb_action` and `_csb_ticket_body` rescue message
strings. The set_fact expression was corrected to `packages_security_rhel` in
commit 08e07d67, but the human-readable rescue messages were not updated. CSB
failure tickets would reference a variable name that no longer exists, confusing
operators.

Action: replace `packages_fido2_rhel` with `packages_security_rhel` in both
rescue message strings.

**N2. `default.config.yml` stale `packages_fido2_rhel` reference** (low)

Line 73 of `default.config.yml` documents the variable as `packages_fido2_rhel`.
The variable was renamed to `packages_security_rhel` during the wip3 refactoring.
Users consulting default.config.yml for override guidance would use the wrong
variable name and get no effect.

Action: rename `packages_fido2_rhel` to `packages_security_rhel` in the comment.

**N3. `default.config.yml` stale `python3-pip is in packages_cli` comment** (low)

Line 76 of `default.config.yml` says "python3-pip is in packages_cli for all
profiles". In wip3, python3-pip was moved from `packages_cli` to
`packages_languages` (roles/packages/defaults/main.yml line 34). The comment
should say `packages_languages`.

Action: update comment to reference `packages_languages`.

**N4. bpfman.socket enable in packages/main.yml missing `not system_is_container` guard** (low)

`roles/packages/tasks/main.yml` enables bpfman.socket with `failed_when: false`
but no `not system_is_container` guard. The system role's `services.yml` properly
guards with `not system_is_container` and uses detect-before-enable. The packages
role relies on `failed_when: false` to suppress the failure in containers, which
generates a noisy red error in Ansible output before continuing. Molecule CI is
unaffected (bpfman not installed in containers), but the inconsistency with the
system role pattern could mask real failures.

Action: add `- not system_is_container` to the `when` conditions.

**N5. opm stable path uses stale SHA256 when oc download block is skipped** (medium)

In `roles/packages/tasks/install_oc_opm.yml`, when `packages_oc_version == 'stable'`
and oc is already at the correct version, the outer oc download block (lines 15-19)
is skipped entirely. `packages_oc_sha256sums` is never registered because the
`Fetch OpenShift client sha256sum.txt` task only runs inside that block. The opm
block's SHA256 extraction tasks (lines 146-159) guard with
`packages_oc_sha256sums is defined`, so they skip silently. The download then uses
the pinned `packages_opm_sha256` from defaults/main.yml. If the stable pointer has
advanced, the SHA256 no longer matches and the download fails. Fix: fetch
sha256sum.txt independently in the opm block when `packages_oc_version == 'stable'`
and `packages_oc_sha256sums` is undefined.

Action: add an independent sha256sum.txt fetch in the opm download block, guarded
by `packages_oc_version == 'stable' and packages_oc_sha256sums is not defined`.

**N6. openssl dropped from `packages_brew` without macOS fallback verification** (low)

Commit f0a6a562 removed `openssl` from `packages_brew` claiming brew's dependency
resolver handles it automatically. However, brew's openssl is keg-only and not
linked into PATH; macOS ships LibreSSL at `/usr/bin/openssl`, not GNU OpenSSL.
`packages_security` (line 42 of defaults/main.yml) documents that the openssl CLI
is "needed for s_client, genrsa, and vault scripts" -- LibreSSL's `s_client` has
different TLS 1.3 behavior and `genrsa` defaults differ. Combined with original
finding 6 (packages_security orphaned on dnf), openssl CLI is now uninstalled on
both Linux and macOS. The dnf fix (adding `+ packages_security` to set_fact) would
restore Linux; macOS needs `openssl` re-added to `packages_brew` or the
`packages_security` comment updated to clarify LibreSSL sufficiency.

Action: either re-add openssl to `packages_brew` or document that LibreSSL is
acceptable; fix original finding 6 for Linux.

## Messages from the Old Laptop

Direct communications to the P16v provisioning agent. Each addresses something
the automation does not know or cannot discover on its own.

### docker-workflow-conflict

DOCKER WORKFLOW RISK CONFIRMED but on a DIFFERENT MACHINE. This is a ThinkPad
X1 Carbon 7th Gen, not the P16v. Docker CE 29.5.3 is installed with
docker-ce-stable repo, docker.socket is active, and three
submariner-operator CVE fix images exist (fix-0.24-cves-2026-08-11, -v2,
-2026-08-12). No containers are currently running (all exited 3+ hours ago).
The critical gap: no config.yml exists on this machine, and
group_vars/all/vars.yml has install_docker: false (line 59). The containers
role at roles/containers/tasks/main.yml lines 204-218 would mask
docker.socket, stop docker.service when install_docker is false. FOR THE
P16V: if this user also does Docker-based CVE work there, ensure config.yml
includes `install_docker: true` and `repo_docker_ce: true` before running
make all. The automation handles this correctly when configured -- the toggle
exists, it just needs to be set.

### vpn

VPN MIGRATION CONFIRMED DELIBERATE. Old laptop runs ExpressVPN 3.84.0.6
(active, connected USA-Atlanta, service running). New P16v automation will
install Mullvad VPN instead -- this is intentional with zero ExpressVPN
references anywhere in the codebase. The automation handles: Mullvad RPM repo
setup (repos_dnf role, Fedora-only, skipped on CSB), mullvad-vpn package
install, mullvad-daemon service start+enable, and firewall WireGuard
interface allowance. POST-PROVISION ACTION REQUIRED: user must run
`mullvad account set ACCOUNT_NUMBER` to authenticate (interactive step, like
tailscale up). The user also needs to cancel their ExpressVPN subscription
separately -- that is a billing action outside the scope of automation. No
code changes needed; install_vpn defaults to true and repo_mullvad defaults
to true.

### disk-usage

DISK MIGRATION GUIDANCE -- What to copy from the old laptop:

**COPY THESE (small, irreplaceable):**
- ~/.ssh/ (SSH keys -- also encrypted in vault, but copy as backup)
- ~/laptop-setup/group_vars/all/vault.yml (encrypted vault)
- ~/laptop-setup/config.yml (machine-specific overrides)
- ~/.vimrc, ~/.zshrc customizations (if any beyond dotfiles role)
- ~/.zsh_history (command history)
- ~/notes/ (7.4 MB, private notes repo)
- ~/notes-backup/ (4.7 MB)

**REVIEW BEFORE DISCARDING:**
- ~/Downloads/ (6.1 GB) -- check for irreplaceable files
- ~/Maps/ (1.8 GB) -- possibly personal/irreplaceable map data
- ~/drone_25e29epelicantodavis/ (1.3 GB) -- drone footage, likely irreplaceable

**DO NOT COPY (all auto-rebuild or reclone):**
- ~/ovnk/ (77 GB) -- automation clones to ~/src/
- ~/.cache/ (60 GB) -- go-build 46G, chrome 4.2G, all rebuildable
- ~/go/ (20 GB) -- src/pkg/bin all reinstall via go install
- ~/.claude/jobs/ (6.8 GB) -- ephemeral session artifacts
- ~/advisory-database/ (4.5 GB) -- git reclone
- ~/.local/share/containers/ (3.3 GB) -- podman storage, rebuild
- ~/sdk/ (1.9 GB) -- Go SDKs, GOTOOLCHAIN=auto reinstalls
- ~/konflux/ (1.5 GB) -- git reclone
- ~/tmp*/ (3.4 GB) -- orphaned temp dirs
- Docker/Podman images (4+ GB) -- pull/build on demand

**EMERGENCY SPACE RECOVERY** (if old laptop needs room now):
`rm -rf ~/.cache/go-build ~/tmp.* ~/advisory-database` frees ~50 GB
immediately with zero risk.

### gemini-cli

Gemini CLI is a dead experiment on the old laptop. Installed 2025-07-26 via
manual `brew install gemini-cli`, used exactly twice on 2025-08-14, then
abandoned. The automation has no knowledge of it (zero references in any
role). Linuxbrew itself is also not managed by the automation. Do NOT add
Gemini CLI to P16v provisioning. If the user wants it later, it is a
30-second manual `brew install` -- no automation overhead is justified for a
tool with 2 lifetime sessions. The existing claude role already handles the
primary AI CLI tool.

### k8s-rebase-scripts

The k8s-rebase scripts are NOT candidates for laptop-setup automation
deployment. They live in the openshift-eng/ai-helpers git repo
(plugins/k8s-rebase/scripts/) which gets cloned by the git_repos role. The
loose copies at ~/.claude/k8s-rebase-autofix.sh (726 lines) and
~/k8s-rebase-tmp-scripts/k8s-rebase.sh (1129 lines) are stale development
copies that diverged from the canonical repo versions -- they were placed
there during iterative plugin development (shell history shows 19+ backup
branches). Once the ai-helpers repo is cloned on the new machine and the
Claude Code plugin is activated, the scripts are available automatically. The
~/k8s-rebase-tmp-scripts/ directory can be safely ignored during migration.
The ~/.claude/ copy might have a few newer fix functions not yet upstreamed
(fix_kubevirt_version, fix_metallb_version, etc.) but those should be
reconciled in the ai-helpers repo, not preserved as loose files.

### go-sdk-cache

No action needed for ~/sdk/. It is an auto-managed Go toolchain download
cache created by the GOTOOLCHAIN=auto environment variable (already
provisioned by the dotfiles role in zshrc.j2 line 30 and bashrc.j2 line 5).
When you build Go projects whose go.mod contains a 'toolchain' directive
requiring a different version than the system Go, the SDK is automatically
downloaded to ~/sdk/. This will happen naturally on the P16v as you work. The
old laptop's ~/sdk/ (1.9 GB, 5 versions) is safe to delete -- it will
regenerate on demand.

### openshift-kubeconfig

KUBECONFIG MIGRATION NOTES: Do NOT copy ~/.kube/config or ~/oc/ from the old
laptop. All cluster tokens are ephemeral OAuth and will be invalid. After
provisioning, the user needs to run `oc login` against these active clusters
in priority order:
1. Konflux prod kflux-prd-rh02.0fk9.p1.openshiftapps.com -- namespaces
   submariner-tenant and rhtap-releng-tenant
2. Konflux prod stone-prd-rh01.pg1f.p1.openshiftapps.com -- namespace
   arewm-tenant
3. OpenShift CI api.ci.l2s4.p1.openshiftapps.com -- namespace
   edge-infrastructure

Two additional clusters (perf1.chris.ocs.ninja and
almng-hive-upgrade-gcp) may be transient/decommissioned -- verify before
attempting. Registry auth (registry.redhat.io, quay.io,
brew.registry.redhat.io) also needs regeneration via `podman login` and
pull-secret from console.redhat.com. The laptop-setup dotfiles role already
provisions kfiles (fzf kubeconfig picker) and kenv (per-repo direnv
KUBECONFIG seeder) helpers -- these will be available after `make dotfiles`.
No automation changes needed for kubeconfig management.

### npm-config

NPM CONFIG STATUS: The old laptop has a manual ~/.npmrc with
prefix=~/.npm-global and fetch-retry timeout bumps. The automation does NOT
manage this file at all -- no template, no task, no variable. The
~/.npm-global/bin directory is empty (no real packages installed), so there
is nothing to migrate. On the P16v, npm (installed via Linuxbrew by the
packages role) will use its default prefix (/home/linuxbrew/.linuxbrew)
unless you manually create ~/.npmrc. The project-level .npmrc in
laptop-setup/ has ignore-scripts=true for security, but the user's global
~/.npmrc does NOT have this setting. RECOMMENDATION: Since npm-global is
empty and unused, you can skip recreating it on the P16v. If the user later
wants a custom prefix for npm globals, they should add it to ~/.npmrc
manually and also add ~/.npm-global/bin to PATH in their zshrc (the
automation's zshrc.j2 template does not include it). The automation correctly
backs up .npmrc (backup.sh) and blocks Claude from reading it (claude role
deny list), but does not provision it.

### aws-config

AWS config requires two manual steps on the P16v:
1. Add `dotfiles_aws_profile: "aws-acm-subm"` to config.yml so
   `make dotfiles` writes `export AWS_PROFILE=aws-acm-subm` into your .zshrc
   -- this is the only part the automation handles.
2. Manually create ~/.aws/config (with `[profile aws-acm-subm]`,
   region=us-east-2, output=text) and ~/.aws/credentials (with freshly
   rotated keys from AWS IAM).

The old laptop's keys (AKIA...47EB, created 2022-06-15) are over 4 years old
and should be rotated regardless. No Ansible role deploys these files; the
automation only audits reads of ~/.aws/ for claude-sensitive-read detection
and excludes ~/.aws/** from claude's file access.

### host-drift

The old laptop reveals significant unmanaged software that would cause
disruption if not accounted for during migration. The top items to address in
config.yml or the automation itself:
1. VPN choice -- this machine runs ExpressVPN but automation provisions
   Mullvad; decide which to use.
2. Docker CE is active but automation masks it -- set
   install_docker/repo_docker_ce to true in config.yml if Docker is still
   needed.
3. Six GUI apps (GIMP, LibreOffice, meld, gedit, eog, GNOME Boxes) need
   adding to packages role or manual install.
4. AWS CLI + credentials need migration.
5. Crypto tools (Trezor Suite, Ledger Live, Daedalus) and ham radio (CHIRP)
   are fully manual.
6. Dev tools grype, openshift-install, doctoc, markdownlint are in
   /usr/local/bin but unmanaged.

Files of interest: roles/packages/defaults/main.yml (package lists),
roles/repos_dnf/defaults/main.yml (repo toggles),
default.config.yml (config override documentation).

### Verified Medium+ Findings

1. **docker-workflow-conflict (medium)**: Running `make all` without
   `install_docker: true` in config.yml will mask docker.socket and stop
   docker.service (roles/containers/tasks/main.yml lines 204-218). Any
   active Docker workflow on the P16v would be killed mid-run. Set the
   toggle before provisioning if Docker is needed.
2. **host-drift / Docker CE (medium)**: The old laptop has Docker CE active
   with no config.yml and install_docker: false in vars.yml. The same
   default applies to the P16v. This is the same root cause as finding 1
   but confirms the pattern is real (it already bit this machine).
3. **aws-config / stale credentials (medium)**: AWS access keys are 4+ years
   old (created 2022-06-15). AWS best practice is 90-day rotation. Rotate
   before deploying to P16v.
4. **openshift-kubeconfig / registry auth gap (medium)**: Registry auth for
   registry.redhat.io, quay.io, and brew.registry.redhat.io must be
   regenerated manually post-provision. The smoke-test flags
   registry-redhat-auth as WARN but the other two registries are unchecked.
5. **host-drift / unmanaged dev tools (medium)**: grype, openshift-install,
   doctoc, and markdownlint in /usr/local/bin are not managed by any role.
   They will be absent on the P16v unless manually installed or added to the
   packages role.

## Learning Loop 2

### data-loss-risk

88 unpushed commits across 14 repos, 11 dirty working trees. Four critical
repos have 12-16 unpushed CVE-fix commits each: submariner-operator,
ci-tools, openshift/api, lighthouse (branches: fix-0.24-cves-*,
fix-main-cves-*, fix-master-cves-*). konflux/submariner-release-management
has 6 unpushed autorelease automation commits. Push all unpushed CVE-fix
branches before migration -- losing these would require significant effort
to recreate.

### chrome-extensions

Chrome security policy exists but is not deployed
(`desktop_deploy_chrome_policies: false`). Neither allowlisted extension
(uBlock Origin Lite MV3, Bitwarden) is installed in any profile. Six
disabled remnants across profiles (LastPass, MetaMask, Phantom, Privacy
Badger, YouTube, Google Docs Offline) would be blocked if the policy
deploys. Two distro-provided external extensions (Fedora User Agent, GNOME
Shell integration) would be removed by `BlockExternalExtensions: true`.
Active profiles: Default (gmail, Aug 2026) and Profile 6 (onc.design, Jul
2025); Profiles 1-5 are stale redhat.com profiles from 2022-2024.

### systemd-journal

Disk pressure (99%, 4.1G free) has cascading effects: auditd stopped
logging, journald cannot rotate logs, plocate-updatedb fails
(advisory-database exceeds default file descriptor limit -- either exclude
from index or raise LimitNOFILE). Quick wins beyond those in disk-usage:
vacuum journald logs (3.9G down to ~500M), clean Go caches. Claude CLI
shows recurring SIGILL crashes in coredumps (versions 2.1.140-2.1.153) --
likely upstream, not hardware. No hardware faults detected.

### non-dnf-apps

No Flatpak apps installed. No custom .desktop launchers. Two Ledger Live
AppImages in ~/Downloads (v2.60.0 stale, v2.96.0 current) -- only
non-dnf applications on the system. Clean up the stale 2.60.0 copy.

### scheduled-tasks

Nothing to migrate. No user crontab, no custom systemd user timers, no
~/.config/systemd/user/ directory. Only stock Fedora timers active
(systemd-tmpfiles-clean, grub-boot-success). podman-auto-update.timer
exists but is disabled.

### python

Homebrew python3 (3.13.5) shadows system python3 (3.13.13) in PATH. All
53 pip --user packages live under Homebrew's site-packages. The automation
expects yamllint/ansible-lint via pipx -- neither is currently present in
pipx. Non-automation pip --user packages: anthropic, gitlint, ruff,
rpm-lockfile-prototype, skillsaw, conforma. Non-automation pipx packages:
chirp, jinjanator, pipeline-migration-tool. Key files:
roles/packages/tasks/install_pipx.yml, requirements-test.lock.

### hardware-inventory

USBGuard whitelist requirements for P16v planning: YubiKey (1050:0407),
ZSA Moonlander (3297:1969), internal camera (13d3:56bb), fingerprint
reader (06cb:00bd), Bluetooth adapter (8087:0aaa). Root hubs (1d6b:0002,
1d6b:0003) are implicitly trusted. Eight Bluetooth-paired peripherals (3
Logitech MX mice across 5 pairings, 2 headsets, Keychron K2) connect via
the BT adapter and need no USB rules. If a Logitech Unifying (046d:c52b)
or Bolt (046d:c548) dongle is later connected, add it to the whitelist.

### gcloud-vertex-config

Vertex AI environment variables are manual additions in ~/.zshrc (lines
88-93), not managed by Ansible. The Claude role work-env template
(roles/claude/tasks/main.yml lines 95-105) has them commented out.
Migration actions: (1) uncomment and populate Vertex vars in Claude role
work-env template, (2) remove manual GOOGLE_CLOUD_PROJECT,
CLAUDE_CODE_USE_VERTEX, CLOUD_ML_REGION, ANTHROPIC_VERTEX_PROJECT_ID
lines from ~/.zshrc, (3) add `gcloud auth login` and `gcloud auth
application-default login` to CLAUDE.md step 6 manual actions. Project ID
mismatch needs resolution: GOOGLE_CLOUD_PROJECT=rich-charmer-232113 vs
gcloud core/project=itpc-gcp-hybrid-pe-eng-claude vs
ANTHROPIC_VERTEX_PROJECT_ID=itpc-gcp-hcm-pe-eng-claude.

### tailscale

Tailscale has never been installed on the old laptop -- no binary, RPM,
systemd unit, or state artifacts. This is new infrastructure on the P16v,
not a migration. No node identity or pre-existing keys to transfer. The
automation fully supports fresh Tailscale provisioning; `tailscale up` is
the only post-provision step.

### active-work-snapshot

Old laptop is under heavy load from an active Kubernetes 1.36.2 rebase
operation. Six Claude CLI sessions running the k8s-rebase plugin across
6 OVN-K repos (cloud-network-config-controller, cluster-network-operator,
ingress-node-firewall, multus-cni, ovn-kubernetes, ovn-kubernetes-mcp),
22 concurrent Go compiles, load average 58.58, 3 active Podman containers.
System at capacity: 7.9G/15G RAM used, 4.1G swap consumed, /home 98% full
(5.6G remaining). Migration during this rebase would be highly disruptive
-- all 6 Claude sessions hold significant state and the worktree data
under .repos/ represents hours of accumulated rebase progress. Disk
pressure is the most immediate risk even without migration.

### data-loss-risk (update)

Broader 130-repo scan found additional at-risk repos not in the initial
14-repo sweep: stolostron/acm-threat-model has 9 unpushed commits on
main. The 5 ovnk rebasebot repos (ovn-kubernetes, cluster-network-operator,
multus-cni, cloud-network-config-controller, ingress-node-firewall)
collectively have hundreds of local-only bump branches with no remote
backup. konflux/submariner-release-management has 120 local-only branches
beyond the 7 unpushed on main. Four repos with dirty tracked files:
stolostron/deploy, stolostron/submariner-addon, ovnk workspace root,
konflux/old_fbc.

### python (update)

Stale /usr/local/bin/pip wrappers from 2021 shadow RPM-provided
/usr/bin/pip. Testing dependencies (molecule, ansible-core, etc.) are
installed globally via pip --user rather than in the intended .venv
(no .venv directory exists; `make pip-sync` has never been run).

### gcloud-vertex-config (update)

`repo_google_cloud_sdk` defaults to false. If gcloud CLI is needed on
the P16v host (not just in containers), set `repo_google_cloud_sdk: true`
in config.yml before `make all`.

### active-work-snapshot (update)

Conditions have deteriorated since initial snapshot: /home now 100% full
with only 324MB free (was 5.6G), load average 72 (was 58.58), 45 total
Claude processes across 6 interactive terminals plus background tasks.
Swap 43% consumed. Migration preconditions: (1) k8s-rebase must complete
or be checkpointed, (2) /home disk space must be freed, (3) all 6
interactive Claude sessions must be saved/closed. Submariner operator
fix (`make fix REPO=submariner-operator BRANCH=0.24`) may also have
in-flight state from recent history.

### data-loss-risk (update 2)

78 git stashes across 13 repos are at risk -- these are local-only and
not backed up by pushing branches. Highest counts: submariner-operator
(20 stashes), submariner-operator-fbc (17), submariner (14), shipyard
(13). Stashes are destroyed if the repo is recloned or the disk fails.
Consider `git stash list` and selectively converting important stashes
to branches before migration.

### python (update 2)

Orphaned site-packages directories from Python 3.10 (1.8 MB) and 3.9
(8.9 MB) under ~/.local/lib/ are safe to remove on the old laptop.
Homebrew python3 exists only as a transitive dependency of gemini-cli
and node -- not directly installed.

## Learning Loop 2 (continued)

### data-loss-risk (update 3)

Three additional CVE-fix repos with unpushed branches not previously
itemized: submariner (8 commits), cloud-prepare (7),
prometheus/client_golang (4). Two repos have untracked local-only
branches not covered in earlier updates: ai-helpers (k8s-rebase-skill
branch) and secure-engineering-container (notgitleak branch).

### chrome-extensions (update)

Credential migration from LastPass to Bitwarden should happen before
deploying the Chrome security policy. LastPass is installed in the
Default profile but would be blocked by the allowlist; neither
allowlisted Bitwarden extension is installed yet. Migration sequence:
install Bitwarden, import credentials, verify, then deploy policy.

### active-work-snapshot (update 2)

Disk pressure eased to 82% full (193G/237G, ~44G free) from the prior
100% (324MB free). Cleanup appears to have occurred. Migration
disruption risk remains due to 4 active Claude rebase agents and 6
Podman containers, but the immediate disk-failure urgency is reduced.

## Learning Loop 3

### active-work-snapshot (update 3)

Swap fully exhausted (8.0/8.0Gi used, 8.5Mi free) causing system
thrashing. Load average 94.61 (was 72 in prior update), 59 Claude
processes (was 45), 33 concurrent Go compiler/linker processes. The
rebase has progressed to the Go test compilation phase. Disk at 84%
(39G free, down from 44G in update 2).

### data-integrity (update)

Revised aggregate totals from 130-repo scan: approximately 500+
unpushed commits across 200+ branches in 30+ repositories (prior
estimate was 88 commits across 14 repos). Key repos with larger
exposure than previously documented: submariner-operator has 40+
unpushed branches including a 108-commit bundle-update branch,
shipyard has 30+ unpushed branches plus 100+ untracked local branches,
lighthouse has 30+ unpushed branches, submariner has 25+ unpushed
branches. enhancements repo has 6 unpushed commits on devel including
the SEP modernization proposal (not previously identified).
CREDENTIAL RISK: stolostron/deploy contains an untracked file
prereqs/quay_dfarrell_secret.yml which may contain registry
credentials -- do not include in any bulk migration copy.

### system-health

/tmp holds 3.5G reclaimable space (not previously identified as a
cleanup target). For the plocate-updatedb failure caused by
advisory-database exceeding open-file limits, the simplest fix is
adding the path to PRUNEPATHS in /etc/updatedb.conf rather than
raising LimitNOFILE.

## Learning Loop 4

### active-work-snapshot (update 4)

System stabilizing: load average dropped from 94.61 to 20+, swap
from 100% exhausted to 56%, Claude processes from 59 to 24 (6
interactive + background rebase daemon). Disk at 87% (32G free).
K8s 1.36.2 rebase still in flight across multus-cni, CNO, CNCC,
ingress-node-firewall with two Podman golang:1.26 build containers.
Crisis conditions from LL3 have eased but migration should still
wait for rebase completion.

### data-safety (update 4)

Two previously unidentified repos with local-only branches (no
remote tracking): cncf/foundation (13 commits on subm_maints) and
submariner-diagnostics (2 commits on add-shellcheck-ci). These are
entirely on-disk with no upstream backup.

### chrome-extensions (update 2)

If user wants Fedora User Agent or GNOME Shell integration to
survive policy deployment, add their IDs to ExtensionInstallAllowlist
in chrome-security-policy.json: hojggiaghnldpcknpbciehjcaoafceil
(Fedora User Agent, active in Profiles 1-4),
gphhapmejobijbbhgpjhcjognlahblep (GNOME Shell integration, active
in Profile 5). Deployment trigger: policy will auto-deploy on next
`make all` because the csb-chrome-policies.json gate file is absent
from /etc/opt/chrome/policies/managed/.

### systemd-journal (update)

Recurring Chrome crashes: 3 instances over 3 months (SIGBUS/SIGILL).
Confirmed not hardware-related -- no MCE, EDAC, NVMe, thermal, or
GPU/DRM errors anywhere in the journal.

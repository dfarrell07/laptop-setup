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

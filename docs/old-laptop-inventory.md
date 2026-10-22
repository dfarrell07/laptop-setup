# Old laptop inventory and disposition

Metadata audit dated 2026-10-13. Paths are relative to the old home directory.
620 Git checkouts were found; 42 use `.git` files (worktrees/submodules),
26 have tracked changes. These are initial discovery counts, not current status
or transfer verification; the later report is recorded in
[the plan](old-laptop-migration-plan.md). Refresh both reports before the move.
Dispositions below describe the implemented automation or remaining manual transfer.

## Durable workspace origins

All direct `evpn` and `repos` checkouts were inspected. Existing manifest entries
are reused for duplicate origins. Destinations are defined in `group_vars/all/repos.yml`; new EVPN/RH groups use
`~/src/<category>/<owner>/<repo>`, while existing layouts are retained.
The EVPN context companion stays in the work category despite its personal owner.

| Old path | Origin | Disposition |
| --- | --- | --- |
| `evpn/evpn-on-cloud` | `git@git.example.internal:datucker/evpn-on-cloud.git` | Added to work defaults |
| `evpn/bootc-image-builder` | `https://github.com/osbuild/bootc-image-builder.git` | Added to work defaults |
| `evpn/bootc` | `https://github.com/containers/bootc.git` | Added to work defaults |
| `evpn/aap-konflux-pipelines` | `https://github.com/ansible-automation-platform/aap-konflux-pipelines.git` | Added to work defaults |
| `evpn/bootc-foundry` | `https://github.com/osbuild/bootc-foundry.git` | Added to work defaults |
| `evpn/mapt` | `https://github.com/redhat-developer/mapt.git` | Added to work defaults |
| `evpn/src/ansible-content-actions` | `https://github.com/ansible/ansible-content-actions` | Added to work defaults |
| `evpn/src/bgp-cloud-connector` | `https://github.com/openshift/bgp-cloud-connector` | Added to work defaults |
| `evpn/src/build-definitions` | `https://github.com/konflux-ci/build-definitions` | Reused manifest entry |
| `evpn/src/build-service` | `https://github.com/konflux-ci/build-service` | Added to work defaults |
| `evpn/src/container-build-catalog` | `https://github.com/konflux-ci/container-build-catalog` | Added to work defaults |
| `evpn/src/docs` | `https://github.com/konflux-ci/docs` | Reused manifest entry |
| `evpn/src/integration-service` | `https://github.com/konflux-ci/integration-service` | Added to work defaults |
| `evpn/src/network.offline_migration_sdn_to_ovnk` | openshift/network.offline_migration_sdn_to_ovnk | Added to work defaults |
| `evpn/src/release-service` | `https://github.com/konflux-ci/release-service` | Added to work defaults |
| `evpn/src/release-service-catalog` | `https://github.com/konflux-ci/release-service-catalog` | Reused manifest entry |
| `evpn/src/tekton-integration-catalog` | `https://github.com/konflux-ci/tekton-integration-catalog` | Added to work defaults |
| `evpn/src/release-service-utils` | `https://github.com/konflux-ci/release-service-utils` | Added to work defaults |
| `evpn/src/mintmaker-presets` | `https://github.com/konflux-ci/mintmaker-presets` | Added to work defaults |
| `evpn/src/multi-platform-controller` | `https://github.com/konflux-ci/multi-platform-controller` | Added to work defaults |
| `evpn/src/policy` | `https://github.com/conforma/policy` | Reused manifest entry |
| `evpn/src/rhtap-ec-policy` | `https://github.com/release-engineering/rhtap-ec-policy` | Reused manifest entry |
| `evpn/src/openperouter` | `https://github.com/openperouter/openperouter` | Added to work defaults |
| `evpn/src/openperouter-downstream` | `https://github.com/openshift-kni/openperouter` | Added to work defaults |
| `evpn/src/openshift-docs` | `https://github.com/openshift/openshift-docs` | Reused manifest entry |
| `evpn/src/aipcc-konflux-data` | `https://github.com/red-hat-data-services/aipcc-konflux-data` | Added to work defaults |
| `evpn/src/cs-bootc` | `https://gitlab.com/redhat/centos-stream/containers/bootc.git` | Added to work defaults |
| `evpn/src/prow` | `https://github.com/kubernetes-sigs/prow` | Added to work defaults |
| `repos/app-interface` | `git@git.example.internal:service/app-interface.git` | Added to work defaults |
| `repos/wg-agentic-sdlc` | `git@git.example.internal:global-engineering/wg-agentic-sdlc.git` | Added to work defaults |
| `repos/components` | `git@git.example.internal:bugzilla-data/components.git` | Added to work defaults |
| `repos/developer-guide` | `git@git.example.internal:developer-guide/developer-guide.git` | Added to work defaults |
| `repos/catalog` | `git@git.example.internal:catalog/catalog.git` | Added to work defaults |
| `repos/support-scripts` | `git@git.example.internal:tools.gss.redhat.com/support-scripts.git` | Added to work defaults |
| `repos/jenkins-csb` | `git@git.example.internal:ccit/jenkins-csb.git` | Added to work defaults |
| `repos/user-documentation` | `git@git.example.internal:models-corp/user-documentation.git` | Added to work defaults |
| `repos/product-configs` | `git@git.example.internal:cpaas-products/product-configs.git` | Added to work defaults |
| `repos/skills-hub` | `git@git.example.internal:dataverse/agentic-engineering/skills-hub.git` | Added to work defaults |
| `repos/managerkit` | `git@git.example.internal:ncredi/managerkit.git` | Added to work defaults |
| `repos/gobot-slack` | `git@git.example.internal:tme/gobot-mpp/gobot-slack.git` | Added to work defaults |
| `repos/kernel` | `git@git.example.internal:kernel-qe/kernel.git` | Added to work defaults |
| `repos/ccx-rules-ocp` | `git@git.example.internal:ccx/ccx-rules-ocp.git` | Added to work defaults |
| `repos/tailwind` | `git@git.example.internal:tailwind/tailwind.git` | Added to work defaults |
| `repos/sdlc-starter` | `git@git.example.internal:mmcgrath/sdlc-starter.git` | Added to work defaults |
| `repos/bragai` | `git@git.example.internal:bragaitools/bragai.git` | Added to work defaults |
| `repos/ocm-resources` | `git@git.example.internal:service/ocm-resources.git` | Added to work defaults |
| `repos/dataproduct-config` | `git@git.example.internal:dataverse/dataverse-config/dataproduct-config.git` | Added to work defaults |
| `repos/advisories` | `git@git.example.internal:releng/advisories.git` | Reused manifest entry |
| `repos/ai-skills-workshop` | `git@git.example.internal:redhat-ai-analysis/ai-skills-workshop.git` | Added to work defaults |
| `repos/aap-containerized-installer` | `git@git.example.internal:ansible/aap-containerized-installer.git` | Added to work defaults |
| `repos/gss-rules` | `git@git.example.internal:support-insights/gss-rules.git` | Added to work defaults |
| `repos/sushe-agent-tools` | `git@git.example.internal:cee-data-engineering/sushe-agent-tools.git` | Added to work defaults |
| `repos/sop` | `git@git.example.internal:konflux/docs/sop.git` | Added to work defaults |
| `repos/uhc-clusters-service` | `git@git.example.internal:service/uhc-clusters-service.git` | Added to work defaults |
| `repos/users` | `git@git.example.internal:konflux/docs/users.git` | Reused manifest entry |
| `repos/product-definitions` | `git@git.example.internal:prodsec/product-definitions.git` | Added to work defaults |
| `repos/ocp-edge` | `git@git.example.internal:ocp-edge-qe/ocp-edge.git` | Added to work defaults |
| `repos/org` | `git@git.example.internal:hybrid-platforms/org.git` | Added to work defaults |
| `repos/scripts` | `git@git.example.internal:pmyers/scripts.git` | Added to work defaults |
| `repos/service` | `git@git.example.internal:cpaas/service.git` | Added to work defaults |
| `repos/fedora-config` | `git@git.example.internal:jscarbor/fedora-config.git` | Added to work defaults |
| `repos/matrix.osci.redhat.com` | `git@git.example.internal:vrutkovs/matrix.osci.redhat.com.git` | Added to work defaults |
| `repos/documentation` | `git@git.example.internal:cpaas/documentation.git` | Added to work defaults |
| `repos/kab` | `git@git.example.internal:kab/kab.git` | Added to work defaults |
| `repos/ccs-ai-agentic-workflow` | `git@git.example.internal:ccs-ai/ccs-ai-agentic-workflow.git` | Added to work defaults |
| `repos/evpn-gateway-appliance-context` | `https://github.com/dfarrell07/evpn-gateway-appliance-context.git` | Added to work defaults |
| `repos/evpn-gateway-appliance` | `https://github.com/openshift/evpn-gateway-appliance.git` | Added to work defaults |

## Tracked local changes at initial discovery

This dated table identifies places to review, not an exhaustive transfer list.
Clean repositories can also contain unpushed branches, stashes or untracked files.
These entries include generated/test fixtures as well as real work. Review each before
discarding it; the scan did not read diffs or untracked file contents.

| Path | Branch | Changed tracked entries |
| --- | --- | --- |
| `notes` | `master` | 1 |
| `.dotfiles` | `master` | 5 |
| `go/src/submariner-io/.worktrees/issue-2635/subctl-release-0.22` | `validation-upgrade-baseline-0.22` | 1 |
| `go/src/open-cluster-management/submariner-addon` | `main` | 1 |
| `go/src/open-cluster-management/submariner-addon/_output/registration-operator` | `release-2.2` | 546 |
| `go/src/stolostron/deploy` | `master` | 5 |
| `go/src/github.com/submariner-io/submariner-operator` | `fix-devel-cves-2026-09-16` | 3 |
| `konflux/old_fbc` | `main` | 2 |
| `konflux/submariner-release-management` | `asdlc-jira-update-plan` | 7 |
| `konflux/fbc-failure-fix-work/release-gate` | `fix-fbc-completed-test-gate` | 6 |
| `konflux/fbc-failure-fix-work/ocp-5-tenant` | `fix-fbc-5-0-tenant-config` | 1 |
| `konflux/fbc-failure-fix-work/fbc-plan` | `fix-fbc-recovery-plan-links` | 1 |
| `konflux/fbc-failure-fix-work/plans-review` | `verify-fbc-recovery-plans` | 2 |
| `konflux/fbc-failure-fix-work/fbc-4-19-lifecycle` | `fix-fbc-4-19-required-lifecycle` | 12 |
| `ovnk` | `main` | 1 |
| `ovnk/openshift-eng/ai-helpers/plugins/k8s-rebase/test/.repos/ovn-org/ovn-kubernetes` | `bump1.36` | 12 |
| `ovnk/openshift-eng/ai-helpers/plugins/k8s-rebase/.work/claude-codex-compatibility/marketplace` | `k8s-rebase-skill` | 14 |
| `ovnk/openshift-eng/ai-helpers/plugins/k8s-rebase/.work/claude-codex-compatibility/step4-forward.ZjhKfO/repo` | `rebase` | 2 |
| ai-helpers k8s-rebase `.work/k8s-rebase-mixed-eval/followup-eval-clones/openshift/cluster-network-operator` | `bump1.36` | 2373 |
| `ovnk/openshift-eng/ai-helpers/.work/k8s-1.37/image-reference-probe/repo` | `master` | 1 |
| `ovnk/openshift-eng/ai-helpers/.work/k8s-1.37-verification/ingress-a/repo` | `bump1.37` | 1 |
| `ovnk/openshift-eng/ai-helpers/.work/k8s-1.37-serial/clones/cno` | `bump1.37` | 9878 |
| `ovnk/openshift-eng/ai-helpers/.work/k8s-1.37-serial/clones/ingress` | `bump1.37` | 6921 |
| `ovnk/openshift-eng/ai-helpers/.work/k8s-1.37-serial/clones/mcp2` | `bump1.37` | 6186 |
| `backup-notes-dir` | `master` | 3 |
| `notes-backup` | `master` | 3 |

## Additional repository dispositions

- Personal `patents`, `startups`, and legacy `dotfiles` origins exist on GitHub.
  Added with full history; legacy dotfiles remain reference material.
- Submariner `coastguard` and `submariner-diagnostics` were added. Existing
  shipyard, admiral, cloud-prepare, enhancements, releases, website and charts
  clones have personal fork remotes and active branches; retain full history.
- New Konflux integration, testing-farm, enterprise-contract, RHEL AI docs and
  QE context sources were added. Duplicate local clones can use one destination.
- `toc`, `ipset-go`, and `dependent-issues` are older secondary sources: record
  origins in the private migration inventory report and copy local refs as
  needed; do not make them mandatory work provisioning dependencies.
- Hundreds of Go dependencies and ai-helpers test/evaluation clones are
  reconstructable build/test inputs. Do not bulk-clone them during provisioning.

## Tools and applications

| Evidence | Disposition |
| --- | --- |
| npm Codex 0.162.1 | Pinned, integrity-locked user install and two seed configs |
| native Claude 2.1.296 | Existing installer; second personal instance and familiar launchers |
| npm Gemini 0.60.0 and Homebrew Gemini | Optional pinned npm install; choose one provider |
| native gcloud 588.0.0 | DNF SDK repo/install toggle; macOS work gcloud-cli cask; authenticate separately |
| Antigravity `~/.local/bin/agy` ELF | Preserve launcher; reinstall vendor CLI manually |
| pipx chirp 20240706 | Manual reinstall from original spec; transfer complete radio profiles |
| broken pipx jinjanator/pipeline-migration-tool | Reinstall against new Python, recover original package specs |
| user pip black, ruff, skillsaw, rpm-lockfile-prototype, gitlint | Optional pinned pipx tools |
| Homebrew Linux node/python/GCC/Gemini and their dependencies | Prefer managed distro packages; do not copy Cellar |
| go/bin generators and Prow commands | Rebuild from the owning repos; do not copy old toolchains |
| /usr/local grype, doctoc, markdown-link-check | Optional tool gap; project locks or vendor install |
| RPM chatgpt desktop, browser variants, Slack, ExpressVPN | Vendor reinstall; Chrome toggle; Slack manual on Linux, cask on macOS |
| Ledger/Trezor/Daedalus user apps, browser PWAs | Manual reinstall and independent recovery material |
| RH VPN/Wi-Fi NetworkManager profiles | Protected out-of-band export; machine-specific secrets |
| user services and cron | No user crontab or custom enabled user daemon found |
| Podman two remaining containers, Vagrant storage | Review volumes/VMs separately; do not copy runtime storage live |

## Desktop and shell

- GNOME uses Adwaita, Adwaita Sans 11, tap-to-click, and the Fedora background
  extension. The desktop role now manages the selected portable preferences.
- i3 has a stock config plus older `.dotfiles/i3_config`/`i3status.conf` assets.
  Capture both; keep the modern desktop role and configure modifier/font as needed.
- Dock display repair hardcodes old connector names and Bluetooth aliases
  hardcode device addresses. Put replacement-specific commands in local zsh config.
- Portable additions: `~dl`, parent directory aliases, ping/mtr helpers, `gv`, `gi`,
  `GREP_OPTIONS` cleanup, `~/.npm-global/bin` for older user installations.
- Legacy `.zshrc` sources Vertex globally, uses obsolete `/home/daniel` paths,
  and forces old Vagrant storage. New launchers isolate backend/account state.

## AI state

- Work Codex: model `gpt-6.1-sol`, effort `medium`. Personal: `gpt-6-luna`,
  effort `high`. Preserve these observed choices as configurable seed values.
- Codex plugins: browser, chrome, visualization, documents, PDF, spreadsheets,
  presentations, template-creator, and ai-helpers k8s-rebase. Bundled marketplace
  paths are app-managed; ai-helpers points at a temporary work clone. Reinstall
  bundled plugins in the app and register a durable ai-helpers checkout.
- Codex node_repl uses `/usr/lib/chatgpt/resources/cua_node/`; reinstall desktop
  before enabling it. Never seed that machine-specific MCP path in CLI config.
- Claude marketplaces: anthropics/claude-plugins-official, submariner-io/shipyard,
  stolostron/submariner-release-management, openshift-eng/ai-helpers and personal
  claude-skills. Plugins include release-management, shipyard, Jira, Go and LSPs.
  New account settings now declare the personal skill repo; the isolated work home
  adds shipyard/release plugins and ai-helpers. Existing settings remain untouched;
  review/install missing plugins in the client after authentication.
- Source AI homes are several GB (Codex ~4.3G + ~3.1G; Claude ~1.7G + ~150M).
  The backup helper can capture selected config/skills/Claude memory, but this rollout
  skips raw memory/history transfer. Skills stay in their owning repos.

Portable feedback from source memories is captured in
[the shared working agreements](../roles/common/files/agent-baseline.md): signed-off
incremental commits, autonomy, concrete questions, verified concise plans, owning
Make targets and appropriate scratch storage. Repo-specific push exceptions,
active branch names, project/person facts and transient bug reports do not become
global policy. No raw memory files were committed. Codex UI onboarding flags,
trusted hook hashes and temporary project paths are also excluded from seeds.

Codex instruction discovery follows the
[official AGENTS.md guide](https://learn.chatgpt.com/docs/agent-configuration/agents-md);
Claude installation targets follow the
[official setup guide](https://code.claude.com/docs/en/setup#install-a-specific-version).

## Final metadata cross-checks

A command-name-only frequency count of zsh history confirmed frequent use of
`gi`, `gv`, Git, Make, `oc`, Podman, `gh`, GCP, ACLI/Jira, rpm-lockfile-prototype,
and grype. No command arguments or history contents were recorded. Managed
packages already include shfmt; grype and the older Jira CLI remain optional
vendor/project tooling. SSH config has four host entries with key selection;
its contents are handled through the protected backup, not copied into Git.

A final AI-home root check found `~/.claude` is itself a local Git repository
(no remote, `master`, tracked `settings.json`) and contains an untracked 30 KB
`k8s-rebase-autofix.sh`. The inventory tool now includes config Git roots while
excluding their cache/plugin children; the script and saved Claude plans are
included in protected backup. Preserve `.claude` local Git refs separately. Codex
memory lives in `memories_1.sqlite`; the default backup excludes that database and the
session index (only `scripts/backup.sh --ai-history` copies them), keeping reusable lessons in the tracked baseline.

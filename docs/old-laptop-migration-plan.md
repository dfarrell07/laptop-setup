# Old laptop migration plan

Audit: 2026-10-13, Fedora 43 source laptop.
**Portable baseline capture and verification are complete; destination setup is pending.**
Provisioning will run later from the destination side for Linux and macOS.
The source laptop's packages and running services were not provisioned.

## Purpose and document map

Capture durable laptop configuration and development intent in Ansible; preserve
local work and account state through an explicit transfer. Discovery covered Git
roots/worktrees, shell/editor config, AI accounts/plugins, package provenance,
PATH conflicts, desktop preferences, services, storage and hardware exceptions.
Committed evidence excludes credential values, transcripts and private source.

- [Inventory](old-laptop-inventory.md): dated source paths, origins and dispositions.
- [Wider audit](old-laptop-second-pass.md): additional system findings and decisions.
- [Migration checklist](migration-checklist.md): ordered source backup, destination
  prerequisites, restore steps and acceptance checks. Use this for the actual move.
- [Config reference](../default.config.yml): available overrides; role defaults are
  authoritative. Dated inventories record source evidence and final dispositions.

## Delivered automation

| Area | Result | Implementation |
| --- | --- | --- |
| Repositories | 69 additions, EVPN/RH groups, durable sources and fork remotes | `group_vars/all/repos.yml`, `roles/git_repos` |
| Codex | Locked 0.162.1 install; model/effort and global instruction seeds | `roles/codex`, `make codex` |
| AI launchers | Isolated launchers and shared working agreements | `roles/claude`, `roles/dotfiles` |
| Shell/editor | Portable aliases, managed Go PATH precedence, preserved local zsh extension, Vim conveniences | `roles/dotfiles` |
| Tooling | Pinned optional tools, additive packages, native AWS and macOS work gcloud | `roles/packages` |
| Backup | Legacy confined dotfile links, SSH keys/config, selected application and AI state, optional AI history | `scripts/backup.sh` |
| Repeatable audits | Private Git/workstation metadata reports | `scripts/migration-inventory.py`, `scripts/workstation-audit.py` |
| GNOME/power | Typed preferences; guarded DNF power transitions | `roles/desktop`, `roles/system/tasks/power_management.yml` |

The manifest has 147 entries: personal 6, downstream 19, BPFman 6, OVN 38,
Konflux 19, EVPN 24 and RH-context 35. Work defaults select all categories;
personal defaults select personal and OVN. An explicit `make repos-<category>`
overrides that selection. Destinations follow the manifest, with existing layouts
retained; both EVPN project repos and active Submariner/release repos use full history.

Codex seeds model/effort and global instructions while preserving user files. Installation is skipped
when `codex_install_method: skip` or `packages_install_binaries: false`; the latter
is not an air-gapped provisioning mode. Claude user preferences are seed-only;
managed `settings.json` is reapplied. Default familiar launchers retain the
observed permission-bypass arguments; configured sandbox/deny settings must not
be described as guaranteed enforcement under those arguments.

## Deliberate transfer boundaries

- Cloning restores upstream repositories, not local branches, dirty/untracked
  files, reflogs, worktree relationships or Git LFS objects. Preserve these separately.
- Backup uses a fixed path list and selected directory trees, not a whole-home or
  system image. Custom account homes, symlinked skills, VPN/browser profiles,
  complete radio/wallet data and VM/container volumes need separate handling.
- Authenticate each AI/Git/cloud account; reinstall plugins from durable sources
  and vendor apps from their installers. Review old MCP/trusted-project paths.
- Retain managed Git TLS/directory-trust policies and narrow Go exceptions. Old
  global TLS/checksum bypasses, `/home/daniel` paths, monitor/device IDs and wake
  workarounds are not portable defaults.
- Storage layout, hardware rules and the power-stack choice belong to destination
  configuration. Package snapshots are evidence, not a desired package list.

## Validation evidence and limits

Local gates passed: `make lint`, `make syntax-check` and the complete
`LAPTOP_SETUP_TEST_TIMEOUT=180 make test-scripts` suite on this busy source.
The isolated Linux Codex download/repeat and shared CI assertions also passed;
large native archives use a disk-backed test fixture. VM/macOS converge and verify
playbooks passed syntax checks. Claude pin behavior uses a fixture installer.
Optional actionlint/zizmor were absent and skipped. Regression coverage includes
account/environment isolation, seed preservation, integrity locks, symlink backup,
Git state/redaction/worktrees, workstation reports, GoENV policy and GNOME preferences.
Power tests use fake package/service modules; GNOME uses isolated DBus/dconf.
Local macOS template checks ran on Linux. Subsequent
[PR CI for the initial squashed commit](https://github.com/dfarrell07/laptop-setup/actions/runs/38487030360)
passed all ten Molecule scenarios, including native macOS; real destination
power transactions, reboot behavior and internal authentication remain rollout gates.

Three independent reviewers challenged security/recovery, platform behavior and
completeness/complexity, followed by integration reviews. Their fixes cover fresh
macOS runtimes and PATH order, resolved Codex account/install separation and check
mode, exclusive symlink-safe backups, configurable Claude runtime paths and
Vertex routing, recovery across power-transition stages and native overrides, TLP-aware
smoke checks, and conservative public Go checksum guards. Regressions exercise
the production tasks, including startup failures, path aliases, backup collisions
and shell paths containing spaces/apostrophes. The repository manifest and CI
selection were checked without adding unrelated provisioning features.
A fresh expert panel then reproduced and fixed unreadable backup sources,
composite-URI credential redaction, the second personal Claude home's path guard,
invalid Codex model seeds, public dependency checksum exceptions, and TLP
reconfiguration recovery. Power regressions distinguish active/exited oneshot
services from stopped-but-enabled units and reject unsafe configuration links.
Further review covered credential-directory permissions, malformed/unreadable
pipx reports, control characters in model overrides and restoration of running
masked native services. The inventory
now consistently excludes raw Codex memory and session history from this rollout.

Private source snapshots (not acceptance counts):

- Git report `~/migration-inventory-20261013-145105.json`: 621 roots, 24 unborn,
  27 with staged/modified files. Four unreadable generated kubelet fixture
  directories were reported as scan errors. The later `.claude` Git-root finding
  is supported by the final scanner; regenerate the report before transferring.
- Workstation report `~/workstation-audit-20261013-151839.json`: ten distinct
  PATH conflicts, two broken pipx environments, zero probe errors at that scan.
- Final backup dry run previewed 733 files. No actual credential backup was
  created by the audit; a dry run does not establish recoverability.

## Confirmed rollout and final baseline

This work captures automation for later destination setup; it does not provision
the source or create migration backups. Keep existing launcher arguments.
Do not transfer AI transcripts/session history or raw memory databases
(`scripts/backup.sh` copies them only with the opt-in `--ai-history`). Skills belong in their owning repositories;
retain useful cross-project lessons as concise tracked instructions, while
project facts and repository-specific publishing rules stay with their projects.

The final pass implemented:

1. Shared Claude/Codex working agreements, preserved Codex user instructions,
   and a repo-local guide without a fixed laptop-setup checkout path.
2. Seed-only Claude fullscreen UI and repository plugin declarations, scoped to
   the appropriate account; macOS work gcloud. Temporary paths/trust are excluded.
3. Native Linux power by default, retained native service overrides, explicit TLP.
4. Codex coverage in Linux/macOS provisioning scenarios; corrected Claude version
   targeting and verification; regression checks for retained launcher arguments.

Desktop/browser choices remain destination overrides. No destination config or
credentials will be inferred from this source. See the checklist for later setup.

## Remaining destination gates

1. Verify encrypted backups and local Git/worktree recovery before retiring the source.
2. Select destination overrides, reuse existing vault/hardware-key access, and
   establish GitHub/internal SSH/VPN authentication before repository provisioning.
3. Run provisioning at the destination, reboot, run smoke checks and inspect
   per-repository clone failures and desktop warnings even if the play succeeds.
4. Exercise real native package/power transactions and boot behavior on the
   destination or a disposable VM. The source audit did not validate these.
5. Verify each account/plugin and EVPN/release workflow with restored local work.

Follow the [checklist](migration-checklist.md) for commands and completion criteria.

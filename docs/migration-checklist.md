# Laptop migration checklist

Prepare the automation on this source, then run it from the destination side for
Linux and macOS. Keep launcher arguments; skip AI history transfer and use
repo-owned skills plus the tracked instruction baseline. See [the plan](old-laptop-migration-plan.md)
for validation limits and [the inventory](old-laptop-inventory.md) for dated source
paths. Run destination provisioning at its local console. Keep the source until
all acceptance checks below pass.

## 1. Preserve source state

1. Run `make migration-inventory` and `make workstation-audit`. Both write private
   JSON reports in your home. Review probe errors and omissions; keep reports in
   encrypted storage. Git status counts do not establish that work was pushed.
2. Run `make backup-dry-run`, inspect copied paths and skipped-link warnings, then
   `make backup`. Transfer its timestamped home directory through your encrypted
   backup channel. The root has mode 0700; ordinary copied files lose group/other
   access, while GnuPG key/database copies preserve source modes. Keep it out of Git.
3. Check coverage against your actual account/config paths. The helper backs up
   fixed defaults, selected files/trees and top-level regular SSH files. Only
   named legacy dotfile links resolving inside `~/.dotfiles` are followed, including
   `.ssh/config`. Other links, nested SSH files and custom AI homes need separate
   transfer. It does not read Ansible overrides to discover custom account paths.
4. Default AI coverage includes selected config/credentials, local skills, Claude
   saved plans/project memory in `.claude`, `.claude-work`, `.claude-personal` and
   `.claude-per2`, plus selected Codex config in `.codex`/`.codex-personal`.
   Claude plugin registration files are captured only for `.claude`/`.claude-per2`;
   save other registrations separately. Symlinked skills are skipped.
5. This rollout skips AI transcripts/session databases and raw memory restoration;
   do not use the helper's optional `--ai-history` snapshot. Use the tracked baseline
   below and retain needed project facts in their owning repositories. For GnuPG,
   use a consistent export/backup separately if needed; a live keybox copy is not
   a verified snapshot.
6. For each important Git repository, preserve all local refs with
   `git bundle create <backup-file> --all` and verify with
   `git bundle verify <backup-file>` in that repository. Save staged/unstaged
   patches and untracked files separately, or copy the closed workspace. Bundles
   include stash refs, but exclude working files, reflogs and Git LFS objects;
   transfer required LFS objects separately. Unborn repositories need file copies.
7. For linked worktrees, retain the common Git directory and each working tree.
   A copied worktree alone has a broken `.git` pointer. Recreate worktrees from
   saved branches or copy the complete workspace and use
   `git worktree repair <moved-path>`. Review submodules and their Git storage separately.
8. Prioritize release-management/recovery worktrees, dotfiles/notes, OVN and
   ai-helpers work. Include clean repositories with local branches and the local
   `.claude` Git root. `notes-backup`/`backup-notes-dir` have no remote. Inspect
   generated fixtures before discarding them; the reports do not read their diffs.
9. Separately export needed VPN/Wi-Fi, browser, complete radio/wallet and VM/container
   data. The helper captures selected CHIRP config, not all radio profiles. Recreate
   Tailscale membership and hardware-key access; do not copy live agent sockets
   or live container storage. Wallet recovery material belongs outside this repo.
10. Verify the transferred backup and test recovery of important files/refs before
    proceeding. A dry-run count or successful clone is not proof of recoverability.

## 2. Prepare destination configuration and authentication

1. Follow [CLAUDE.md](../CLAUDE.md) for OS prerequisites, `make bootstrap`, identity,
   USB/hardware prerequisites and reboot requirements. Start from
   `default.config.yml`; merge reviewed source overrides into private `config.yml`.
   Set profile, desktop, timezone and identity; replace every `CHANGE_ME` sentinel.
   macOS supports `desktop_environment: aerospace`; Linux selects GNOME/i3/Sway.
   Claude native installation also requires a reviewed `claude_install_sha256`
   and `claude_pinned_version`. The role passes the version as an installer argument
   and verifies the result; installer hashing does not freeze later auto-updates.
2. Reuse your encrypted vault and reviewed `scripts/vault-pass.sh` with the existing
   hardware keys. After restoring the helper, use `chmod 700 scripts/vault-pass.sh`
   and refresh its local integrity file from the reviewed contents:
   `(cd scripts && sha256sum vault-pass.sh > vault-pass.sh.sha256)`.
   Verify decryption with `make vault-view` in a private terminal. **Do not run
   `make setup-yubikeys` just to move laptops**: it generates a new secret and can
   overwrite slot 2, invalidating access to the existing vault. Use that workflow
   only for an intentional key/secret setup. Keep `vault_*` values out of config.yml.
3. Native power management is the baseline (tuned/tuned-ppd on recent Fedora).
   TLP and charge thresholds require explicit `system_tlp_enabled: true`. Keep
   the native default unless the destination needs that change. See
   [transition limits](old-laptop-second-pass.md#power-transition-limits).
4. Connect RH VPN and configure `ssh_work_keyscan_hosts` for
   `gitlab.cee.redhat.com` with an independently verified fingerprint before work
   provisioning. Optionally set `git_repos_vpn_check_host` to that host; its check
   warns about connectivity and does not supply authentication. Existing internal
   access grants and the RH key are still required.
5. Run `make preflight`, then stage prerequisites before cloning: `make repos_dnf`
   on DNF hosts, `make packages`, `make ssh`, and `make dotfiles`, in that order.
   These targets still run common/preflight checks and may need sudo/hardware-key
   access. Restore the RH key if it is not vault-managed, using the configured
   `dotfiles_ssh_redhat_key_basename`. Authenticate with `gh auth login` and verify
   both GitHub SSH and internal Git access. Review old host entries instead of
   replacing managed `.ssh/config`; put compatible extras in `.ssh/config.local`.
   SSH uses the first obtained value for each setting, so late includes do not
   override already-set managed values.
6. Keep managed Git CA/directory-trust policy. Do not restore source
   `sslVerify=false`, `safe.directory=*` or `GONOSUMDB=*` as defaults. Review
   `.config/go/env`; work shells and Go installs use explicit no-match/private
   defaults. Deliberate private patterns belong in `dotfiles_goprivate` and
   `dotfiles_gonosumdb`; local shell overrides can change the effective policy.

## 3. Provision and recover workspaces

1. Run `make all` after authentication. Inspect failed-clone reports: the repo role
   can succeed when some clones fail. Reconnect VPN if system/network changes
   interrupted it, and rerun the affected category. Reboot and run `make smoke-test`.
   Check GNOME preference warnings/readback in a real desktop session and verify
   the selected power stack, suspend/resume and charging on the new hardware.
2. Work defaults include EVPN/RH-context; personal defaults exclude them. Explicit
   `make repos-evpn` or `make repos-rh-context` overrides profile category selection.
   An EVPN-only run does not clone shared Konflux/OVN references: also use
   `make repos-konflux`/`make repos-ovnk` when needed.
3. Default project paths are `~/src/evpn/openshift/evpn-gateway-appliance` (with a
   `fork` remote for your GitHub fork),
   `~/src/evpn/<github-user>/evpn-gateway-appliance-context`, and
   `~/src/rh-context/<internal-namespace>/<repo>`. The manifest defines other
   layouts. Provisioning leaves old source locations in place; it does not migrate
   their state. Import saved refs/files, repair worktrees and update absolute paths.
4. Recover optional tools through additive `packages_extra_dnf`,
   `packages_extra_apt`, `packages_extra_brew`, `packages_extra_pipx` and
   `packages_extra_npm` overrides; see [config examples](../default.config.yml).
   Native entries are package names; npm/pipx require explicit version pins.
   Only Codex has a committed transitive npm integrity lock. Choose one Gemini
   provider; `gper` launches the separate Antigravity `agy` vendor CLI.
5. Reinstall jinjanator, pipeline-migration-tool and CHIRP from their original
   specs with the new Python; rebuild the two broken source pipx environments. Restore needed ACLI/Jira/GCP/release config
   selectively. Reinstall vendor desktop apps and browser PWAs; Slack uses the
   supported Flatpak path on Linux and the work Homebrew cask on macOS. Avoid old
   Homebrew Cellars/toolchains and the source
   `.local/bin/aws` link that shadowed the native AWS package.

`make codex` runs common checks plus Codex; bootstrap/config/vault and npm
prerequisites still apply. `codex_install_method: skip` seeds config only.
`make offline` sets `packages_install_binaries: false`, skipping gated binary/npm
installs including Codex; native package installs, Git clones and other network
operations can still occur. It is not an air-gapped workflow.

## 4. Restore AI accounts and local preferences

The following homes are defaults; substitute configured paths in login/restore
commands and transfer custom homes separately.

| Launcher | Home/backend | Recovery |
| --- | --- | --- |
| `cwork` (work profile) | `.claude-work`, Vertex | Restore legacy Vertex env and authenticate GCP |
| `cper` | `.claude`, personal | Sign in separately |
| `cper2` | `.claude-per2`, personal | Sign in separately |
| `owork` (work profile) | `.codex` | `CODEX_HOME="$HOME/.codex" codex login` |
| `oper` | `.codex-personal` | `CODEX_HOME="$HOME/.codex-personal" codex login` |
| `gper` | Separate `agy` CLI | Vendor install and login |

`cwork` sources managed `~/.config/claude/work-env`, which sources the restored
`~/.config/claude-code-vertex/env.sh` when present, and forces Vertex. Old work
and first personal Claude launchers shared `.claude`; copy desired preferences
selectively into the new work home. `cw`/`ccp` tmux workflows remain available;
`ccp` uses the separate `.claude-personal` home.

For GCP/Vertex authentication, macOS work provisioning includes the
[gcloud-cli cask](https://formulae.brew.sh/cask/gcloud-cli). On supported DNF hosts,
set `repo_google_cloud_sdk: true` to use the existing SDK install/versionlock path;
RHEL CSB skips that external repository. Other Linux hosts need the
[vendor installation](https://docs.cloud.google.com/sdk/docs/install-sdk).
Authentication and private project/region settings remain outside Git.

Familiar launchers retain observed bypass arguments: Claude
`--dangerously-skip-permissions --plugin-dir .`, Codex
`--ask-for-approval=never --sandbox=danger-full-access`. Do not rely on managed
sandbox/deny config as an enforcement guarantee with those defaults. To omit
these arguments, set:

```yaml
dotfiles_claude_launcher_args: []
dotfiles_codex_launcher_args: []
```

Codex config and `AGENTS.md` are seeded once; existing user files are preserved.
Claude user preferences are also seeded once, including fullscreen UI and repository
marketplace declarations. Common personal skills are declared for each account;
shipyard/release plugins and ai-helpers sources are added only to the work home.
Review/install these sources in the client after authentication; declarations do
not certify a downloaded plugin or its hooks. Override `claude_personal_plugin_settings`
and `claude_work_plugin_settings` to change the seed. Keep skills in those repos.

The shared [working agreements](../roles/common/files/agent-baseline.md) are copied
into Codex homes and rendered into managed Claude instructions. Keep project facts
in project instructions rather than restoring machine-path-specific memory folders.
Review MCP/trusted-project paths in any restored preferences. Managed
Claude `settings.local.json` is reapplied, so put intended policy changes in
Ansible config. Reinstall bundled Codex plugins and desktop-dependent servers
through the app; register ai-helpers from its durable checkout. Existing Claude
settings are not merged with new defaults: add missing repository declarations
through the client if reusing those files. Old cached registrations are references,
not portable installed plugins.

Put hardware commands and personal aliases in `~/.config/zsh/local.zsh`, seeded
once and sourced last. `gi` retains forgit's meaning; `gri` is the old grep helper.
Opt into `dotfiles_legacy_grep_alias_enabled: true` only to override forgit's `gi`.
Review old `.zlogin` wake commands, display connectors, Bluetooth IDs and local
intel_vbtn/USB rules for the new hardware. GNOME preferences use typed
`desktop_gnome_*` overrides. If restoring Chrome MIME/PWA handlers on Fedora,
select `repo_google_chrome: true` and `desktop_wm_browser: google-chrome` before
provisioning; otherwise review saved MIME/launcher files for the chosen browser.

## 5. Acceptance before source retirement

- [ ] Encrypted backup is readable; important files, Git refs, untracked work,
  worktree relationships and required LFS/submodule state recover successfully.
- [ ] Destination has rebooted, smoke checks pass, and reported clone/desktop
  warnings are resolved; real power behavior is verified.
- [ ] Each needed AI/Git/cloud account and plugin works; EVPN and release workflows
  run with the restored local work and correct identity/remotes.
- [ ] Fresh Git/workstation reports are compared with the source. Every needed
  omission has a transfer, reinstall or deliberate discard decision; transient
  package/repository counts need not match.
- [ ] Browser/VPN/radio/wallet/VM data and hardware-key/vault access are verified.
  Keep encrypted recovery material after retiring the source.

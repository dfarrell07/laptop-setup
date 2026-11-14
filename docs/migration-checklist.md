# Laptop migration checklist

Prepare on the source laptop, provision from the destination at its local console, and keep the source until the
acceptance checks pass. Skip AI transcript and session history; restore AI tooling from the tracked baseline
(section 4). Provisioning itself is chunked: see [README.md](../README.md) and [rollout-plan.md](rollout-plan.md).
When agent sessions on both laptops help, follow [multi-machine.md](multi-machine.md): the old laptop's session
audits it for gaps the automation does not cover yet.

## 1. Preserve source state

1. Run `make migration-inventory` and `make workstation-audit`. Both write private JSON reports in your home;
   review probe errors and omissions and keep the reports in encrypted storage. Git status counts do not show
   that work was pushed.
2. Run `make backup-dry-run`, inspect the copied paths and skipped-link warnings, then `make backup`. Move its
   timestamped home directory (mode 0700) over an encrypted channel and keep it out of Git. Ordinary copied files
   lose group/other access; GnuPG key and database copies keep their source modes.
3. Check coverage against your real paths. The helper copies fixed defaults, selected files and trees, and
   top-level regular SSH files. It follows only named legacy dotfile links that resolve inside `~/.dotfiles`
   (including `.ssh/config`). When the checkout is under `~/` and the files exist, it also copies the repo's
   `config.yml`, `group_vars/all/vault.yml`, `scripts/vault-pass.sh` and `CLAUDE.local.md` (a plaintext vault or
   `vault-pass.sh` aborts the run). Other links, nested SSH files and custom AI homes need separate transfer.
4. AI coverage is selected config and credentials, local skills, Claude plans and project memory in `.claude`,
   `.claude-work`, `.claude-personal` and `.claude-per2`, selected Codex config in `.codex` and `.codex-personal`,
   and Antigravity config and file-stored logins in `.gemini` and `.gemini-per2`. Claude plugin registrations are captured for
   `.claude` and `.claude-per2` only, and symlinked skills are skipped. `.claude-personal` is retired: move anything
   wanted from it into `.claude-per2` and do not restore it as a home. Do not use the optional `--ai-history`
   snapshot; keep project facts in their owning repositories. For GnuPG, make a consistent export: a live keybox copy
   is not a verified snapshot.
5. For each important Git repository, run `git bundle create <file> --all` then `git bundle verify <file>`.
   Bundles include stash refs but not working files, reflogs or Git LFS objects: save patches and untracked
   files separately (or copy the closed workspace) and transfer LFS objects yourself. Unborn repositories need
   file copies. For linked worktrees keep the common Git directory and each working tree, then
   `git worktree repair <moved-path>`; review submodules and their storage separately. Include clean
   repositories that hold local branches, and repositories with no remote.
6. Export separately: VPN and Wi-Fi profiles, browser data, wallet and device profiles, VM and container data.
   Recreate Tailscale membership and hardware-key access; never copy agent sockets or live container storage.
   Keep wallet recovery material out of this repo.
7. Restore a sample of the backup (files and refs) on another path before continuing. A dry-run count or a
   successful clone is not proof of recoverability.

## 2. Prepare the destination

1. Follow the README quick start through `make bootstrap`, then `cp default.config.yml config.yml`; merge reviewed
   source overrides into it. The Claude native install also needs a reviewed `claude_install_sha256` and
   `claude_pinned_version`; the pin does not freeze later auto-updates.
2. Reuse the encrypted vault and a reviewed `scripts/vault-pass.sh` with your existing hardware keys. After
   restoring the helper, `chmod 700 scripts/vault-pass.sh` and refresh its integrity file:
   `(cd scripts && sha256sum vault-pass.sh > vault-pass.sh.sha256)`, then `make vault-init` (with an encrypted vault it only
   marks it skip-worktree, so git never commits it). Check with `make vault-view` in a private
   terminal. **Do not run `make setup-yubikeys` just to move laptops**: it generates a new secret and can
   overwrite slot 2, which locks you out of the existing vault. Keep `vault_*` values out of `config.yml`.
3. Power: native management (tuned) is the baseline. TLP and charge thresholds need `system_tlp_enabled: true`;
   switching stacks is best effort and needs working repositories.
4. Work profile: connect the corporate VPN and set `ssh_work_keyscan_hosts` for the internal Git host with an
   independently verified fingerprint. `git_repos_vpn_check_host` warns when its host is unreachable and skips the
   repos on that host for the run; existing access grants and your work SSH key (`dotfiles_ssh_redhat_key_basename`)
   are still required.
5. Choose chunks with `rollout_enable`, run `make preflight`, then stage prerequisites chunk by chunk, in this
   order: `make apply CHUNK=repos_dnf` (DNF hosts), `make apply CHUNK=packages`, `make apply CHUNK=ssh_client`,
   `make apply CHUNK=dotfiles`. Avoid `make ssh`: it also rewrites `~/.ssh/authorized_keys` to exactly the vault keys.
   Run `gh auth login`, then check GitHub and internal Git access over SSH. Review old host entries instead of
   replacing the managed `.ssh/config`; put compatible extras in `.ssh/config.local` (SSH keeps the first value
   it sees per setting, so late includes cannot override managed values).
6. Keep the managed Git CA and directory-trust policy: do not restore `sslVerify=false`, `safe.directory=*` or
   `GONOSUMDB=*`. Put deliberate private Go patterns in `dotfiles_goprivate` and `dotfiles_gonosumdb`.

## 3. Provision and recover workspaces

1. Apply the remaining chunks one at a time (`make status`, `make plan CHUNK=x`, `make apply CHUNK=x`,
   `make verify CHUNK=x`) or `make all`. The repo role succeeds when some clones fail: read its failed-clone
   report, reconnect the VPN if network changes dropped it, and re-run the affected category. Reboot, run
   `make smoke-test`, and check GNOME preference warnings, suspend/resume and charging in a real session.
2. Work defaults include the EVPN and internal-context repo categories; personal defaults exclude them. An
   explicit `make repos-evpn` or `make repos-rh-context` overrides the profile, and does not clone the shared
   `konflux` and `ovnk` categories: add `make repos-konflux` or `make repos-ovnk` when needed. Layouts are in
   `group_vars/all/repos.yml`. Provisioning leaves old source locations alone and does not migrate their state:
   import saved refs and files, repair worktrees and fix absolute paths.
3. Recover optional tools with the additive `packages_extra_dnf`, `packages_extra_apt` and `packages_extra_brew`
   overrides (package names; examples in [default.config.yml](../default.config.yml)). Reinstall pipx tools from
   their original specs with the new Python, restore vendor desktop apps and browser PWAs, and restore needed
   cloud and Jira config selectively. Do not carry over old Homebrew Cellars or toolchains.
4. `make codex` runs the common checks plus Codex (`codex_install_method: skip` only seeds config).
   `make offline` sets `packages_install_binaries: false`, skipping gated binary and npm installs including
   Codex; native packages, clones and other network operations still run, so it is not an air-gapped workflow.

## 4. Restore AI accounts and preferences

Homes below are defaults; substitute configured paths and transfer custom homes separately.

| Launcher | Home/backend | Recovery |
| --- | --- | --- |
| `cwork` (work profile) | `.claude-work`, Vertex | Restore the Vertex env and authenticate GCP |
| `cper` | `.claude`, personal | Sign in separately |
| `cper2` | `.claude-per2`, personal | Sign in separately |
| `owork` (work profile) | `.codex` | `vault_codex_api_key` then `make codex`, or `CODEX_HOME="$HOME/.codex" codex login` |
| `oper` | `.codex-personal` | `CODEX_HOME="$HOME/.codex-personal" codex login` |
| `gper` | `agy`, `.gemini`, login in the desktop keyring | Run `gper` and finish the browser sign-in |
| `gper2` (Linux) | `agy`, `.gemini-per2`, login in a 0600 file, not the keyring | Run `gper2` and finish the browser sign-in |

`cwork` sources the managed `~/.config/claude/work-env`, which sources a restored
`~/.config/claude-code-vertex/env.sh` when present and forces Vertex. For GCP, macOS work provisioning installs
the `gcloud-cli` cask; on DNF hosts set `repo_google_cloud_sdk: true` (any `csb_detected` host skips that
repository, RHEL CSB or hybrid Fedora CSB); other Linux hosts use the
[vendor install](https://docs.cloud.google.com/sdk/docs/install-sdk). Authentication and private project settings stay outside Git.

The launchers keep their old bypass arguments: Claude `--dangerously-skip-permissions`, Codex
`--ask-for-approval=never --sandbox=danger-full-access`, gper and gper2
`--mode=accept-edits --dangerously-skip-permissions`. Do not treat the managed sandbox and deny config as an
enforcement guarantee while they are in place. The old `--plugin-dir .` is gone: a session started in any other
directory no longer loads it as a plugin (skills, hooks, MCP servers, `bin/` on PATH). Inside a repository listed
in `dotfiles_claude_plugin_dev_dirs`, or a worktree of one, the launchers pass `--plugin-dir <its top level>`;
the default lists only `claude-skills` under `git_repos_base_dir`, so add other plugin checkouts you develop
(absolute paths; a checkout left at an old location does not match). To drop the bypass arguments:

```yaml
dotfiles_claude_launcher_args: []
dotfiles_codex_launcher_args: []
dotfiles_gemini_launcher_args: []
```

Codex config and Claude preferences (fullscreen UI, mobile notifications) are seeded once; existing user files are
kept and not merged with new defaults. The global working agreements (Claude `CLAUDE.md`, Codex `AGENTS.md`,
Antigravity `GEMINI.md`) are rendered from the shared [baseline](../roles/common/files/agent-baseline.md) on every
run (`AGENTS.md` and `GEMINI.md` only while `codex_enabled` / `antigravity_enabled`), so hand edits are overwritten:
move rules from restored copies into the baseline or a project's own instructions first. Managed Claude
`settings.json` is reapplied every run, so put policy changes in Ansible config. Repository marketplace and plugin declarations
(`claude_personal_plugin_settings`, plus `claude_work_plugin_settings` in the work home) are added when missing
and never change an entry already there. To opt out, disable the plugin (`claude plugin disable` writes
`"name@marketplace": false`); `claude plugin uninstall` and `claude plugin marketplace remove` delete the entry,
so the next `make claude` adds it back. To drop a declaration for good, override those variables in `config.yml`.
Declarations do not vouch for a downloaded plugin or its hooks: review and install them in each home after authenticating
(`CLAUDE_CONFIG_DIR=<home> claude plugin install <name@marketplace>`), and run `claude plugin update` there after
a release. Old cached plugin registrations are references, not portable installs.

Keep project facts in project instructions, not machine-specific memory folders. Review MCP and trusted-project paths in anything restored.

Put hardware commands and personal aliases in `~/.config/zsh/local.zsh` (seeded once, sourced last); `gi` keeps
forgit's meaning and `gri` is the old grep helper. Review old login-shell wake commands, display connectors,
Bluetooth IDs and udev rules for the new hardware. GNOME preferences use typed `desktop_gnome_*` overrides. To
install Chrome on Fedora, set `repo_google_chrome: true` (adds the vendor repo only) and add `google-chrome-stable` to
`packages_extra_dnf` before provisioning. Set `desktop_wm_browser: google-chrome` only for the Sway/i3 `$mod+Shift+b`
binding. The playbook configures no MIME or PWA handlers; `make backup` copies `~/.config/mimeapps.list`.

## 5. Acceptance before retiring the source

- [ ] The transferred backup is readable; files, Git refs, untracked work, worktrees and LFS/submodule state recover.
- [ ] The destination has rebooted, `make smoke-test` passes, and clone and desktop warnings are resolved.
- [ ] Each needed AI, Git and cloud account and plugin works, with the right identity and remotes.
- [ ] Fresh inventory and audit reports match the source: every omission is a transfer, reinstall or deliberate
  discard. Package and repository counts need not match.
- [ ] Browser, VPN, device and wallet data, and hardware-key and vault access are verified. Keep encrypted
  recovery material after retiring the source.

# Automatic updates

What updates itself on a provisioned Linux machine, how, and where to look when it fails. Every update job logs to the
journal, and the sway/i3 status bar shows **UPD** in red while any of them is in the failed state. None of them reboots: when a
kernel or core library update lands, the bar shows **REBOOT** and you reboot when it suits you.

## Updates itself

The dnf update set is `system_dnf_automatic_upgrade_type` (`default`: all updates).

| Software | Mechanism | Schedule | Logs |
| --- | --- | --- | --- |
| dnf/rpm packages (Firefox, Chrome, podman, ...) | `dnf5-automatic.timer`, all updates | daily | `journalctl -u dnf5-automatic` |
| Flatpak apps and runtimes (system installation) | `flatpak-update.timer` | daily | `journalctl -u flatpak-update` |
| Firmware metadata (LVFS) | `fwupd-refresh.timer` (`sys_baseline` chunk) | daily | `journalctl -u fwupd-refresh` |
| Allowlisted npm globals (Codex) | `laptop-user-update.timer`, latest | daily | `journalctl --user -u laptop-user-update` |
| pipx apps you installed yourself | `laptop-user-update.timer` (`pipx upgrade-all`) | daily | same |
| Dev container packages (distrobox) | `laptop-user-update.timer` (`distrobox upgrade`) | daily | same |
| Claude Code | its own updater (native install) | on use | `claude doctor` |
| Antigravity `agy` | its own updater | on use | `agy --version` |
| Browser extensions | the browser | browser's schedule | browser |

- RHEL CSB: IT applies OS updates (RHSM/Satellite), so dnf-automatic, the Flatpak timer and LVFS firmware are not set up.
  CSB Fedora gets them: IT's playbooks there update only their own package.
- On CSB Fedora, IT's compliance run also uses dnf. If it starts while dnf-automatic holds the dnf lock it can fail once; the
  bar then shows CSB FAIL until IT's next run (about four hours later).
- Codex lives in `~/.npm-global` (codex role), so its own update prompt and this timer update the copy the launchers run.
- npm: only `packages_user_updates_npm` (default `@openai/codex`) is updated, so a package installed from git or an
  internal registry is never replaced by a same-named public one; a newer prerelease is left alone.
- pipx apps the packages role pins (`packages_user_updates_pipx_skip`: yamllint, ansible-lint) are skipped.
- Per-user Flatpak installations (`flatpak --user`) are not covered; the automation installs none.
- GNOME Software also downloads updates in a GNOME session (`org.gnome.software download-updates`); it is not needed
  alongside dnf-automatic and does not run in sway.

## Stays manual, and why

| Software | Why not automatic | How to update |
| --- | --- | --- |
| Firmware/BIOS | Flashing needs a reboot and can fail on battery or a dock; it must be attended | `sudo fwupdmgr update` |
| Binaries in `/usr/local/bin` (oc, kubectl, helm, kind, ...) | SHA256-pinned; a new hash needs review | bump the pin, `make packages` |
| The role's Go tools (`~/go/bin`, `subctl*`, golangci-lint) | Version-pinned, `@latest` is forbidden | bump the pin, `make packages` |
| Other Go tools in `~/go/bin` (code generators) | Installed by project builds at the project's version | the project's Make target |
| Other npm globals | May come from git or a private registry; trusted names go in `packages_user_updates_npm` | `npm i -g <name>@latest` |
| yamllint, ansible-lint (pipx) | Pinned to match CI's linters | bump the pin, `make packages` |
| gitleaks in `~/.local/bin` | SHA256-pinned by `make bootstrap-hooks` | bump the pin, `make bootstrap-hooks` |
| krew and its plugins | Pinned manifests; krew is kept from self-upgrading | bump the pin, `make packages` |
| oh-my-zsh, forgit | Pinned to commit SHAs; oh-my-zsh's updater is disabled | bump the SHA, `make dotfiles` |
| google-cloud-cli, acli | Held by `dnf versionlock` | bump the lock, `make repos_dnf` |
| Dev container base image | Pinned by digest; packages inside update daily | bump the digest, `make container-rebuild` |
| Other container images | Pulled by the projects that use them; no container opts into `podman auto-update` | `podman pull` |
| Ansible collections | Vendored with checksums | `make vendor-collections` |
| Homebrew (macOS) | The LaunchAgent refreshes metadata only | `brew upgrade` |
| Cloned git repos | Not software; pulling could disturb work in progress | `make update` |
| IT's agents (Fleet `orbit`, Falcon, Splunk forwarder, CSB playbooks) | IT-managed | IT's own update channels |

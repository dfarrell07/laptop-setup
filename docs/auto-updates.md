# Automatic updates

What updates itself on a provisioned Linux machine, how, and where to look when it fails. Every update job logs to the
journal, and the status bar shows **UPD** in red while any of them is in the failed state. None of them reboots: when a
kernel or core library update lands, the bar shows **REBOOT** and you reboot when it suits you.

## Updates itself

The dnf update set is `system_dnf_automatic_upgrade_type` (`default`: all updates).

| Software | Mechanism | Schedule | Logs |
| --- | --- | --- | --- |
| dnf/rpm packages (Firefox, Chrome, podman, ...) | `dnf5-automatic.timer`, all updates | daily | `journalctl -u dnf5-automatic` |
| Flatpak apps and runtimes (system) | `flatpak-update.timer` | daily | `journalctl -u flatpak-update` |
| Firmware metadata (LVFS) | `fwupd-refresh.timer` | daily | `journalctl -u fwupd-refresh` |
| npm global packages, incl. Codex | `laptop-user-update.timer` (`npm update -g`) | daily | `journalctl --user -u laptop-user-update` |
| pipx apps | `laptop-user-update.timer` (`pipx upgrade-all`) | daily | same |
| Dev container packages (distrobox) | `laptop-user-update.timer` (`distrobox upgrade`) | daily | same |
| Claude Code | its own updater (native install) | on use | `claude doctor` |
| Antigravity `agy` | its own updater | on use | agy's updater status |
| Browser extensions | the browser | browser's schedule | browser |

Codex also shows an update prompt; `npm install -g @openai/codex` updates the same install the launchers run.

On RHEL CSB, IT applies OS updates (RHSM/Satellite) and dnf-automatic is not installed. On CSB Fedora it runs, because IT's
playbooks there update only their own package.

## Stays manual, and why

| Software | Why not automatic | How to update |
| --- | --- | --- |
| Firmware/BIOS | Flashing needs a reboot and can fail on battery or a dock; it must be attended | `sudo fwupdmgr update` |
| Binaries in `/usr/local/bin` (oc, kubectl, helm, kind, ...) | SHA256-pinned; a new hash needs review | bump the pin, `make packages` |
| Go tools in `~/go/bin`, `~/.local/bin/subctl*` | Version-pinned, `@latest` is forbidden | bump the pin, `make packages` |
| krew and its plugins | Pinned manifests; krew is kept from self-upgrading | bump the pin, `make packages` |
| oh-my-zsh, forgit | Pinned to commit SHAs; oh-my-zsh's updater is disabled | bump the SHA, `make dotfiles` |
| google-cloud-cli, acli | Held by `dnf versionlock` | bump the lock, `make repos_dnf` |
| Dev container base image | Pinned by digest; packages inside update daily | bump the digest, `make container-rebuild` |
| Other container images | Pulled by the projects that use them; no container opts into `podman auto-update` | `podman pull` |
| Ansible collections | Vendored with checksums | `make vendor-collections` |
| Homebrew (macOS) | The LaunchAgent refreshes metadata only | `brew upgrade` |
| Cloned git repos | Not software; pulling could disturb work in progress | `make update` |

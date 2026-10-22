# Wider workstation audit decisions

Second pass: 2026-10-13. Implementation is complete;
source packages/services were untouched. Overall validation and remaining gates
are recorded in [the migration plan](old-laptop-migration-plan.md); transfer
instructions live in [the checklist](migration-checklist.md).

## Evidence and disposition

| Source evidence at audit time | Implemented decision |
| --- | --- |
| Confined SSH config link; spare YubiKey `.priv`/`.pub` pairs | Back up link and top-level regular files; review skipped links |
| tmux/Vim, URL/terminal prefs, release helper | Back up selected state; manage Vim shortcuts; keep automation in its project |
| Old AWS shadows distro CLI; duplicate Go/lint tools | Report PATH; prefer managed Go tools and native AWS packages |
| Two pipx environments point at missing Python 3.13 | Report interpreter health; rebuild from original specs on the destination |
| 605 explicit RPMs include installer defaults | Record provenance; use additive lists and pinned optional tools |
| 24-hour clock, touchpad, workspaces, right-Alt | Manage typed GNOME prefs under platform guards |
| Fedora 43 uses tuned/tuned-ppd | Handle package conflicts; restore native stack after opt-out |
| Distro timers; LUKS/Btrfs/zram/boot layout | Report units/mount types; select layout at OS install |
| intel_vbtn/USB rules, dock connectors | Report ownership; review on destination |

Source AWS 2.13.7 shadowed native 2.37.0; Go 1.22.0 remained beside native
1.26.8, with three competing golangci-lint builds.

Work AWS packages are enabled on Fedora, Debian-family and macOS. RHEL requires
an explicit package choice from configured repositories. Package names in additive
lists are validated; old vendor binaries and Homebrew Cellars are not transferred
as managed installations.

## GoENV behavior

The source persistent GoENV file sets `GONOSUMDB=*`. Empty process exports fall
back to that file. Managed work shells and Go install tasks now use explicit
`none` when no private patterns are configured; GONOSUMDB inherits configured
GOPRIVATE if its own override is empty. Guards validate that effective exception
and reject broad/public-module patterns. Local shell extensions can deliberately
override it. Regressions query the real Go binary with a temporary GoENV file.

The audit emits policy flags from the default `~/.config/go/env`, excluding
private patterns/proxy credentials. It does not establish effective policy under
custom GOENV paths or later process/shell overrides.

## Power transition limits

Native distribution power management is the default, including laptops. Set
`system_tlp_enabled: true` only to select TLP and its charge-threshold configuration.
On supported DNF hosts, enabling TLP removes named conflicting native packages
before installing it, then masks their units and enables TLP. Package,
configuration and service failures attempt to recover the previous package names,
unit masks, actual activity and enablement. Failed TLP reconfiguration restores
the previous managed config and mode; nonregular config paths fail before mutation.
Native service overrides are retained. Opt-out
restoration runs only when the managed TLP config exists, preserving unmanaged
installations; failures attempt to restore TLP and retain its config for retry.
Recovery is best effort and needs working repositories; it does not preserve exact
package versions or provide an atomic transaction. RHEL CSB and containers skip
these transitions.

Tests exercise task decisions through fake package/service modules, including
install failure recovery. Real dependency resolution, service behavior and reboot
remain destination/VM gates; this document does not certify those transactions.

## Report scope

`make workstation-audit` writes a private metadata report and reports probe errors.
It collects native package metadata, selected npm/pipx/user-Python inventories,
PATH alternatives, enabled/masked unit names, mount targets/types, allowlisted
GNOME preferences and selected config paths/system-file owners. It does not
execute shadowed PATH alternatives; each probed tool runs once from its first absolute PATH entry. It is not a complete service-state,
storage, package-source or configuration backup. See the plan for dated snapshots.

Design references: [TLP Fedora installation](https://linrunner.de/tlp/installation/fedora.html),
[Fedora's native power stack](https://fedoraproject.org/wiki/Changes/TunedAsTheDefaultPowerProfileManagementDaemon),
[Go module environment](https://go.dev/ref/mod#environment-variables),
[Fedora AWS package](https://packages.fedoraproject.org/pkgs/awscli2/awscli2/),
[Debian AWS package](https://packages.debian.org/trixie/awscli).

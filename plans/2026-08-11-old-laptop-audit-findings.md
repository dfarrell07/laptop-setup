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

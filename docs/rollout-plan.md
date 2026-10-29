# Chunk selection

<!-- markdownlint-disable MD013 -->

How chunked provisioning works and how to roll it out on a managed corporate laptop (CSB). Intent and
constraints: [design-principles.md](design-principles.md). Undo steps: [rollout-recovery.md](rollout-recovery.md).

## Mechanism

- **Chunk**: a named slice of the playbook. Its name is also its Ansible **tag**.
- **Registry**: `group_vars/all/rollout.yml`, one ordered list (low risk first, system hardening last) with
  `desc`, `risk`, `sudo`, `requires` and `verify` (smoke-check names). Undo notes are one heading per chunk in
  the recovery sheet.
- **Enabled list**: `rollout_enable` in `config.yml`, `all` by default.
- **Tag-skip**: `scripts/rollout.py skip-tags` prints only the disabled chunk names. `scripts/verify-ansible-args.sh`
  appends them as `--skip-tags` (when any are disabled) to each `ansible-playbook` run that includes `site.yml`;
  every `make` Ansible target runs through it. Untagged prerequisites and top-level `always` guards still run. With
  the default no skip tags are generated, so behaviour is identical to a run without chunks.
- **Fail closed**: an `always` assert in Play 0 (`pre-flight.yml`) requires every disabled chunk to appear in
  `ansible_skip_tags`, so a bare `ansible-playbook` run cannot apply one. Before any sudo prompt, a pre-check
  rejects unknown names (with a "did you mean"), duplicates and unmet `requires`; the wrapper rejects `-e` overrides.
- **Box type** is the facts the repo already computes (`profile`, `csb_detected`, `is_apt`, `is_macos`, the
  container group); roles already no-op where they do not apply.
- **Commands**: `make status`, `make plan CHUNK=x`, `make chunk-x`, `make verify [CHUNK=x]`. Existing targets
  keep working; `make minimal` and `make container` are tag-based aliases and the enabled list applies on top.
- **Sshd lockout guard**: the Play 0 pre-flight check (`pre-flight.yml`) blocks, over SSH, unless an ed25519 or
  FIDO2 key is in `authorized_keys` or the vault key will be deployed by this run; at the console it only prints a
  notice.
  `make verify` reports a missing key (`authorized-keys-ed25519`).

## Rolling out on a CSB laptop

1. Prerequisites: hooks and scanner (`make hooks && make bootstrap-hooks`), IT's compliance job green,
   `make backup` plus `dconf dump / > ~/dconf-before.ini`, a rehearsed break-glass (recovery sheet), and the
   `config.yml` defaults below.
2. Per chunk: `make plan CHUNK=x`, read it, `make chunk-x`, `make verify CHUNK=x`, soak, next.
3. System chunks only after a snapshot, a second session and the recovery sheet. Reboot checkpoints after
   `sys_baseline`, `sys_mounts`, `sys_lockdown`, `sys_usbguard`, `sys_remote_access`.
4. `ssh_authorized_keys` and `sys_remote_access` last, with a tested ed25519 or `ed25519-sk` key and a second
   session open. Tailscale needs no port rule (`tailscale0` joins the firewalld `trusted` zone in
   `roles/system/tasks/nm.yml`).

Suggested `config.yml` for such a machine (not the `default.config.yml` defaults): `desktop_environment: gnome`;
`repo_tailscale: false` (a YAML boolean) with `packages_extra_dnf: [tailscale]`; `repo_rpmfusion_free: false`;
`install_virtualization: false`; `install_vpn: false`; `system_tmout: 0`; `system_aide_enabled: false`;
`system_enable_ip_forward: false`; launcher args without the bypass flags (the defaults in
`roles/dotfiles/defaults/main.yml` include them, and the `.zshrc` template adds none; each list replaces the whole
default); third-party Claude plugin marketplaces off.

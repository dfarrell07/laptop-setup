.PHONY: status plan verify rollout-banner bootstrap-hooks help all minimal offline backup backup-dry-run bootstrap bootstrap-test lint check test smoke-test \
       dotfiles packages repos notes \
       ssh desktop system repos_dnf redhat containers claude codex container \
       container-rebuild csb-audit vault-edit vault-view vault-diff update hooks \
       smoke-test-container smoke-test-user \
       ci syntax-check shellcheck markdownlint commitlint check-vars-sync \
       test-scripts test-poller test-gitleaks test-% \
       repos-% \
       preflight guard-not-root guard-libvirt-group \
       pip-lock pip-sync npm setup-yubikeys vendor-collections migration-inventory workstation-audit usb-add aide-rebaseline \
       test-vm repos-dnf

# *** ORDERING CRITICAL — DO NOT MOVE THESE BLOCKS ***
# The make -n check MUST precede 'override MAKEFLAGS :=' because:
# override MAKEFLAGS := clears MAKEFLAGS to empty string; the ifneq after
# it sees empty MAKEFLAGS and NEVER fires — making the protection useless.
# This has been reverted 3 times. If you are an agent reading this: DO NOT
# move 'override MAKEFLAGS :=' before the ifneq check below.

# NOTE: GNU make has no -x option ('make -x' fails with "invalid option"), so no guard is
# needed here; shell tracing of the vault password is handled in the wrapper and vault-pass.sh
# (set +x, BASH_XTRACEFD/PS4/SHELLOPTS cleared). Only the first MAKEFLAGS word carries
# single-letter flags; long options such as --no-print-directory must not match.
_short_makeflags := $(filter-out --%,$(firstword -$(MAKEFLAGS)))

# Guard against dry-run bypass — 'make -n' prints but does not execute recipes, skipping VERIFY_AND_RUN and preflight.
# MUST be before 'override MAKEFLAGS :=' — see ordering note above.
ifneq ($(findstring n,$(_short_makeflags)),)
$(error ERROR: Provisioning with 'make -n' is forbidden — dry-run bypasses preflight and VERIFY_AND_RUN guards.)
endif

# Guard against MAKEFLAGS flag injection (CWE-426) — clears inherited MAKEFLAGS. MUST be AFTER the ifneq above.
override MAKEFLAGS :=

# Guard against SHELL variable injection (CWE-426) — 'make SHELL=/tmp/evil_sh all' causes every
# recipe line to be invoked via the attacker-controlled shell, bypassing verify-ansible-args.sh,
# preflight checks, and the SSTI guard with become:true (root) execution. The 'override' directive
# defeats command-line variable assignments, the same mechanism used to lock MAKEFLAGS above.
override SHELL := /bin/bash

# Bootstrap installs the Homebrew runtime and checksum tools used below.
# coreutils' gnubin provides the unprefixed sha256sum/timeout the recipes and scripts call.
ifeq ($(shell uname -s),Darwin)
export PATH := /opt/homebrew/opt/coreutils/libexec/gnubin:/usr/local/opt/coreutils/libexec/gnubin:/opt/homebrew/bin:/usr/local/bin:$(PATH)
endif

# Guard against BASH_ENV/ENV/ZDOTDIR shell-init injection (CWE-454/CWE-426).
# BASH_ENV=/tmp/evil.sh make all causes the Makefile recipe to spawn a bash process
# (via #!/usr/bin/env bash shebang) that sources BASH_ENV before any script code runs,
# executing the payload before the 50+ variable sanitization block in verify-ansible-args.sh.
# 'unexport' strips the variable from every child process Make spawns — load-bearing fix.
# ENV and ZDOTDIR are analogous vectors for sh/zsh child processes.
unexport BASH_ENV ZDOTDIR ENV NODE_OPTIONS NODE_PATH NPM_CONFIG_REGISTRY NPM_CONFIG_CACHE NPM_CONFIG_PREFIX CDPATH
unexport KUBECONFIG KUBE_CONFIG_PATH KUBE_CONTEXT KUBE_APISERVER KUBE_TOKEN KUBE_CERT_AUTH KUBE_CLIENT_CERT KUBE_CLIENT_KEY
unexport EDITOR VISUAL ANSIBLE_EDITOR
unexport PAGER MANPAGER SYSTEMD_PAGER GIT_PAGER GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES

CONTAINER ?= fedora-dev

define INSTALL_COLLECTIONS
env -u ANSIBLE_GALAXY_SERVER_LIST -u ANSIBLE_GALAXY_TOKEN ANSIBLE_GALAXY_SERVER=https://galaxy.ansible.com/ \
ansible-galaxy collection install -p ./collections \
	collections-dist/ansible-posix-2.2.2.tar.gz \
	collections-dist/community-general-13.5.0.tar.gz \
	collections-dist/community-library_inventory_filtering_v1-1.1.5.tar.gz \
	collections-dist/containers-podman-1.21.2.tar.gz
endef

# Verify collections integrity, validate ANSIBLE_COLLECTIONS_PATH, and reject dangerous flags.
# REQUIRED: guard against TOCTOU tampering and supply chain attacks.
# SECURITY: Rejects --start-at-task (bypasses Play 0 pre-flight checks) and
# --skip-tags=always (skips [always]-tagged security assertions). Does NOT block --tags
# or other --skip-tags values (needed for role-specific targets like make claude).
override VERIFY_AND_RUN := scripts/verify-ansible-args.sh

# Prevent ANSIBLE_COLLECTIONS_PATH injection (CWE-426) — guard against untrusted collections search path. REQUIRED: block environment variable override.
export ANSIBLE_COLLECTIONS_PATH := $(CURDIR)/collections:~/.ansible/collections:/usr/share/ansible/collections

# display_ok_hosts is a callback plugin option not in the core config schema;
# ansible-config validate rejects it in [defaults]. Use the env var instead.
# Shown by default: the playbook reports warnings and notices through debug tasks, which
# the default callback hides along with every other 'ok' result. QUIET=1 restores terse output.
export ANSIBLE_DISPLAY_OK_HOSTS = $(if $(QUIET),false,true)
# callback_result_format is a callback plugin option not in the core config schema;
# ansible-config validate rejects it in [defaults]. Use the env var instead.
export ANSIBLE_CALLBACK_RESULT_FORMAT = yaml

help:
	@echo "Start here: make status  (which chunks are on)  then  make plan CHUNK=<name>, make chunk-<name>, make verify CHUNK=<name>"
	@echo "Primary:    all minimal offline container container-rebuild update"
	@echo "Roles:      dotfiles packages repos notes ssh desktop system repos_dnf"
	@echo "            redhat containers claude codex"
	@echo "Repos:      repos-ovnk repos-konflux repos-personal repos-bpfman repos-downstream repos-evpn repos-rh-context"
	@echo "Testing:    lint ci test test-scripts test-poller test-fedora test-rocky test-debian test-macos test-vm test-container test-container-offline test-container-offline-distrobox test-distrobox-role test-packages-binaries test-ssti-guard test-gitleaks smoke-test smoke-test-container smoke-test-user check"
	@echo "Linting:    shellcheck markdownlint commitlint check-vars-sync syntax-check"
	@echo "Setup:      bootstrap bootstrap-hooks bootstrap-test hooks npm setup-yubikeys vendor-collections"
	@echo "Output:     QUIET=1 hides ok results, including debug warnings (e.g. make all QUIET=1)"
	@echo "USB:        usb-add  (allow blocked/connected USB devices now; save rules to config.yml)"
	@echo "AIDE:       aide-rebaseline  (accept current files as the baseline after expected changes, e.g. dnf upgrade)"
	@echo "Other:      backup backup-dry-run migration-inventory workstation-audit csb-audit vault-edit vault-view vault-diff pip-lock pip-sync preflight"

# --- Primary targets ---
# Safety guard: user-space role targets must not run as root (dotfiles would install to /root/)
guard-not-root:
	@[ "$$(id -u)" != "0" ] || \
		{ echo "ERROR: Do not run as root. Use -K for privilege escalation (make all)." >&2; exit 1; }

all: guard-not-root rollout-banner preflight
	$(VERIFY_AND_RUN) --run-log=all ansible-playbook site.yml --ask-become-pass

minimal: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,dotfiles,ssh,git_repos --skip-tags become

offline: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --ask-become-pass -e packages_install_binaries=false

container: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,distrobox --skip-tags become

container-rebuild: guard-not-root
	@printf 'Destroy and rebuild container $(CONTAINER)? Anything installed inside it (outside $$HOME) is lost. [y/N] '; \
		read -r a; [ "$$a" = y ] || [ "$$a" = Y ] || { echo "Aborted."; exit 1; }
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,distrobox -e container_replace=true --skip-tags become

backup: guard-not-root
	scripts/backup.sh

backup-dry-run: guard-not-root
	scripts/backup.sh --dry-run

# --- Bootstrap ---
# Fedora/RHEL: if make is not yet installed: sudo dnf install -y make
# macOS: Xcode CLT + Homebrew required first: xcode-select --install && /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

bootstrap-hooks: guard-not-root
	python3 scripts/bootstrap-hooks.py

bootstrap: guard-not-root bootstrap-hooks
	@# macOS: brew openssh links libfido2; system ssh (LibreSSL) lacks sk-ssh-ed25519/FIDO2 support
	@if [ "$$(uname -s)" = "Darwin" ]; then \
		brew install ansible git openssh libfido2 ykman make shellcheck coreutils node bash python3; \
	elif command -v apt-get >/dev/null 2>&1; then \
		sudo apt-get update && sudo apt-get install -y ansible git yubikey-manager make shellcheck python3-venv python3-pip; \
	else \
		sudo bash -c 'dnf install -y ansible-core git make && { dnf install -y ykpers yubikey-manager ShellCheck || echo "WARN: ykpers/yubikey-manager/ShellCheck unavailable (RHEL: install EPEL first; Fedora: check repo availability) — continuing without optional tools"; }'; \
	fi
	@# --- Integrity verification: reject tampered security-critical scripts ---
	@test -f scripts/vault-pass-ci.sh || { printf 'ERROR: scripts/vault-pass-ci.sh missing — restore with: git checkout scripts/vault-pass-ci.sh\n' >&2; exit 1; }
	@# Verify vault-pass-ci.sh integrity BEFORE copying (FATAL if check fails) — guards against supply chain tampering
	@cd scripts && sha256sum -c vault-pass-ci.sh.sha256 > /dev/null 2>&1 && echo "✓ vault-pass-ci.sh integrity verified" || { echo "ERROR: vault-pass-ci.sh failed integrity check — possible tampering. Run: (cd scripts && sha256sum vault-pass-ci.sh > vault-pass-ci.sh.sha256)" >&2; exit 1; }
	@if [ ! -f scripts/vault-pass.sh ]; then \
		cd scripts && cat vault-pass-ci.sh > vault-pass.sh && chmod 700 vault-pass.sh && sha256sum vault-pass.sh > vault-pass.sh.sha256 && cd .. && \
		echo "SECURITY: Created stub vault-pass.sh — you MUST replace with YubiKey/keyring/encrypted-file before encrypting vault.yml"; \
	elif [ ! -f scripts/vault-pass.sh.sha256 ]; then \
		(cd scripts && sha256sum vault-pass.sh > vault-pass.sh.sha256); \
	fi
	@chmod 700 scripts/vault-pass.sh scripts/vault-pass-ci.sh
	@# Verify vault-pass.sh integrity after creation/update (FATAL if check fails)
	@cd scripts && sha256sum -c vault-pass.sh.sha256 > /dev/null 2>&1 && echo "✓ vault-pass.sh integrity verified" || { echo "ERROR: vault-pass.sh failed integrity check — possible tampering or stale .sha256 file. If expected (after make setup-yubikeys), the hash file should have been automatically updated. Run: (cd scripts && sha256sum vault-pass.sh > vault-pass.sh.sha256)" >&2; exit 1; }
	@# Verify verify-collections.sh integrity (FATAL if check fails) — guards against supply chain tampering
	@cd scripts && sha256sum -c verify-collections.sh.sha256 > /dev/null 2>&1 && echo "✓ verify-collections.sh integrity verified" || { echo "ERROR: verify-collections.sh failed integrity check — possible tampering. Run: (cd scripts && sha256sum verify-collections.sh > verify-collections.sh.sha256)" >&2; exit 1; }
	@# Verify verify-ansible-args.sh integrity (FATAL if check fails) — guards against pre-flight check bypass
	@cd scripts && sha256sum -c verify-ansible-args.sh.sha256 > /dev/null 2>&1 && echo "✓ verify-ansible-args.sh integrity verified" || { echo "ERROR: verify-ansible-args.sh failed integrity check — possible tampering. Run: (cd scripts && sha256sum verify-ansible-args.sh > verify-ansible-args.sh.sha256)" >&2; exit 1; }
	@# Verify pre-commit hook integrity (FATAL if check fails) — guards against hook tampering
	@cd .githooks && sha256sum -c pre-commit.sha256 > /dev/null 2>&1 && echo "✓ pre-commit hook integrity verified" || { echo "ERROR: .githooks/pre-commit failed integrity check — possible tampering. Run: (cd .githooks && sha256sum pre-commit > pre-commit.sha256)" >&2; exit 1; }
	@# Verify verify-collections-galaxy.sh integrity (FATAL if check fails) — guards against supply chain verification bypass
	@cd scripts && sha256sum -c verify-collections-galaxy.sh.sha256 > /dev/null 2>&1 && echo "✓ verify-collections-galaxy.sh integrity verified" || { echo "ERROR: verify-collections-galaxy.sh failed integrity check — possible tampering. Run: (cd scripts && sha256sum verify-collections-galaxy.sh > verify-collections-galaxy.sh.sha256)" >&2; exit 1; }
	cd collections-dist && sha256sum -c SHA256SUMS
	@# Verify collections against Galaxy API to detect supply chain tampering
	@scripts/verify-collections-galaxy.sh
	@echo ""
	@echo "Repository signing policy: .gpg-enforcement"
	@echo "  The bundled public key and checksum signature are placeholders."
	@echo "  GPG enforcement: $$(scripts/gpg-policy.sh); hashes and source checks remain active."
	@echo "  Set .gpg-enforcement to enabled after key/signature setup; see SECURITY.md."
	@echo ""
	$(INSTALL_COLLECTIONS)
	$(MAKE) npm
	$(MAKE) hooks
	@echo ""
	@echo "Bootstrap complete. Git hooks active."
	@echo ""
	@echo "Next steps:"
	@echo "  1. Create config.yml (REQUIRED before make all — do not skip):"
	@echo "       cp default.config.yml config.yml"
	@echo "     Edit config.yml and replace each CHANGE_ME sentinel value:"
	@echo "       desktop_environment: sway   # Linux: sway/i3/gnome; macOS: aerospace"
	@echo "       dotfiles_github_user: 'yourusername'"
	@echo "       dotfiles_user_name: 'Your Name'"
	@echo "       dotfiles_user_email_work: 'you@company.com'"
	@echo "       dotfiles_user_email_personal: 'you@personal.com'"
	@echo "     Also set (default UTC, not a CHANGE_ME sentinel):"
	@echo "       system_timezone: America/Chicago   # timedatectl list-timezones"
	@echo "     Personal machine? also add: profile: personal  # skips work tooling; work email then optional"
	@echo "     Want notes provisioned? also add: notes_enabled: true  # opt-in; requires a GitHub repo named \"notes\" accessible as dotfiles_github_user/notes"
	@echo "     HiDPI display (e.g. ThinkPad P16v 2560x1600):"
	@echo "       desktop_sway_hidpi_scale: \"1.5\""
	@echo "  2. GPG signing follows .gpg-enforcement ($$(scripts/gpg-policy.sh)):"
	@echo "     - If you don't have a GPG key: gpg --full-generate-key"
	@echo "     - To enforce signing: set .gpg-enforcement to enabled and configure commit.gpgsign"
	@echo "     - For details: see SECURITY.md § GPG Key Requirements for Supply-Chain Commits"
	@echo "  3. Run make setup-yubikeys — programs your YubiKeys with the same HMAC-SHA1 secret,"
	@echo "     writes scripts/vault-pass.sh, and shows next steps including vault encryption."
	@echo "     (For manual vault-pass.sh without hardware key, see SECURITY.md §Setting Up vault-pass.sh)"
	@echo "     (For first provision without real secrets, vault.yml plaintext stub is fine.)"
	@echo "  4. make preflight   # validate all pre-conditions before provisioning"
	@echo "     make check       # full sudo dry run; review the diff before provisioning"
	@echo "  5. make all         # full provisioning (run at local console or inside tmux)"
	@echo "  6. Reboot — kernel hardening (lockdown/IOMMU) and SSH port 722 only take effect after reboot"
	@echo "  7. After reboot — required manual steps:"
	@echo "       make smoke-test              # verify provisioning succeeded"
	@echo "       tailscale up                 # authenticate Tailscale (browser step)"
	@echo "       gh auth login                # GitHub CLI auth (required for HTTPS git credential helper)"
	@echo "       podman login registry.redhat.io  # work profile only, if oc is installed"
	@echo "       Log out and back in for libvirt/kvm group membership changes"
	@echo "       make container               # CSB/hybrid machines only: provision distrobox dev container"
	@echo "     See CLAUDE.md 'Quick Reference' step 6 for post-provisioning details."
	@echo ""

# Fedora 41+ runs modular libvirt daemons (virtqemud.service Conflicts=libvirtd.service), so their
# sockets are enabled instead of the monolithic libvirtd. vagrant-libvirt uses qemu:///system;
# without the libvirt group every call needs a polkit admin prompt. gpasswd resolves SSSD/IdM
# users through NSS (usermod cannot). The box download is skipped when already added.
# molecule/vm uses its own 'laptop-setup' storage pool under /var/lib/libvirt/images: on CSB
# laptops IT points the 'default' pool at ~/VirtualMachines, which qemu cannot traverse into.
bootstrap-test: guard-not-root
	sudo dnf install -y libvirt vagrant vagrant-libvirt
	sudo systemctl enable --now virtqemud.socket virtnetworkd.socket virtstoraged.socket virtproxyd.socket
	@sudo virsh -c qemu:///system pool-info laptop-setup >/dev/null 2>&1 || { \
		sudo virsh -c qemu:///system pool-define-as laptop-setup dir --target /var/lib/libvirt/images/laptop-setup && \
		sudo virsh -c qemu:///system pool-build laptop-setup; }
	@sudo virsh -c qemu:///system pool-info laptop-setup | grep -q '^State: *running' || sudo virsh -c qemu:///system pool-start laptop-setup
	sudo virsh -c qemu:///system pool-autostart laptop-setup
	@# The monolithic libvirtd sockets conflict with the modular ones above (Conflicts=); keep them off.
	sudo systemctl disable --now libvirtd.socket libvirtd-ro.socket libvirtd-admin.socket
	@if getent group libvirt | cut -d: -f4 | tr , '\n' | grep -qx "$$USER"; then :; else \
		sudo gpasswd -a "$$USER" libvirt && echo "Added $$USER to the libvirt group: log out and back in before make test-vm"; fi
	@if vagrant box list 2>/dev/null | grep -q '^fedora/44-cloud .*libvirt'; then echo "Vagrant box fedora/44-cloud already present"; else \
		vagrant box add --name fedora/44-cloud --provider libvirt \
			--checksum-type sha256 --checksum 4695d5ef2bfcc882649b98de0c545d04671d2064f6609e7e68e606718c8333dc \
			https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Vagrant-libvirt-44-1.7.x86_64.vagrant.libvirt.box; fi

hooks:
	git config --local core.hooksPath .githooks
	@echo "Git hooks installed (core.hooksPath = .githooks)"

npm:
	@if command -v npm >/dev/null 2>&1; then \
		npm ci --ignore-scripts; \
	else \
		echo "NOTE: npm not found — install nodejs for commitlint hooks"; \
	fi

vendor-collections: guard-not-root
	@mkdir -p collections-dist
	@# SECURITY: Download collection tarballs from Galaxy and cross-verify SHA256 against
	@# the Galaxy API metadata endpoint (artifact.sha256 field). NOTE: This is NOT independent
	@# verification — both CDN tarball and API metadata are galaxy.ansible.com infrastructure.
	@# If Galaxy is compromised, attacker controls both CDN and API, defeating this check.
	@# Mitigations: version pinning, mandatory code review, CI integrity checks, runtime verification.
	@# See SECURITY.md § "Ansible Collections Maintainer Identity Verification" for details.
	@# Run when bumping versions in requirements.yml, then update SHA256SUMS and git commit.
	env -u ANSIBLE_GALAXY_SERVER_LIST -u ANSIBLE_GALAXY_TOKEN ANSIBLE_GALAXY_SERVER=https://galaxy.ansible.com/ \
		ansible-galaxy collection download -r requirements.yml -p collections-dist/
	@echo "Cross-verifying downloads against Galaxy API artifact.sha256 metadata..."; \
	GALAXY_API="https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/index"; \
	_fail=0; \
	for spec in \
		"ansible/posix/2.2.2/ansible-posix-2.2.2.tar.gz" \
		"community/general/13.5.0/community-general-13.5.0.tar.gz" \
		"community/library_inventory_filtering_v1/1.1.5/community-library_inventory_filtering_v1-1.1.5.tar.gz" \
		"containers/podman/1.21.2/containers-podman-1.21.2.tar.gz"; do \
		ns=$$(echo "$$spec" | cut -d/ -f1); \
		name=$$(echo "$$spec" | cut -d/ -f2); \
		ver=$$(echo "$$spec" | cut -d/ -f3); \
		file=$$(echo "$$spec" | cut -d/ -f4); \
		api_hash=$$(curl -sf "$${GALAXY_API}/$${ns}/$${name}/versions/$${ver}/" \
			| python3 -c "import sys,json; print(json.load(sys.stdin)['artifact']['sha256'])" 2>/dev/null); \
		dl_hash=$$(sha256sum "collections-dist/$${file}" | awk '{print $$1}'); \
		if [ -z "$$api_hash" ]; then \
			echo "  FAIL $${file}: could not fetch Galaxy API hash; manual verification required" >&2; \
			_fail=1; \
		elif [ "$$api_hash" = "$$dl_hash" ]; then \
			echo "  PASS $${file}"; \
		else \
			echo "  FAIL $${file}: CDN hash ($$dl_hash) != Galaxy API hash ($$api_hash)" >&2; \
			_fail=1; \
		fi; \
	done; \
	[ "$$_fail" = "0" ] || exit 1
	cd collections-dist && sha256sum *.tar.gz > SHA256SUMS
	@echo "Regenerating Python manifest for supply chain verification..."
	python3 scripts/gen-collection-manifest.py collections-dist collections-dist/PYTHON_MANIFEST.json
	@echo ""
	@echo "Tarballs downloaded, API-verified, and manifest generated."
	@echo ""
	@echo "NEXT STEP (REQUIRED): Verify vendored tarballs against upstream GitHub sources:"
	@echo "  scripts/verify-vendor-tarball.sh"
	@echo ""
	@echo "Then review and commit: git diff collections-dist/"
	@echo "           git add collections-dist/ && git commit -s"

setup-yubikeys: guard-not-root
	scripts/setup-yubikeys.sh

# With USBGuard running: allow blocked USB devices now and save their rules. Otherwise: only save
# rules for connected devices no rule covers yet. Rules go to config.yml for the next make system.
usb-add: guard-not-root
	python3 scripts/usb-add.py

# Accept the current filesystem as the AIDE baseline after expected changes (dnf upgrade,
# new tools). aide --update exits 1-7 when it found changes; 8+ is an error.
aide-rebaseline: guard-not-root
	@# Review the changes aide reports before accepting them: anything listed becomes trusted.
	sudo sh -c 'cd /var/lib/aide && aide --update; rc=$$?; \
		if [ $$rc -ge 8 ]; then rm -f aide.db.new.gz; echo "aide --update failed (exit $$rc); baseline unchanged" >&2; exit 1; fi; \
		printf "Accept the changes above as the new baseline? [y/N] "; read -r a; \
		case "$$a" in y|Y) cp -p aide.db.gz aide.db.gz.prev && mv aide.db.new.gz aide.db.gz \
			&& echo "Baseline updated (previous kept as /var/lib/aide/aide.db.gz.prev).";; \
		*) rm -f aide.db.new.gz; echo "Baseline unchanged." >&2; exit 1;; esac'

update: guard-not-root rollout-banner preflight
	$(MAKE) npm
	cd collections-dist && sha256sum -c SHA256SUMS
	$(INSTALL_COLLECTIONS)
	$(VERIFY_AND_RUN) ansible-playbook site.yml --ask-become-pass -e git_repos_pull=true

# --- Individual roles ---

# Targets without --ask-become-pass skip become-tagged tasks: the always-tagged system
# audits in the sudo play would otherwise call sudo with no password and fail.
dotfiles: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,dotfiles --skip-tags become

packages: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,packages --ask-become-pass

repos: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,git_repos --skip-tags become

notes: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,notes --skip-tags become

repos-%: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,git_repos -e repo_category=$* --skip-tags become

ssh: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,ssh --skip-tags become

desktop: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,desktop --ask-become-pass

system: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,system --ask-become-pass

repos-dnf:
	@echo 'ERROR: Did you mean: make repos_dnf (underscore) -- manages DNF package repositories' >&2 && exit 1

repos_dnf: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,repos_dnf --ask-become-pass

redhat: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,redhat --ask-become-pass

containers: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,containers --skip-tags become

claude: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,claude --ask-become-pass

codex: guard-not-root rollout-banner
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,codex --skip-tags become

# --- Audit and testing ---

workstation-audit: guard-not-root
	python3 scripts/workstation-audit.py --output "$(HOME)/workstation-audit-$$(date +%Y%m%d-%H%M%S).json"

migration-inventory: guard-not-root
	python3 scripts/migration-inventory.py --output "$(HOME)/migration-inventory-$$(date +%Y%m%d-%H%M%S).json"


preflight:
	scripts/preflight.sh

csb-audit: guard-not-root preflight
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common --check -v --skip-tags become

# Full dry run of every play, including the sudo system play; run before 'make all'.
# 'make check' and 'make all' log to ~/.cache/laptop-setup/logs/<target>-latest.log.
check: guard-not-root rollout-banner preflight
	$(VERIFY_AND_RUN) --run-log=check ansible-playbook site.yml --check --diff --ask-become-pass

# --- Chunks (registry: group_vars/all/rollout.yml; design: docs/rollout-plan.md) ---
# status: which chunks are on (config.yml rollout_enable). plan: preview one chunk (CHUNK=name). chunk-<name>: apply exactly one enabled chunk. verify: run only the smoke checks mapped to it.
# Validate rollout_enable (config.yml) first and print which chunks are skipped, before preflight or any sudo prompt.
rollout-banner:
	@if [ -x .venv/bin/python3 ]; then .venv/bin/python3 -I scripts/rollout.py banner || exit 2; fi

status: .venv
	@.venv/bin/python3 -I scripts/rollout.py status

plan: guard-not-root .venv rollout-banner
	@[ -n "$$CHUNK" ] || { echo "usage: make plan CHUNK=<name>  (the whole enabled list: make check; names: make status)" >&2; exit 2; }
	@args=$$(.venv/bin/python3 -I scripts/rollout.py chunk-args --preview "$$CHUNK") || exit 2; \
	  $(VERIFY_AND_RUN) ansible-playbook site.yml $$args --check --diff

# The chunk name reaches the recipe through the environment, never as shell text (no injection via the target name).
chunk-%: export ROLLOUT_CHUNK = $*
chunk-%: guard-not-root .venv
	@args=$$(.venv/bin/python3 -I scripts/rollout.py chunk-args "$$ROLLOUT_CHUNK") || exit 2; \
	  $(VERIFY_AND_RUN) ansible-playbook site.yml $$args && echo "Next: make verify CHUNK=$$ROLLOUT_CHUNK"

verify: .venv
	@.venv/bin/python3 -I scripts/rollout.py verify "$$CHUNK"

# NOTE: includes test-macos — requires macOS runner. On Linux use: make test
ci: lint syntax-check test-scripts test-poller test-gitleaks test-fedora test-rocky test-debian test-macos test-container test-container-offline test-container-offline-distrobox test-packages-binaries test-distrobox-role test-ssti-guard

lint: export PATH := $(CURDIR)/.venv/bin:$(PATH)
lint: .venv shellcheck markdownlint check-vars-sync
	@cd scripts && sha256sum -c verify-collections.sh.sha256 > /dev/null 2>&1 && echo "✓ verify-collections.sh integrity verified" || { echo "ERROR: verify-collections.sh failed integrity check — possible tampering"; exit 1; }
	@cd scripts && sha256sum -c verify-ansible-args.sh.sha256 > /dev/null 2>&1 && echo "✓ verify-ansible-args.sh integrity verified" || { echo "ERROR: verify-ansible-args.sh failed integrity check — possible tampering"; exit 1; }
	$(VERIFY_AND_RUN) .venv/bin/ansible-lint
	$(VERIFY_AND_RUN) .venv/bin/yamllint --strict .
	@if command -v actionlint >/dev/null 2>&1; then actionlint -color; else echo "SKIP: actionlint not installed (run: make packages)"; fi
	@if command -v zizmor >/dev/null 2>&1; then zizmor .github/; else echo "SKIP: zizmor not installed (run: make packages)"; fi

check-vars-sync: .venv
	$(VERIFY_AND_RUN) .venv/bin/python3 scripts/check-vars-sync.py

.venv: requirements-test.lock
	@test -x .venv/bin/python || python3 -m venv .venv
	@touch -t 197001010000 .venv  # stale until the final touch: a failed pip step is retried next run
	.venv/bin/pip install --no-deps --require-hashes -r requirements-bootstrap.txt
	.venv/bin/pip install --require-hashes -r requirements-test.lock
	touch .venv

# Regenerate the hash-pinned lockfile (run after editing requirements-test.txt)
pip-lock: .venv
	.venv/bin/pip-compile --generate-hashes --allow-unsafe --output-file=requirements-test.lock requirements-test.txt

# Sync .venv to match the lockfile exactly (removes packages not pinned in requirements-test.lock)
pip-sync: .venv
	.venv/bin/pip-sync requirements-test.lock

syntax-check: .venv
	$(VERIFY_AND_RUN) .venv/bin/ansible-playbook site.yml --syntax-check

shellcheck:
	shellcheck -S warning scripts/*.sh roles/claude/files/*.sh .githooks/pre-commit .githooks/commit-msg .githooks/prepare-commit-msg .githooks/pre-push roles/dotfiles/files/git-template-*

markdownlint:
	@if [ -d node_modules ]; then npm run lint:markdown; \
	else echo "SKIP: node_modules absent — run: npm ci --ignore-scripts"; fi

commitlint:
	@if [ -d node_modules ]; then npx --no -- commitlint --from origin/main --to HEAD; \
	else echo "SKIP: node_modules absent — run: npm ci --ignore-scripts"; fi

# Container-based molecule tests + script tests (Podman, no libvirt required).
# Matches CI molecule coverage (test-macos excluded — requires macOS runner).
# For VM tests: make test-vm (requires: make bootstrap-test first).
test: shellcheck test-scripts test-poller test-fedora test-rocky test-debian test-container test-container-offline test-container-offline-distrobox test-packages-binaries test-distrobox-role test-ssti-guard

# Syntax-check scripts/ (bash -n), then run the hook tests and scripts/test-*.py suites.
test-scripts: .venv
	bash -n scripts/preflight.sh
	bash -n scripts/smoke-test.sh
	bash -n scripts/backup.sh
	bash -n scripts/vault-pass-ci.sh
	bash -n scripts/test-queue-poller.sh
	bash -n scripts/verify-collections.sh
	bash -n scripts/verify-ansible-args.sh
	bash -n scripts/verify-collections-galaxy.sh
	bash -n scripts/test-hooks-security.sh
	bash scripts/test-hooks-security.sh
	.venv/bin/python3 scripts/test-config-parser.py
	.venv/bin/python3 scripts/test-pre-push.py
	.venv/bin/python3 scripts/test-vault-stub.py
	.venv/bin/python3 scripts/test-workflow-guards.py
	.venv/bin/python3 scripts/test-pinned-git.py
	.venv/bin/python3 scripts/test-hook-baselines.py
	.venv/bin/python3 scripts/test-commit-message.py
	.venv/bin/python3 scripts/test-bootstrap-hooks.py
	.venv/bin/python3 scripts/test-aerospace-terminal.py
	.venv/bin/python3 scripts/test-audit-policy.py
	.venv/bin/python3 scripts/test-brew-update.py
	.venv/bin/python3 scripts/test-chrony-template.py
	.venv/bin/python3 scripts/test-kernel-cmdline.py
	.venv/bin/python3 scripts/test-podman-distrobox.py
	.venv/bin/python3 scripts/test-ssh-known-hosts.py
	.venv/bin/python3 scripts/test-sshd-template.py
	.venv/bin/python3 scripts/test-sysctl-template.py
	.venv/bin/python3 scripts/test-rendered-configs.py
	.venv/bin/python3 scripts/test-module-args.py
	.venv/bin/python3 scripts/test-pam-faillock-debian.py
	.venv/bin/python3 scripts/test-critical-assertions.py
	.venv/bin/python3 scripts/test-claude-settings-merge.py
	.venv/bin/python3 scripts/test-smoke-test.py
	.venv/bin/python3 scripts/test-workstation-audit.py
	.venv/bin/python3 scripts/test-go-checksum.py
	.venv/bin/python3 scripts/test-laptop-migration.py
	.venv/bin/python3 scripts/test-claude-privacy.py
	.venv/bin/python3 scripts/test-ansible-input-guards.py
	.venv/bin/python3 scripts/test-usb-add.py
	.venv/bin/python3 scripts/test-collection-source-verifier.py
	.venv/bin/python3 scripts/test-rollout.py
	bash test-var-override.sh

# Regression checks for the pinned secret scanner configuration.
test-gitleaks:
	python3 scripts/test-gitleaks-config.py

# Unit tests for roles/claude/files/claude-queue-poller.sh internal helpers.
test-poller:
	scripts/test-queue-poller.sh

# The VM scenario talks to qemu:///system: fail early with the fix instead of a polkit error.
test-vm: export PATH := $(CURDIR)/.venv/bin:$(PATH)
test-vm: guard-libvirt-group .venv
	$(VERIFY_AND_RUN) .venv/bin/molecule test -s vm
guard-libvirt-group:
	@id -nG | tr ' ' '\n' | grep -qx libvirt || { echo "ERROR: $$USER is not in the libvirt group in this session. Run make bootstrap-test, then log out and back in." >&2; exit 1; }

test-%: export PATH := $(CURDIR)/.venv/bin:$(PATH)
test-%: .venv
	$(VERIFY_AND_RUN) .venv/bin/molecule test -s $*

smoke-test:
	scripts/smoke-test.sh

smoke-test-container:
	@echo "$(CONTAINER)" | grep -qE '^[a-zA-Z0-9_.-]+$$' || { echo 'ERROR: invalid CONTAINER name' >&2; exit 1; }
	scripts/smoke-test.sh --container "$(CONTAINER)"

smoke-test-user:
	scripts/smoke-test.sh --user-only

# --- Vault ---

vault-edit:
	EDITOR= VISUAL= ANSIBLE_EDITOR= $(VERIFY_AND_RUN) ansible-vault edit group_vars/all/vault.yml

vault-view:
	PAGER= ANSIBLE_PAGER= MANPAGER= SYSTEMD_PAGER= EDITOR= VISUAL= ANSIBLE_EDITOR= $(VERIFY_AND_RUN) ansible-vault view group_vars/all/vault.yml

vault-diff:
	PAGER= MANPAGER= SYSTEMD_PAGER= EDITOR= VISUAL= ANSIBLE_EDITOR= $(VERIFY_AND_RUN) bash -c \
		'old=$$(ansible-vault decrypt --output=- <(git show HEAD:group_vars/all/vault.yml)) \
			|| { echo "Cannot decrypt the committed vault.yml" >&2; exit 1; }; \
		new=$$(ansible-vault decrypt --output=- group_vars/all/vault.yml) \
			|| { echo "Cannot decrypt group_vars/all/vault.yml" >&2; exit 1; }; \
		diff <(printf "%s\\n" "$$old") <(printf "%s\\n" "$$new"); [ $$? -le 1 ]'

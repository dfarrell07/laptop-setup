.PHONY: help all minimal offline backup backup-dry-run bootstrap bootstrap-test lint check diff test smoke-test \
       dotfiles packages repos notes \
       ssh desktop system repos_dnf redhat containers claude distrobox container \
       container-rebuild csb-audit vault-edit update hooks \
       smoke-test-container smoke-test-user \
       ci syntax-check shellcheck markdownlint commitlint check-vars-sync \
       test-scripts test-poller test-% \
       repos-% \
       preflight guard-not-root \
       pip-lock pip-sync npm setup-yubikeys vendor-collections

# Guard against MAKEFLAGS environment variable injection (CWE-426)
# REQUIRED: prevent attacker-controlled MAKEFLAGS from disabling targets
override MAKEFLAGS :=

CONTAINER ?= fedora-dev

# Verify collections integrity and reject dangerous flags before ansible-playbook execution
# REQUIRED: guard against TOCTOU tampering (CVE-mitigation)
# SECURITY: Also rejects --start-at-task and --tags/--skip-tags to prevent bypassing
# pre-flight checks (see PATCH: Ansible pre_tasks bypassed with --start-at-task)
# Use exec to run ansible-playbook in the SAME process/shell context,
# preventing attacker from modifying collections between verify and import.
override VERIFY_AND_RUN := scripts/verify-ansible-args.sh

# Explicitly set ANSIBLE_COLLECTIONS_PATH to prevent environment variable override (CWE-426)
# REQUIRED: guard against ANSIBLE_COLLECTIONS_PATH environment variable injection
export ANSIBLE_COLLECTIONS_PATH := $(CURDIR)/collections:~/.ansible/collections:/usr/share/ansible/collections

# display_ok_hosts is a callback plugin option not in the core config schema;
# ansible-config validate rejects it in [defaults]. Use the env var instead.
export ANSIBLE_DISPLAY_OK_HOSTS = false
# callback_result_format is a callback plugin option not in the core config schema;
# ansible-config validate rejects it in [defaults]. Use the env var instead.
export ANSIBLE_CALLBACK_RESULT_FORMAT = yaml

help:
	@echo "Primary:    all minimal offline container container-rebuild update  (distrobox=alias for container)"
	@echo "Roles:      dotfiles packages repos notes ssh desktop system repos_dnf"
	@echo "            redhat containers claude"
	@echo "Repos:      repos-ovnk repos-konflux repos-personal repos-bpfman repos-downstream"
	@echo "Testing:    lint ci test test-scripts test-poller test-fedora test-rocky test-debian test-macos test-vm test-container test-container-offline test-container-offline-distrobox test-distrobox-role test-packages-binaries smoke-test smoke-test-container smoke-test-user check"
	@echo "Linting:    shellcheck markdownlint commitlint check-vars-sync syntax-check"
	@echo "Setup:      bootstrap bootstrap-test hooks npm setup-yubikeys vendor-collections"
	@echo "Other:      backup backup-dry-run csb-audit diff vault-edit pip-lock pip-sync preflight"

# --- Primary targets ---
# Safety guard: user-space role targets must not run as root (dotfiles would install to /root/)
guard-not-root:
	@[ "$$(id -u)" != "0" ] || \
		{ echo "ERROR: Do not run as root. Use -K for privilege escalation (make all)." >&2; exit 1; }

all: guard-not-root preflight
	$(VERIFY_AND_RUN) ansible-playbook site.yml --ask-become-pass
	@if command -v npm >/dev/null 2>&1 && [ ! -d node_modules ]; then \
		npm ci --ignore-scripts; \
	fi

minimal: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,dotfiles,ssh,repos --skip-tags become

offline: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --ask-become-pass -e packages_install_binaries=false

container: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,distrobox

container-rebuild: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,distrobox -e container_replace=true

backup: guard-not-root
	scripts/backup.sh

backup-dry-run: guard-not-root
	scripts/backup.sh --dry-run

# --- Bootstrap ---
# Fedora/RHEL: if make is not yet installed: sudo dnf install -y make
# macOS: Xcode CLT + Homebrew required first: xcode-select --install && /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

bootstrap: guard-not-root
	@# macOS: brew openssh links libfido2; system ssh (LibreSSL) lacks sk-ssh-ed25519/FIDO2 support
	@if [ "$$(uname -s)" = "Darwin" ]; then \
		brew install ansible git openssh libfido2 ykman make shellcheck; \
	elif command -v apt-get >/dev/null 2>&1; then \
		sudo apt-get update && sudo apt-get install -y ansible git yubikey-personalization make shellcheck python3-venv python3-pip; \
	else \
		sudo bash -c 'dnf install -y ansible-core git make; dnf install -y ykpers yubikey-manager ShellCheck || echo "WARN: ykpers/yubikey-manager/ShellCheck unavailable (RHEL: install EPEL first; Fedora: check repo availability) — continuing without optional tools"'; \
	fi
	@test -f scripts/vault-pass-ci.sh || { printf 'ERROR: scripts/vault-pass-ci.sh missing — restore with: git checkout scripts/vault-pass-ci.sh\n' >&2; exit 1; }
	@test -f scripts/vault-pass.sh || { cp scripts/vault-pass-ci.sh scripts/vault-pass.sh && echo "Created stub vault-pass.sh (replace with YubiKey version for real secrets)"; }
	@chmod 700 scripts/vault-pass.sh scripts/vault-pass-ci.sh
	@# Verify vault-pass.sh integrity after creation/update (FATAL if check fails)
	@cd scripts && sha256sum -c vault-pass.sh.sha256 > /dev/null 2>&1 && echo "✓ vault-pass.sh integrity verified" || { echo "ERROR: vault-pass.sh failed integrity check — possible tampering or stale .sha256 file. If expected (after make setup-yubikeys), the hash file should have been automatically updated. Run: sha256sum scripts/vault-pass.sh > scripts/vault-pass.sh.sha256" >&2; exit 1; }
	@# Verify verify-collections.sh integrity (FATAL if check fails) — guards against supply chain tampering
	@cd scripts && sha256sum -c verify-collections.sh.sha256 > /dev/null 2>&1 && echo "✓ verify-collections.sh integrity verified" || { echo "ERROR: verify-collections.sh failed integrity check — possible tampering. Run: sha256sum scripts/verify-collections.sh > scripts/verify-collections.sh.sha256" >&2; exit 1; }
	@# Verify verify-ansible-args.sh integrity (FATAL if check fails) — guards against pre-flight check bypass
	@cd scripts && sha256sum -c verify-ansible-args.sh.sha256 > /dev/null 2>&1 && echo "✓ verify-ansible-args.sh integrity verified" || { echo "ERROR: verify-ansible-args.sh failed integrity check — possible tampering. Run: sha256sum scripts/verify-ansible-args.sh > scripts/verify-ansible-args.sh.sha256" >&2; exit 1; }
	cd collections-dist && sha256sum -c SHA256SUMS
	ansible-galaxy collection install -p ./collections \
		collections-dist/ansible-posix-2.2.2.tar.gz \
		collections-dist/community-general-13.2.0.tar.gz \
		collections-dist/community-library_inventory_filtering_v1-1.1.5.tar.gz \
		collections-dist/containers-podman-1.20.2.tar.gz
	@if command -v npm >/dev/null 2>&1; then \
		npm ci --ignore-scripts; \
	else \
		echo "NOTE: npm not found — install nodejs for commitlint hooks"; \
	fi
	$(MAKE) hooks
	@echo ""
	@echo "Bootstrap complete. Git hooks active."
	@echo ""
	@echo "Next steps:"
	@echo "  1. Create config.yml (REQUIRED before make all — do not skip):"
	@echo "       cp default.config.yml config.yml"
	@echo "     Edit config.yml and replace each CHANGE_ME sentinel value — at minimum:"
	@echo "       desktop_environment: sway   # or i3 / gnome"
	@echo "       dotfiles_github_user: 'yourusername'"
	@echo "       dotfiles_user_name: 'Your Name'"
	@echo "       dotfiles_user_email_work: 'you@company.com'"
	@echo "       dotfiles_user_email_personal: 'you@personal.com'"
	@echo "       system_timezone: America/Chicago   # timedatectl list-timezones"
	@echo "     Personal machine? also add: profile: personal  # skips work tooling; work email then optional"
	@echo "     Want notes provisioned? also add: notes_enabled: true  # opt-in; requires a GitHub repo named \"notes\" accessible as dotfiles_github_user/notes"
	@echo "     HiDPI display (e.g. ThinkPad P16v 2560x1600):"
	@echo "       desktop_sway_hidpi_scale: \"1.5\""
	@echo "  2. Run make setup-yubikeys — programs your YubiKeys with the same HMAC-SHA1 secret,"
	@echo "     writes scripts/vault-pass.sh, and shows next steps including vault encryption."
	@echo "     (For manual vault-pass.sh without hardware key, see SECURITY.md §Setting Up vault-pass.sh)"
	@echo "     (For first provision without real secrets, vault.yml plaintext stub is fine.)"
	@echo "  3. make preflight   # validate all pre-conditions before provisioning"
	@echo "  4. make all         # full provisioning (run at local console or inside tmux)"
	@echo "  5. Reboot — kernel hardening (lockdown/IOMMU) and SSH port 722 only take effect after reboot"
	@echo "  6. After reboot — required manual steps:"
	@echo "       make smoke-test              # verify provisioning succeeded"
	@echo "       tailscale up                 # authenticate Tailscale (browser step)"
	@echo "       gh auth login                # GitHub CLI auth (required for HTTPS git credential helper)"
	@echo "       podman login registry.redhat.io  # work profile only, if oc is installed"
	@echo "       Log out and back in for libvirt/kvm group membership changes"
	@echo "       make container               # CSB/hybrid machines only: provision distrobox dev container"
	@echo "     See CLAUDE.md §6 for full details."
	@echo ""

bootstrap-test: guard-not-root
	sudo dnf install -y libvirt vagrant vagrant-libvirt
	sudo systemctl enable --now libvirtd
	vagrant box add githubixx/fedora-44 --provider libvirt

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
	ansible-galaxy collection download -r requirements.yml -p collections-dist/
	@echo "Cross-verifying downloads against Galaxy API artifact.sha256 metadata..."; \
	GALAXY_API="https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/index"; \
	_fail=0; \
	for spec in \
		"ansible/posix/2.2.2/ansible-posix-2.2.2.tar.gz" \
		"community/general/13.2.0/community-general-13.2.0.tar.gz" \
		"community/library_inventory_filtering_v1/1.1.5/community-library_inventory_filtering_v1-1.1.5.tar.gz" \
		"containers/podman/1.20.2/containers-podman-1.20.2.tar.gz"; do \
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

update: guard-not-root preflight
	@if command -v npm >/dev/null 2>&1; then \
		npm ci --ignore-scripts; \
	else \
		echo "NOTE: npm not found — install nodejs for commitlint hooks"; \
	fi
	cd collections-dist && sha256sum -c SHA256SUMS
	ansible-galaxy collection install -p ./collections \
		collections-dist/ansible-posix-2.2.2.tar.gz \
		collections-dist/community-general-13.2.0.tar.gz \
		collections-dist/community-library_inventory_filtering_v1-1.1.5.tar.gz \
		collections-dist/containers-podman-1.20.2.tar.gz
	$(VERIFY_AND_RUN) ansible-playbook site.yml --ask-become-pass -e git_repos_pull=true

# --- Individual roles ---

dotfiles: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,dotfiles

packages: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,packages --ask-become-pass

repos: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,repos

notes: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,notes

repos-%: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,repos -e repo_category=$*

ssh: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,ssh

desktop: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,desktop --ask-become-pass

system: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,system --ask-become-pass

repos-dnf:
	@echo 'ERROR: Did you mean: make repos_dnf (underscore) -- manages DNF package repositories' >&2 && exit 1

repos_dnf: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,repos_dnf --ask-become-pass

redhat: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,redhat --ask-become-pass

containers: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,containers --ask-become-pass

claude: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common,claude --ask-become-pass

distrobox: container  # alias for backwards compatibility

# --- Audit and testing ---

preflight:
	scripts/preflight.sh

csb-audit: guard-not-root preflight
	$(VERIFY_AND_RUN) ansible-playbook site.yml --tags common --check -v

check: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --check --diff --ask-become-pass

diff: guard-not-root
	$(VERIFY_AND_RUN) ansible-playbook site.yml --check --diff --tags dotfiles

# NOTE: includes test-macos — requires macOS runner. On Linux use: make test
ci: lint syntax-check test-scripts test-poller test-fedora test-rocky test-debian test-macos test-container test-container-offline test-container-offline-distrobox test-packages-binaries test-distrobox-role

lint: .venv shellcheck markdownlint check-vars-sync
	@cd scripts && sha256sum -c verify-collections.sh.sha256 > /dev/null 2>&1 && echo "✓ verify-collections.sh integrity verified" || { echo "ERROR: verify-collections.sh failed integrity check — possible tampering"; exit 1; }
	$(VERIFY_AND_RUN) .venv/bin/ansible-lint
	.venv/bin/yamllint --strict .
	@if command -v actionlint >/dev/null 2>&1; then actionlint -color; else echo "SKIP: actionlint not installed (run: make packages)"; fi
	@if command -v zizmor >/dev/null 2>&1; then zizmor .github/; else echo "SKIP: zizmor not installed (run: make packages)"; fi

check-vars-sync: .venv
	.venv/bin/python3 scripts/check-vars-sync.py

.venv: requirements-test.lock
	python3 -m venv .venv
	.venv/bin/pip install pip-tools==7.6.0
	.venv/bin/pip install --require-hashes -r requirements-test.lock
	touch .venv

# Regenerate the hash-pinned lockfile (run after editing requirements-test.txt)
pip-lock: .venv
	.venv/bin/pip-compile --generate-hashes --output-file=requirements-test.lock requirements-test.txt

# Sync .venv to match the lockfile exactly (removes packages not pinned in requirements-test.lock)
pip-sync: .venv
	.venv/bin/pip-sync requirements-test.lock

syntax-check:
	$(VERIFY_AND_RUN) ansible-playbook site.yml --syntax-check

shellcheck:
	shellcheck -S warning scripts/*.sh roles/claude/files/*.sh .githooks/pre-commit .githooks/commit-msg .githooks/prepare-commit-msg roles/dotfiles/files/git-template-*

markdownlint:
	@test -d node_modules || { echo "SKIP: node_modules absent — run: npm ci --ignore-scripts"; exit 0; }
	npx --no -- markdownlint-cli2 "**/*.md" "#node_modules" "#collections" "#.claude" "#references"

commitlint:
	@test -d node_modules || { echo "SKIP: node_modules absent — run: npm ci --ignore-scripts"; exit 0; }
	npx --no -- commitlint --from origin/main --to HEAD

# Container-based molecule tests + script tests (Podman, no libvirt required).
# Matches CI molecule coverage (test-macos excluded — requires macOS runner).
# For VM tests: make test-vm (requires: make bootstrap-test first).
test: shellcheck test-scripts test-poller test-fedora test-rocky test-debian test-container test-container-offline test-container-offline-distrobox test-packages-binaries test-distrobox-role

# Syntax-check the scripts/ directory (bash -n: parse only, no execution).
test-scripts:
	bash -n scripts/preflight.sh
	bash -n scripts/smoke-test.sh
	bash -n scripts/backup.sh
	bash -n scripts/vault-pass-ci.sh
	bash -n scripts/test-queue-poller.sh
	bash -n scripts/verify-collections.sh
	bash -n scripts/verify-ansible-args.sh
	bash -n scripts/test-hooks-security.sh
	bash scripts/test-hooks-security.sh
	bash scripts/test-hooks-security.sh

# Unit tests for roles/claude/files/claude-queue-poller.sh internal helpers.
test-poller:
	scripts/test-queue-poller.sh

test-%: .venv
	$(VERIFY_AND_RUN) .venv/bin/molecule test -s $*

smoke-test:
	scripts/smoke-test.sh

smoke-test-container:
	scripts/smoke-test.sh --container $(CONTAINER)

smoke-test-user:
	scripts/smoke-test.sh --user-only

# --- Vault ---

vault-edit:
	ansible-vault edit group_vars/all/vault.yml

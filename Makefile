.PHONY: help all minimal offline backup backup-dry-run bootstrap bootstrap-test lint check diff test smoke-test \
       dotfiles packages repos notes repos-ovnk repos-konflux repos-personal \
       repos-bpfman repos-downstream \
       ssh desktop system repos-dnf redhat containers claude distrobox container \
       container-rebuild csb-audit vault-edit update hooks \
       smoke-test-container \
       ci syntax-check shellcheck markdownlint commitlint check-vars-sync \
       test-scripts test-poller test-fedora test-rocky test-debian test-macos test-vm test-container test-container-offline test-container-offline-distrobox test-packages-binaries test-distrobox-role \
       preflight guard-not-root \
       pip-lock pip-sync

CONTAINER ?= fedora-dev

# display_ok_hosts is a callback plugin option not in the core config schema;
# ansible-config validate rejects it in [defaults]. Use the env var instead.
export ANSIBLE_DISPLAY_OK_HOSTS = false
# callback_result_format is a callback plugin option not in the core config schema;
# ansible-config validate rejects it in [defaults]. Use the env var instead.
export ANSIBLE_CALLBACK_RESULT_FORMAT = yaml

help:
	@echo "Primary:    all minimal offline container container-rebuild update"
	@echo "Roles:      dotfiles packages repos notes ssh desktop system repos-dnf"
	@echo "            redhat containers claude distrobox"
	@echo "Repos:      repos-ovnk repos-konflux repos-personal repos-bpfman repos-downstream"
	@echo "Testing:    lint ci test test-scripts test-poller test-fedora test-rocky test-debian test-macos test-vm test-container test-container-offline test-container-offline-distrobox test-packages-binaries smoke-test smoke-test-container check"
	@echo "Linting:    shellcheck markdownlint commitlint check-vars-sync syntax-check"
	@echo "Setup:      bootstrap bootstrap-test hooks"
	@echo "Other:      backup backup-dry-run csb-audit diff vault-edit"

# --- Primary targets ---
# Safety guard: user-space role targets must not run as root (dotfiles would install to /root/)
guard-not-root:
	@[ "$$(id -u)" != "0" ] || \
		{ echo "ERROR: Do not run as root. Use -K for privilege escalation (make all)." >&2; exit 1; }

all: guard-not-root preflight
	ansible-playbook site.yml --ask-become-pass

minimal: guard-not-root
	ansible-playbook site.yml --tags common,dotfiles,ssh,repos --skip-tags become

offline: guard-not-root preflight
	ansible-playbook site.yml --ask-become-pass -e packages_install_binaries=false

container: guard-not-root
	ansible-playbook site.yml --tags common,distrobox

container-rebuild: guard-not-root
	ansible-playbook site.yml --tags common,distrobox -e container_replace=true

backup:
	bash scripts/backup.sh

backup-dry-run:
	bash scripts/backup.sh --dry-run

# --- Bootstrap ---
# Fedora/RHEL: if make is not yet installed: sudo dnf install -y make

bootstrap:
	@if [ "$$(uname -s)" = "Darwin" ]; then \
		brew install ansible git openssh libfido2 ykman ykpers make; \
	elif command -v apt-get >/dev/null 2>&1; then \
		sudo apt-get update && sudo apt-get install -y ansible git yubikey-personalization make shellcheck python3-venv python3-pip; \
	else \
		sudo dnf install -y ansible-core git ykpers make ShellCheck; \
	fi
	@test -f scripts/vault-pass.sh || { cp scripts/vault-pass-ci.sh scripts/vault-pass.sh && echo "Created stub vault-pass.sh (replace with YubiKey version for real secrets)"; }
	@chmod 700 scripts/vault-pass.sh scripts/vault-pass-ci.sh
	ansible-galaxy collection install -r requirements.yml -p ./collections
	@if command -v npm >/dev/null 2>&1; then \
		npm install --ignore-scripts; \
	else \
		echo "NOTE: npm not found — install nodejs for commitlint hooks"; \
	fi
	git config --local core.hooksPath .githooks
	@echo "Bootstrap complete. Git hooks active."

bootstrap-test: .venv
	ansible-galaxy collection install -r requirements.yml -p ./collections
	sudo dnf install -y libvirt vagrant vagrant-libvirt
	sudo systemctl enable --now libvirtd
	vagrant box add githubixx/fedora-44 --provider libvirt

hooks:
	git config --local core.hooksPath .githooks
	@echo "Git hooks installed (core.hooksPath = .githooks)"

update: guard-not-root preflight
	@if command -v npm >/dev/null 2>&1; then \
		npm install --ignore-scripts; \
	else \
		echo "NOTE: npm not found — install nodejs for commitlint hooks"; \
	fi
	ansible-galaxy collection install -r requirements.yml --force -p ./collections
	ansible-playbook site.yml --ask-become-pass -e git_repos_pull=true

# --- Individual roles ---

dotfiles: guard-not-root
	ansible-playbook site.yml --tags common,dotfiles

packages: guard-not-root
	ansible-playbook site.yml --tags common,packages --ask-become-pass

repos: guard-not-root
	ansible-playbook site.yml --tags common,repos

notes: guard-not-root
	ansible-playbook site.yml --tags common,notes

repos-ovnk: guard-not-root
	ansible-playbook site.yml --tags common,repos -e repo_category=ovnk

repos-konflux: guard-not-root
	ansible-playbook site.yml --tags common,repos -e repo_category=konflux

repos-personal: guard-not-root
	ansible-playbook site.yml --tags common,repos -e repo_category=personal

repos-bpfman: guard-not-root
	ansible-playbook site.yml --tags common,repos -e repo_category=bpfman

repos-downstream: guard-not-root
	ansible-playbook site.yml --tags common,repos -e repo_category=downstream

ssh: guard-not-root
	ansible-playbook site.yml --tags common,ssh

desktop: guard-not-root
	ansible-playbook site.yml --tags common,desktop --ask-become-pass

system: guard-not-root
	ansible-playbook site.yml --tags common,system --ask-become-pass

repos-dnf: guard-not-root
	ansible-playbook site.yml --tags common,repos_dnf --ask-become-pass

redhat: guard-not-root
	ansible-playbook site.yml --tags common,redhat --ask-become-pass

containers: guard-not-root
	ansible-playbook site.yml --tags common,containers --ask-become-pass

claude: guard-not-root
	ansible-playbook site.yml --tags common,claude

distrobox: container  # alias for backwards compatibility

# --- Audit and testing ---

preflight:
	scripts/preflight.sh

csb-audit: preflight
	ansible-playbook site.yml --tags common --check -v

check:
	ansible-playbook site.yml --check --diff --ask-become-pass

diff:
	ansible-playbook site.yml --check --diff --tags dotfiles

# NOTE: includes test-macos — requires macOS runner. On Linux use: make test
ci: lint syntax-check test-scripts test-poller test-fedora test-rocky test-debian test-macos test-container test-container-offline test-container-offline-distrobox test-packages-binaries test-distrobox-role

lint: .venv shellcheck
	.venv/bin/ansible-lint
	.venv/bin/yamllint --strict .
	.venv/bin/python3 scripts/check-vars-sync.py

check-vars-sync: .venv
	.venv/bin/python3 scripts/check-vars-sync.py

.venv: requirements-test.lock
	python3 -m venv .venv
	.venv/bin/pip install pip-tools
	.venv/bin/pip install --require-hashes -r requirements-test.lock
	touch .venv

# Regenerate the hash-pinned lockfile (run after editing requirements-test.txt)
pip-lock: .venv
	.venv/bin/pip-compile --generate-hashes --output-file=requirements-test.lock requirements-test.txt

# Create .venv from the exact lockfile for reproducible CI builds
pip-sync: .venv
	.venv/bin/pip-sync requirements-test.lock

syntax-check:
	ansible-playbook site.yml --syntax-check

shellcheck:
	shellcheck -S warning scripts/*.sh roles/claude/files/*.sh .githooks/pre-commit .githooks/commit-msg

markdownlint:
	@test -d node_modules || { echo "SKIP: node_modules absent — run: npm install --ignore-scripts"; exit 0; }
	npx --no -- markdownlint-cli2 "**/*.md" "#node_modules" "#collections" "#.claude" "#references"

commitlint:
	@test -d node_modules || { echo "SKIP: node_modules absent — run: npm install --ignore-scripts"; exit 0; }
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

# Unit tests for roles/claude/files/claude-queue-poller.sh internal helpers.
test-poller:
	bash scripts/test-queue-poller.sh

test-fedora: .venv
	.venv/bin/molecule test -s fedora

test-rocky: .venv
	.venv/bin/molecule test -s rocky

test-debian: .venv
	.venv/bin/molecule test -s debian

test-macos: .venv
	.venv/bin/molecule test -s macos

test-container: .venv
	.venv/bin/molecule test -s container

# Rescue/degradation path test: no stubs, invalid oc version forces 404 → rescue.
# Confirms graceful degradation when the mirror is unreachable.
# Idempotence skipped (download always fails, rescue debug fires every run).
test-container-offline: .venv
	.venv/bin/molecule test -s container-offline

# Rescue/degradation path test in a distrobox environment (distrobox-host-exec present).
# Confirms container_oc_installed=false blocks the export tasks and symlink path is taken.
# Idempotence skipped (download always fails, rescue debug fires every run).
test-container-offline-distrobox: .venv
	.venv/bin/molecule test -s container-offline-distrobox

test-distrobox-role: .venv
	.venv/bin/molecule test -s distrobox-role

test-packages-binaries: .venv
	.venv/bin/molecule test -s packages-binaries

test-vm: .venv
	.venv/bin/molecule test -s vm

smoke-test:
	scripts/smoke-test.sh

smoke-test-container:
	scripts/smoke-test.sh --container $(CONTAINER)

# --- Vault ---

vault-edit:
	ansible-vault edit group_vars/all/vault.yml

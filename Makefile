.PHONY: help all minimal backup bootstrap bootstrap-test lint check diff test smoke-test \
       dotfiles packages repos notes repos-ovnk repos-konflux repos-personal \
       repos-bpfman repos-downstream repos-cncf \
       ssh desktop system repos-dnf redhat containers claude distrobox container \
       container-rebuild csb-audit vault-edit update hooks \
       smoke-test-container smoke-test-fedora \
       ci syntax-check shellcheck markdownlint commitlint \
       test-scripts test-fedora test-centos test-debian test-macos test-vm \
       preflight guard-not-root

help:
	@echo "Primary:    all minimal container container-rebuild update"
	@echo "Roles:      dotfiles packages repos notes ssh desktop system repos-dnf"
	@echo "            redhat containers claude distrobox"
	@echo "Repos:      repos-ovnk repos-konflux repos-personal repos-bpfman repos-downstream repos-cncf"
	@echo "Testing:    lint ci test test-scripts test-fedora test-centos test-debian test-vm smoke-test smoke-test-container check"
	@echo "Linting:    shellcheck markdownlint commitlint syntax-check"
	@echo "Setup:      bootstrap bootstrap-test hooks"
	@echo "Other:      backup csb-audit diff vault-edit"

# --- Primary targets ---
# Safety guard: user-space role targets must not run as root (dotfiles would install to /root/)
guard-not-root:
	@[ "$$(id -u)" != "0" ] || \
		{ echo "ERROR: Do not run as root. Use -K for privilege escalation (make all)." >&2; exit 1; }

all: guard-not-root
	ansible-playbook site.yml --ask-become-pass

minimal: guard-not-root
	ansible-playbook site.yml --tags common,dotfiles,ssh,repos --skip-tags become

container: guard-not-root
	ansible-playbook site.yml --tags common,distrobox

container-rebuild: guard-not-root
	ansible-playbook site.yml --tags common,distrobox -e container_replace=true

backup:
	bash scripts/backup.sh

# --- Bootstrap ---

bootstrap:
	@if [ "$$(uname -s)" = "Darwin" ]; then \
		brew install ansible git openssh libfido2 ykman ykpers make; \
	elif command -v apt-get >/dev/null 2>&1; then \
		sudo apt-get update && sudo apt-get install -y ansible git yubikey-personalization make; \
	else \
		sudo dnf install -y ansible-core git ykpers make; \
	fi
	@test -f scripts/vault-pass.sh || { cp scripts/vault-pass-ci.sh scripts/vault-pass.sh && chmod 700 scripts/vault-pass.sh && echo "Created stub vault-pass.sh (replace with YubiKey version for real secrets)"; }
	ansible-galaxy collection install -r requirements.yml
	find collections -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; ansible-galaxy collection verify community.general containers.podman ansible.posix
	@if command -v npm >/dev/null 2>&1; then \
		npm install --ignore-scripts; \
	else \
		echo "NOTE: npm not found — install nodejs for commitlint hooks"; \
	fi
	git config --local core.hooksPath .githooks
	@echo "Bootstrap complete. Git hooks active."

bootstrap-test: .venv
	ansible-galaxy collection install -r requirements.yml
	sudo dnf install -y libvirt vagrant vagrant-libvirt
	vagrant box add githubixx/fedora-44 --provider libvirt || true

hooks:
	git config --local core.hooksPath .githooks
	@echo "Git hooks installed (core.hooksPath = .githooks)"

update: guard-not-root
	ansible-galaxy collection install -r requirements.yml --force
	find collections -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; ansible-galaxy collection verify community.general containers.podman ansible.posix
	ansible-playbook site.yml --ask-become-pass

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

repos-cncf: guard-not-root
	ansible-playbook site.yml --tags common,repos -e repo_category=cncf

ssh: guard-not-root
	ansible-playbook site.yml --tags common,ssh

desktop: guard-not-root
	ansible-playbook site.yml --tags common,desktop --ask-become-pass

system:
	ansible-playbook site.yml --tags common,system --ask-become-pass

repos-dnf:
	ansible-playbook site.yml --tags common,repos_dnf --ask-become-pass

redhat:
	ansible-playbook site.yml --tags common,redhat --ask-become-pass

containers:
	ansible-playbook site.yml --tags common,containers --ask-become-pass

claude: guard-not-root
	ansible-playbook site.yml --tags common,claude

distrobox: guard-not-root
	ansible-playbook site.yml --tags common,distrobox

# --- Audit and testing ---

preflight:
	scripts/preflight.sh

csb-audit:
	scripts/preflight.sh
	ansible-playbook site.yml --tags common --check

check:
	ansible-playbook site.yml --check --ask-become-pass

diff:
	ansible-playbook site.yml --check --diff --tags dotfiles

ci: lint syntax-check test-scripts test-fedora test-centos test-debian test-macos

lint: .venv
	.venv/bin/ansible-lint
	.venv/bin/yamllint --strict .
	shellcheck -S warning scripts/*.sh roles/claude/files/*.sh .githooks/pre-commit .githooks/commit-msg

.venv: requirements-test.txt
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-test.txt

syntax-check:
	ansible-playbook site.yml --syntax-check

shellcheck:
	shellcheck -S warning scripts/*.sh roles/claude/files/*.sh .githooks/pre-commit .githooks/commit-msg

markdownlint:
	npx --no -- markdownlint-cli2 "**/*.md" "#node_modules" "#collections" "#.claude" "#references"

commitlint:
	npx --no -- commitlint --from origin/main --to HEAD

test: test-scripts test-fedora test-centos test-debian test-vm

test-scripts:
	bash scripts/test-queue-poller.sh

test-fedora: .venv
	.venv/bin/molecule test -s fedora

test-centos: .venv
	.venv/bin/molecule test -s centos

test-debian: .venv
	.venv/bin/molecule test -s debian

test-macos: .venv
	.venv/bin/molecule test -s macos

test-vm: .venv
	.venv/bin/molecule test -s vm

smoke-test:
	scripts/smoke-test.sh

smoke-test-container:
	scripts/smoke-test.sh --container fedora-dev

smoke-test-fedora: smoke-test-container

# --- Vault ---

vault-edit:
	ansible-vault edit group_vars/all/vault.yml

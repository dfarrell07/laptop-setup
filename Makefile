.PHONY: help all minimal backup bootstrap bootstrap-test lint check diff test smoke-test \
       dotfiles packages repos notes repos-ovnk repos-konflux repos-personal \
       repos-bpfman repos-downstream repos-cncf \
       ssh desktop system repos-dnf redhat containers claude distrobox container \
       container-rebuild csb-audit vault-edit update hooks \
       smoke-test-fedora \
       ci syntax-check shellcheck markdownlint commitlint \
       test-scripts test-fedora test-centos test-debian test-macos test-vm

help:
	@echo "Primary:    all minimal container container-rebuild update"
	@echo "Roles:      dotfiles packages repos notes ssh desktop system repos-dnf"
	@echo "            redhat containers claude distrobox"
	@echo "Repos:      repos-ovnk repos-konflux repos-personal repos-bpfman repos-downstream repos-cncf"
	@echo "Testing:    lint ci test test-scripts test-fedora test-centos test-debian test-vm smoke-test check"
	@echo "Linting:    shellcheck markdownlint commitlint syntax-check"
	@echo "Setup:      bootstrap bootstrap-test hooks"
	@echo "Other:      backup csb-audit diff vault-edit"

# --- Primary targets ---

all:
	ansible-playbook site.yml --ask-become-pass

minimal:
	ansible-playbook site.yml --tags common,dotfiles,ssh,repos --skip-tags become

container:
	ansible-playbook site.yml --tags common,distrobox

container-rebuild:
	ansible-playbook site.yml --tags common,distrobox -e container_replace=true

backup:
	bash scripts/backup.sh

# --- Bootstrap ---

bootstrap:
	sudo dnf install -y ansible-core git ykpers
	@test -f scripts/vault-pass.sh || { cp scripts/vault-pass-ci.sh scripts/vault-pass.sh && chmod 700 scripts/vault-pass.sh && echo "Created stub vault-pass.sh (replace with YubiKey version for real secrets)"; }
	ansible-galaxy collection install -r requirements.yml
	find collections -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; ansible-galaxy collection verify community.general containers.podman ansible.posix
	@if command -v npm >/dev/null 2>&1; then \
		npm install; \
	else \
		echo "NOTE: npm not found — install nodejs for commitlint hooks"; \
	fi
	git config --local core.hooksPath .githooks
	@echo "Bootstrap complete. Git hooks active."

bootstrap-test:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-test.txt
	ansible-galaxy collection install -r requirements.yml
	sudo dnf install -y libvirt vagrant vagrant-libvirt
	vagrant box add fedora/43-cloud-base --provider libvirt || true

hooks:
	git config --local core.hooksPath .githooks
	@echo "Git hooks installed (core.hooksPath = .githooks)"

update:
	ansible-galaxy collection install -r requirements.yml --force
	find collections -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; ansible-galaxy collection verify community.general containers.podman ansible.posix
	ansible-playbook site.yml --ask-become-pass

# --- Individual roles ---

dotfiles:
	ansible-playbook site.yml --tags common,dotfiles

packages:
	ansible-playbook site.yml --tags common,packages --ask-become-pass

repos:
	ansible-playbook site.yml --tags common,repos

notes:
	ansible-playbook site.yml --tags common,notes

repos-ovnk:
	ansible-playbook site.yml --tags common,repos -e repo_category=ovnk

repos-konflux:
	ansible-playbook site.yml --tags common,repos -e repo_category=konflux

repos-personal:
	ansible-playbook site.yml --tags common,repos -e repo_category=personal

repos-bpfman:
	ansible-playbook site.yml --tags common,repos -e repo_category=bpfman

repos-downstream:
	ansible-playbook site.yml --tags common,repos -e repo_category=downstream

repos-cncf:
	ansible-playbook site.yml --tags common,repos -e repo_category=cncf

ssh:
	ansible-playbook site.yml --tags common,ssh

desktop:
	ansible-playbook site.yml --tags common,desktop --ask-become-pass

system:
	ansible-playbook site.yml --tags common,system --ask-become-pass

repos-dnf:
	ansible-playbook site.yml --tags common,repos_dnf --ask-become-pass

redhat:
	ansible-playbook site.yml --tags common,redhat --ask-become-pass

containers:
	ansible-playbook site.yml --tags common,containers --ask-become-pass

claude:
	ansible-playbook site.yml --tags common,claude

distrobox:
	ansible-playbook site.yml --tags common,distrobox

# --- Audit and testing ---

csb-audit:
	scripts/preflight.sh
	ansible-playbook site.yml --tags common --check

check:
	ansible-playbook site.yml --check --ask-become-pass

diff:
	ansible-playbook site.yml --check --diff --tags dotfiles

ci: lint syntax-check test-scripts test-fedora test-centos test-debian

lint:
	ansible-lint
	yamllint --strict .
	shellcheck -S warning scripts/*.sh roles/claude/files/*.sh .githooks/pre-commit .githooks/commit-msg

syntax-check:
	ansible-playbook site.yml --syntax-check

shellcheck:
	shellcheck -S warning scripts/*.sh roles/claude/files/*.sh .githooks/pre-commit .githooks/commit-msg

markdownlint:
	npx markdownlint-cli2 "**/*.md" "#node_modules" "#collections" "#.claude" "#references"

commitlint:
	npx commitlint --from origin/main --to HEAD

test: test-scripts test-fedora test-centos test-debian test-vm

test-scripts:
	bash scripts/test-queue-poller.sh

test-fedora:
	molecule test -s fedora

test-centos:
	molecule test -s centos

test-debian:
	molecule test -s debian

test-macos:
	molecule test -s macos

test-vm:
	molecule test -s vm

smoke-test:
	scripts/smoke-test.sh

smoke-test-fedora:
	scripts/smoke-test.sh --container fedora-dev

# --- Vault ---

vault-edit:
	ansible-vault edit group_vars/all/vault.yml

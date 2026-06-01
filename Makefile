.PHONY: all minimal bootstrap lint check diff test smoke-test \
       dotfiles packages repos repos-ovnk repos-konflux repos-personal \
       ssh desktop system repos-dnf redhat containers claude distrobox container \
       container-rebuild csb-audit vault-edit update \
       smoke-test-host smoke-test-container \
       syntax-check shellcheck markdownlint commitlint \
       test-container test-vm

# --- Primary targets ---

all:
	ansible-playbook site.yml --ask-become-pass

minimal:
	ansible-playbook site.yml --tags common,dotfiles,ssh,repos --skip-tags become

container:
	ansible-playbook site.yml --tags common,distrobox

container-rebuild:
	ansible-playbook site.yml --tags common,distrobox -e container_replace=true

# --- Bootstrap ---

bootstrap:
	sudo dnf install -y ansible-core git ykpers
	ansible-galaxy collection install -r requirements.yml

update:
	ansible-galaxy collection install -r requirements.yml --force
	ansible-playbook site.yml --ask-become-pass

# --- Individual roles ---

dotfiles:
	ansible-playbook site.yml --tags common,dotfiles

packages:
	ansible-playbook site.yml --tags common,packages --ask-become-pass

repos:
	ansible-playbook site.yml --tags common,repos

repos-ovnk:
	ansible-playbook site.yml --tags common,repos -e repo_category=ovnk

repos-konflux:
	ansible-playbook site.yml --tags common,repos -e repo_category=konflux

repos-personal:
	ansible-playbook site.yml --tags common,repos -e repo_category=personal

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

lint:
	ansible-lint
	yamllint --strict .
	find scripts -type f -name "*.sh" -exec shellcheck -S warning {} +

syntax-check:
	ansible-playbook site.yml --syntax-check

shellcheck:
	find scripts -type f -name "*.sh" -exec shellcheck -S warning {} +

markdownlint:
	npx markdownlint-cli2 "**/*.md" "#node_modules" "#collections" "#.claude" "#references"

commitlint:
	npx commitlint --from origin/main --to HEAD

test: test-container test-vm

test-container:
	molecule test -s container

test-vm:
	molecule test -s vm

smoke-test:
	scripts/smoke-test.sh

smoke-test-host:
	scripts/smoke-test.sh

smoke-test-container:
	scripts/smoke-test.sh --container fedora-dev

# --- Vault ---

vault-edit:
	ansible-vault edit group_vars/all/vault.yml

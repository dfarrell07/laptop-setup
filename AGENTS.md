# Working in laptop-setup

Read [CLAUDE.md](CLAUDE.md) for conventions, Make targets and warnings, and
[docs/design-principles.md](docs/design-principles.md) before changing roles, Make targets or
hardening. Agents do not run provisioning; the owner does (plan: [docs/rollout-plan.md](docs/rollout-plan.md)).

Use feature branches and signed-off incremental commits. Keep secrets, host-specific names and raw AI
memory/history out of Git; capture portable configuration in roles and decisions in
docs/design-principles.md. Before committing, follow "Testing and CI" in CLAUDE.md. Linux rendering
does not establish native macOS behavior.

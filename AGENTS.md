# Working in laptop-setup

Read [CLAUDE.md](CLAUDE.md) for repository conventions, Make targets and provisioning
prerequisites. Use feature branches and signed-off incremental commits.

When auditing a source laptop, capture portable configuration in roles and concise
decisions in the migration docs; run provisioning from the destination checkout.
Keep account secrets and raw AI memory/history out of Git.

Use the focused regressions for changed behavior, then the repository lint,
syntax and script gates. Linux rendering does not establish native macOS behavior.

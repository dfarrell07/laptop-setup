module.exports = {
  rules: {
    'header-max-length': [2, 'always', 72],
    'body-leading-blank': [2, 'always'],
    'body-max-line-length': [2, 'always', 100],
    // This repo uses "Scope: message" format (scope before colon = commitlint "type" field).
    // type-enum is severity 1 (warn) with Title-Case variants matching CLAUDE.md convention.
    // scope-enum stays at 0 (off) — no parenthetical scopes are used in this repo.
    'type-enum': [1, 'always', [
      'Common', 'Repos-dnf', 'Packages', 'Dotfiles', 'Ssh', 'Git-repos',
      'Notes', 'Redhat', 'Containers', 'Desktop', 'System', 'Distrobox',
      'Claude', 'Ci', 'Docs', 'Chore', 'Smoke', 'Molecule', 'Multi',
      'Sshd', 'Nm', 'Auditd', 'Backup', 'Handlers', 'Security',
      'Vault', 'Preflight',
    ]],
    'scope-enum': [0, 'always', [
      'common', 'repos-dnf', 'packages', 'dotfiles', 'ssh', 'git-repos',
      'notes', 'redhat', 'containers', 'desktop', 'system', 'distrobox',
      'claude', 'ci', 'docs', 'chore', 'smoke', 'molecule', 'multi',
      'sshd', 'nm', 'auditd', 'backup', 'handlers', 'security', 'deps',
      'vault', 'preflight',
    ]],
  },
};

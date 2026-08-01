module.exports = {
  rules: {
    'header-max-length': [2, 'always', 72],
    'body-leading-blank': [2, 'always'],
    'body-max-line-length': [2, 'always', 100],
    // This repo uses "Scope: message" format (scope before colon = commitlint "type" field).
    // type-enum enforces the allowed scope list; scope-enum covers parenthetical-scope commits.
    'type-enum': [1, 'always', [
      'common', 'repos-dnf', 'packages', 'dotfiles', 'ssh', 'git-repos',
      'notes', 'redhat', 'containers', 'desktop', 'system', 'distrobox',
      'claude', 'ci', 'docs', 'chore', 'smoke', 'molecule', 'multi',
      'sshd', 'nm', 'auditd', 'backup', 'handlers', 'security',
    ]],
    'scope-enum': [1, 'always', [
      'common', 'repos-dnf', 'packages', 'dotfiles', 'ssh', 'git-repos',
      'notes', 'redhat', 'containers', 'desktop', 'system', 'distrobox',
      'claude', 'ci', 'docs', 'chore', 'smoke', 'molecule', 'multi',
      'sshd', 'nm', 'auditd', 'backup', 'handlers', 'security', 'deps',
    ]],
  },
};

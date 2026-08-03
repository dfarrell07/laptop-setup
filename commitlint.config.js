module.exports = {
  rules: {
    'header-max-length': [2, 'always', 72],
    'body-leading-blank': [2, 'always'],
    'body-max-line-length': [2, 'always', 100],
    // This repo uses "Scope: message" format (scope before colon = commitlint "type" field).
    // type-enum and scope-enum are at severity 0 (off) because the repo uses Title-Case
    // scopes (Multi:, System:, Docs:) per CLAUDE.md convention, but commitlint compares
    // case-sensitively against the lowercase list below — generating a warning on every
    // commit and obscuring real violations. CLAUDE.md is the authoritative scope reference.
    'type-enum': [0, 'always', [
      'common', 'repos-dnf', 'packages', 'dotfiles', 'ssh', 'git-repos',
      'notes', 'redhat', 'containers', 'desktop', 'system', 'distrobox',
      'claude', 'ci', 'docs', 'chore', 'smoke', 'molecule', 'multi',
      'sshd', 'nm', 'auditd', 'backup', 'handlers', 'security',
    ]],
    'scope-enum': [0, 'always', [
      'common', 'repos-dnf', 'packages', 'dotfiles', 'ssh', 'git-repos',
      'notes', 'redhat', 'containers', 'desktop', 'system', 'distrobox',
      'claude', 'ci', 'docs', 'chore', 'smoke', 'molecule', 'multi',
      'sshd', 'nm', 'auditd', 'backup', 'handlers', 'security', 'deps',
    ]],
  },
};

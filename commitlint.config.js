module.exports = {
  rules: {
    'header-max-length': [2, 'always', 72],
    'header-min-length': [2, 'always', 10],
    'body-leading-blank': [2, 'always'],
    'body-max-line-length': [2, 'always', 100],
    'footer-leading-blank': [2, 'always'],
    'scope-enum': [1, 'always', [
      'common', 'repos-dnf', 'packages', 'dotfiles', 'ssh', 'git-repos',
      'notes', 'redhat', 'containers', 'desktop', 'system', 'distrobox',
      'claude', 'ci', 'docs', 'chore',
    ]],
    'scope-empty': [0, 'never'],
  },
};

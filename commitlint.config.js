module.exports = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'scope-enum': [
      2,
      'always',
      [
        'common',
        'dotfiles',
        'packages',
        'ssh',
        'git_repos',
        'repos_dnf',
        'system',
        'redhat',
        'containers',
        'desktop',
        'distrobox',
        'claude',
        'ci',
        'deps',
        'docs',
        'scripts',
        'molecule',
        'notes',
      ],
    ],
  },
};

module.exports = {
  // Parse the repository's Role/Subrole: subject convention, including hyphens.
  parserPreset: {
    parserOpts: {
      headerPattern: /^([A-Z][-A-Za-z0-9/]*):[ \t]+(.+)$/,
      headerCorrespondence: ['type', 'subject'],
    },
  },
  rules: {
    'header-max-length': [2, 'always', 72],
    'body-leading-blank': [2, 'always'],
    'body-max-line-length': [2, 'always', 200],
    // Scope format is enforced by the commit-msg hook regex; scopes are not enumerated here.
    'subject-empty': [2, 'never'],
    'subject-full-stop': [2, 'never', '.'],
    'scope-empty': [2, 'always'],
  },
};

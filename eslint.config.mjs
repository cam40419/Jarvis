import globals from 'globals';

// Classic deferred scripts share page globals in the order declared by chat.html
// and login.html. Keep these contracts explicit until those scripts become modules.
const pageGlobals = {
  'accounts.js': ['$', 'api', 'passwordEnabled', 'session'],
  'background-chat.js': [
    'activeThread',
    'api',
    'assistant',
    'autosize',
    'busy',
    'drafts',
    'el',
    'loading',
    'make',
    'message',
    'messages',
    'ready',
    'refreshThreads',
    'report',
    'setBusy',
  ],
  'chat.js': ['SimonMarkdown', 'actionCard', 'connections', 'showConnectedResults'],
  'connections.js': ['api', 'assistant', 'el', 'googleReturn', 'make', 'openPanel', 'report'],
  'personality.js': ['api', 'el', 'preferences', 'ready'],
  'voice.js': ['api', 'el', 'make', 'ready', 'session'],
};

export default [
  {
    files: [
      'src/simon/api/static/*.js',
      'examples/connectors/adobe/*.js',
      'tests/connectors/*.cjs',
    ],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'script',
      globals: { ...globals.browser, appPath: 'readonly' },
    },
    rules: {
      'constructor-super': 'error',
      'for-direction': 'error',
      'no-async-promise-executor': 'error',
      'no-constant-binary-expression': 'error',
      'no-dupe-args': 'error',
      'no-dupe-else-if': 'error',
      'no-dupe-keys': 'error',
      'no-duplicate-case': 'error',
      'no-fallthrough': 'error',
      'no-promise-executor-return': 'error',
      'no-self-assign': 'error',
      'no-undef': 'error',
      'no-unreachable': 'error',
      'no-unsafe-finally': 'error',
      'no-unused-vars': ['error', { argsIgnorePattern: '^_', caughtErrors: 'none' }],
      'use-isnan': 'error',
      'valid-typeof': 'error',
    },
  },
  {
    files: ['examples/connectors/adobe/*.js'],
    languageOptions: { globals: { require: 'readonly' } },
  },
  {
    files: ['tests/connectors/*.cjs'],
    languageOptions: { sourceType: 'commonjs', globals: globals.node },
  },
  ...Object.entries(pageGlobals).map(([file, names]) => ({
    files: [`src/simon/api/static/${file}`],
    languageOptions: {
      globals: Object.fromEntries(names.map((name) => [name, 'readonly'])),
    },
  })),
];

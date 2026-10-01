const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const code = fs.readFileSync(
  path.join(__dirname, '../../examples/connectors/adobe/index.js'),
  'utf8',
);

function folder(nativePath) {
  const entries = new Map();
  return {
    nativePath,
    entries,
    async renameEntry(entry, newName, options) {
      if (entries.has(newName) && !options.overwrite) throw Error('Exists');
      entries.delete(entry.name);
      entry.name = newName;
      entries.set(newName, entry);
    },
    async getEntries() {
      return [...entries.values()];
    },
    async getEntry(name) {
      if (!entries.has(name)) throw Error('Missing');
      return entries.get(name);
    },
    async createFile(name, options) {
      if (entries.has(name) && !options.overwrite) throw Error('Exists');
      const file = {
        name,
        nativePath: nativePath + '/' + name,
        contents: '',
        async read() {
          return this.contents;
        },
        async write(text) {
          this.contents = text;
        },
        async delete() {
          entries.delete(this.name);
        },
      };
      entries.set(name, file);
      return file;
    },
  };
}

async function setup(application, operation = 'save', overrides = {}) {
  const mailbox = folder('C:/mailbox');
  const workspace = folder('C:/work');
  const id = '12345678-1234-1234-1234-123456789abc';
  const session = {
    application,
    workspace: workspace.nativePath,
    lease_id: 'lease',
    fencing_token: 2,
    expires_at: Date.now() / 1000 + 60,
  };
  const request = {
    ...session,
    version: 1,
    invocation_id: id,
    operation,
    arguments:
      operation === 'save'
        ? { output: application === 'photoshop' ? 'copy.psd' : 'copy.prproj' }
        : {},
    ...overrides,
  };
  await (await mailbox.createFile('session.json', {})).write(JSON.stringify(session));
  await (await mailbox.createFile('request.json', {})).write(JSON.stringify(request));
  let saved = 0;
  let tick;
  const elements = Object.fromEntries(['connect', 'stop', 'status'].map((name) => [name, {}]));
  const folders = [mailbox, workspace];
  const modules = {
    uxp: {
      host: { name: application === 'premiere' ? 'Premiere Pro' : 'Photoshop' },
      storage: { localFileSystem: { getFolder: async () => folders.shift() } },
    },
    photoshop: {
      app: {
        activeDocument: {
          title: 'Test',
          layers: [{ name: 'Layer' }],
          saveAs: {
            psd: async () => {
              saved++;
            },
          },
        },
      },
      core: { executeAsModal: async (fn) => fn() },
    },
    premierepro: {
      Project: {
        getActiveProject: async () => ({
          name: 'Test',
          saveAs: async () => {
            saved++;
            return true;
          },
        }),
      },
    },
  };
  vm.runInNewContext(code, {
    require: (name) => modules[name],
    document: { getElementById: (id) => elements[id] },
    setInterval: (fn) => {
      tick = fn;
    },
    clearInterval() {},
  });
  await elements.connect.onclick();
  return {
    mailbox,
    workspace,
    tick: () => tick(),
    saved: () => saved,
    result: async () => JSON.parse(await (await mailbox.getEntry(id + '.result.json')).read()),
  };
}

for (const application of ['photoshop', 'premiere']) {
  test(application + ' saves through its native API', async () => {
    const host = await setup(application);
    await host.tick();
    assert.equal((await host.result()).ok, true);
    assert.equal(host.saved(), 1);
    await host.tick();
    assert.equal(host.saved(), 1);
  });
  test(application + ' inspects without saving', async () => {
    const host = await setup(application, 'inspect');
    await host.tick();
    assert.equal((await host.result()).result.document, 'Test');
    assert.equal(host.saved(), 0);
  });
}

for (const overrides of [
  { fencing_token: 1 },
  { expires_at: 0 },
  { application: 'rhino' },
  { arguments: { output: '../copy.psd' } },
  { operation: 'eval' },
]) {
  test('rejects invalid request ' + JSON.stringify(overrides), async () => {
    const host = await setup('photoshop', 'save', overrides);
    await host.tick();
    assert.equal((await host.result()).ok, false);
    assert.equal(host.saved(), 0);
  });
}

test('rejects output overwrite', async () => {
  const host = await setup('photoshop');
  await host.workspace.createFile('copy.psd', {});
  await host.tick();
  assert.equal((await host.result()).ok, false);
  assert.equal(host.saved(), 0);
});

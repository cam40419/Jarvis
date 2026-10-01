/* Runs inside Photoshop or Premiere UXP; no network or arbitrary script execution. */
const fs = require('uxp').storage.localFileSystem;
let mailbox;
let workspace;
let timer;
let running = false;
let connected = false;

async function read(folder, name) {
  return JSON.parse(await (await folder.getEntry(name)).read());
}

async function execute(application, operation, args) {
  if (application === 'photoshop') {
    const ps = require('photoshop');
    const doc = ps.app.activeDocument;
    if (!doc) throw new Error('No active document');
    if (operation === 'inspect') {
      return {
        document: doc.title,
        layers: Array.from(doc.layers)
          .slice(0, 200)
          .map((layer) => layer.name),
      };
    }
    return ps.core.executeAsModal(
      async () => {
        const file = await workspace.createFile(args.output, { overwrite: false });
        await doc.saveAs.psd(file, {}, true);
        return { output: file.nativePath };
      },
      { commandName: 'Simon save copy' },
    );
  }
  const ppro = require('premierepro');
  const project = await ppro.Project.getActiveProject();
  if (!project) throw new Error('No active project');
  if (operation === 'inspect') return { document: project.name };
  const output = workspace.nativePath + '/' + args.output;
  const saved = await project.saveAs(output);
  if (saved === false) throw new Error('Project save failed');
  return { output };
}

async function pump() {
  if (running || !connected) return;
  running = true;
  try {
    const entries = await mailbox.getEntries();
    const pending = entries.find((entry) => entry.name === 'request.json');
    if (!pending) return;
    const request = JSON.parse(await pending.read());
    const id = request.invocation_id;
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id)) {
      throw new Error('Invalid invocation');
    }
    const claim = await mailbox.createFile(id + '.claimed', { overwrite: false });
    await claim.write('dispatched');
    let response;
    try {
      const session = await read(mailbox, 'session.json');
      const host = require('uxp').host.name.toLowerCase();
      const application = host.includes('photoshop')
        ? 'photoshop'
        : host.includes('premiere')
          ? 'premiere'
          : null;
      if (
        !application ||
        request.application !== application ||
        session.application !== application ||
        request.version !== 1 ||
        request.lease_id !== session.lease_id ||
        request.fencing_token !== session.fencing_token ||
        !Number.isFinite(request.expires_at) ||
        !Number.isFinite(session.expires_at) ||
        Date.now() / 1000 >= Math.min(request.expires_at, session.expires_at) ||
        session.workspace.replaceAll('\\', '/').toLowerCase() !==
          workspace.nativePath.replaceAll('\\', '/').toLowerCase()
      ) {
        throw new Error('Session mismatch or expiry');
      }
      const args = request.arguments;
      if (request.operation === 'save') {
        const suffix = application === 'photoshop' ? '.psd' : '.prproj';
        if (
          Object.keys(args).length !== 1 ||
          !/^[A-Za-z0-9_-]+\.[a-z]+$/.test(args.output) ||
          !args.output.endsWith(suffix) ||
          (await workspace.getEntries()).some(
            (entry) => entry.name.toLowerCase() === args.output.toLowerCase(),
          )
        ) {
          throw new Error('Invalid or existing output');
        }
      } else if (request.operation !== 'inspect' || Object.keys(args).length) {
        throw new Error('Unsupported operation');
      }
      response = { ok: true, result: await execute(application, request.operation, args) };
    } catch (_) {
      response = { ok: false, error: 'Native operation failed; inspect before retrying' };
    }
    const result = await mailbox.createFile(id + '.tmp', { overwrite: false });
    await result.write(JSON.stringify(response));
    await pending.delete();
    await mailbox.renameEntry(result, id + '.result.json', { overwrite: false });
    if (!response.ok) throw new Error(response.error);
    document.getElementById('status').textContent = 'Connected; last operation completed';
  } catch (_) {
    connected = false;
    clearInterval(timer);
    document.getElementById('status').textContent =
      'Stopped; inspect and reconcile the runner session';
  } finally {
    running = false;
  }
}

document.getElementById('connect').onclick = async () => {
  if (running) return;
  clearInterval(timer);
  connected = false;
  mailbox = await fs.getFolder();
  if (!mailbox) return;
  workspace = await fs.getFolder();
  if (!workspace) return;
  connected = true;
  document.getElementById('status').textContent = 'Connected';
  timer = setInterval(pump, 500);
};
document.getElementById('stop').onclick = () => {
  connected = false;
  clearInterval(timer);
  document.getElementById('status').textContent =
    'Stopped; an in-flight operation may still finish';
};

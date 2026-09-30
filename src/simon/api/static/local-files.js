'use strict';
(() => {
  const node = (tag, text) => { const value = document.createElement(tag); if (text !== undefined) value.textContent = text; return value; };
  const panel = node('dialog'); panel.id = 'local-files-panel'; panel.className = 'project-files-panel';
  panel.setAttribute('aria-label', 'Local files'); document.body.append(panel);
  let generation = 0;
  panel.addEventListener('close', () => { generation++; });
  const action = (name, args) => api('/v1/local-files/action', {name, arguments: args, idempotency_key: crypto.randomUUID()});
  const ask = (root, path) => {
    panel.close(); document.getElementById('chat-open').click();
    const input = document.getElementById('text');
    input.value = `Work with the local file at root "${root}", path "${path}" on Simon's server. Read it first. What I want changed: `;
    autosize(); input.focus();
  };
  async function open(root = 'workspace', path = '', offset = 0) {
    const gen = ++generation;
    if (!panel.open) panel.showModal();
    const title = node('h2', 'Local files');
    const close = node('button', 'Close'); close.type = 'button'; close.onclick = () => panel.close();
    const status = node('p', 'Loading folders…'); status.setAttribute('role', 'status');
    const body = node('div'); panel.replaceChildren(title, close, status, body);
    const button = (parent, label, callback) => {
      const value = node('button', label); value.type = 'button';
      value.onclick = async () => {
        value.disabled = true;
        try { await callback(); } catch (error) { status.textContent = error.message; }
        finally { value.disabled = false; }
      };
      parent.append(value); return value;
    };
    try {
      const [roots, listing] = await Promise.all([
        api('/v1/local-files/roots'), api('/v1/local-files/list?' + new URLSearchParams({root, path, offset})),
      ]);
      if (generation !== gen) return;
      const rootLabel = node('label', 'Folder location '), select = node('select'); select.id = 'local-files-root';
      for (const entry of roots.roots) select.add(new Option(entry.root, entry.root));
      if (root.startsWith('project:')) select.add(new Option('This project', root));
      select.value = root; select.onchange = () => open(select.value); rootLabel.append(select);
      body.append(rootLabel, node('p', path || '/'));
      const navigation = node('div'); navigation.className = 'work-item-actions'; body.append(navigation);
      button(navigation, 'Up', () => open(root, path.split('/').slice(0, -1).join('/')));
      button(navigation, 'Refresh', () => open(root, path, offset));
      button(navigation, 'New folder', async () => {
        const name = window.prompt('Folder name'); if (!name?.trim()) return;
        await action('local_folder_create', {root, path: [path, name.trim()].filter(Boolean).join('/')});
        await open(root, path);
      });
      const label = node('label', 'Upload a file or ZIP (up to 50 MB) '), upload = node('input');
      upload.type = 'file'; upload.id = 'local-file-upload'; label.append(upload); body.append(label);
      upload.onchange = async () => {
        const file = upload.files[0]; if (!file) return;
        if (file.size > 50 * 1024 * 1024) { status.textContent = 'Uploads are limited to 50 MB.'; return; }
        upload.disabled = true; status.textContent = 'Uploading…';
        try {
          const params = new URLSearchParams({root, path: [path, file.name].filter(Boolean).join('/'), idempotency_key: crypto.randomUUID()});
          const response = await fetch(appPath('/v1/local-files/upload?' + params), {
            method: 'POST', credentials: 'same-origin', body: file,
            headers: {'Content-Type': 'application/octet-stream', 'X-CSRF-Token': session.csrf_token},
          });
          const result = await response.json();
          if (!response.ok) throw Error(result.error?.message || 'Upload failed.');
          await open(root, path);
        } catch (error) { status.textContent = error.message; upload.disabled = false; }
      };
      if (!listing.files.length) body.append(node('p', 'This folder is empty.'));
      for (const file of listing.files) {
        const row = node('article'); row.className = 'work-item'; row.append(node('strong', file.name));
        const controls = node('div'); controls.className = 'work-item-actions'; row.append(controls);
        if (file.kind === 'folder') button(controls, 'Open folder', () => open(root, file.path));
        else {
          const link = node('a', 'Download'); link.href = appPath('/v1/local-files/download?' + new URLSearchParams({root, path: file.path})); controls.append(link);
          const preview = node('pre'); preview.hidden = true; row.append(preview);
          let nextOffset = null;
          const read = async start => {
            const result = await api('/v1/local-files/read?' + new URLSearchParams({root, path: file.path, offset: start}));
            preview.textContent = result.text; preview.hidden = false; nextOffset = result.next_offset;
            more.hidden = nextOffset == null;
          };
          button(controls, 'Read', () => read(0));
          const more = button(controls, 'Next text', () => read(nextOffset)); more.hidden = true;
          button(controls, 'Edit with Simon', () => ask(root, file.path));
          if (file.name.toLowerCase().endsWith('.zip')) {
            button(controls, 'Inspect ZIP', async () => {
              const result = await action('local_zip_inspect', {root, path: file.path, offset: 0});
              preview.textContent = result.entries.map(entry => `${entry.path} (${entry.bytes} bytes)`).join('\n'); preview.hidden = false;
              status.textContent = `${result.total} ZIP entries. Showing the first ${result.entries.length}.`;
            });
            button(controls, 'Extract ZIP', async () => {
              const targetRoot = root.startsWith('project:') ? root : 'workspace';
              const suggested = 'imports/' + file.name.slice(0, -4);
              const destination = window.prompt(`New extraction folder in ${targetRoot}`, suggested);
              if (!destination?.trim()) return;
              status.textContent = 'Extracting ZIP…';
              await action('local_zip_extract', {root, path: file.path, destination_root: targetRoot, destination_path: destination.trim()});
              await open(targetRoot, destination.trim());
            });
          }
        }
        body.append(row);
      }
      if (listing.next_offset != null) button(body, 'Next files', () => open(root, path, listing.next_offset));
      if (offset) button(body, 'Previous files', () => open(root, path, Math.max(0, offset - 100)));
      status.textContent = 'Files on Simon’s server. Upload files here to make them available from other devices.';
    } catch (error) { status.textContent = error.message; }
  }
  window.SimonLocalFiles = {open};
  const launcher = node('button', 'Local files'); launcher.id = 'local-files-open'; launcher.type = 'button';
  launcher.onclick = () => open(); document.getElementById('work-refresh').after(launcher);
})();

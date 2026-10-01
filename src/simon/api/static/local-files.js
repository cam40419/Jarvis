'use strict';
(() => {
  const node = (tag, text) => { const value = document.createElement(tag); if (text !== undefined) value.textContent = text; return value; };
  const panel = node('dialog'); panel.id = 'local-files-panel'; panel.className = 'project-files-panel';
  panel.classList.add('local-files-panel');
  panel.setAttribute('aria-label', 'Local files'); document.body.append(panel);
  const rootName = root => root === 'workspace' ? 'My workspace' : root.startsWith('project:') ? 'This project' : root;
  const sizeLabel = bytes => bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
  let generation = 0;
  panel.addEventListener('close', () => { if (!panel.open) generation++; });
  const action = (name, args) => api('/v1/local-files/action', {name, arguments: args, idempotency_key: crypto.randomUUID()});
  const ask = (root, path) => {
    panel.close(); document.getElementById('chat-open').click();
    const input = document.getElementById('text');
    input.value = `Work with the local file at root "${root}", path "${path}" on Simon's server. Read it first. What I want changed: `;
    autosize(); input.focus();
  };
  async function open(root = 'workspace', path = '', offset = 0) {
    const gen = ++generation;
    const title = node('h2', 'Local files');
    const close = node('button', 'Close'); close.type = 'button'; close.onclick = () => panel.close();
    const status = node('p', 'Loading folders…'); status.setAttribute('role', 'status');
    const header = node('header'); header.className = 'lf-header';
    const heading = node('div'), eyebrow = node('p', 'SERVER STORAGE'); eyebrow.className = 'lf-eyebrow';
    heading.append(eyebrow, title); header.append(heading, close);
    status.className = 'lf-status';
    const body = node('div'); body.className = 'lf-body'; panel.replaceChildren(header, body, status);
    if (!panel.open) panel.showModal();
    const button = (parent, label, callback) => {
      const value = node('button', label); value.type = 'button';
      value.onclick = async () => {
        value.disabled = true;
        try { await callback(); } catch (error) { if (generation === gen) status.textContent = error.message; }
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
      for (const entry of roots.roots) select.add(new Option(rootName(entry.root), entry.root));
      if (root.startsWith('project:') && ![...select.options].some(option => option.value === root)) select.add(new Option('This project', root));
      select.value = root; select.onchange = () => open(select.value); rootLabel.append(select);
      const location = node('div'); location.className = 'lf-location';
      const breadcrumbs = node('nav'); breadcrumbs.className = 'lf-breadcrumbs'; breadcrumbs.setAttribute('aria-label', 'Folder path');
      const rootCrumb = button(breadcrumbs, rootName(root), () => open(root));
      if (!path) rootCrumb.setAttribute('aria-current', 'location');
      const segments = path.split('/').filter(Boolean);
      segments.forEach((segment, index) => {
        const separator = node('span', '/'); separator.setAttribute('aria-hidden', 'true'); breadcrumbs.append(separator);
        const crumb = button(breadcrumbs, segment, () => open(root, segments.slice(0, index + 1).join('/')));
        if (index === segments.length - 1) crumb.setAttribute('aria-current', 'location');
      });
      location.append(rootLabel, breadcrumbs); body.append(location);
      const navigation = node('div'); navigation.className = 'lf-toolbar'; body.append(navigation);
      const up = button(navigation, 'Up', () => open(root, segments.slice(0, -1).join('/'))); up.disabled = !path;
      button(navigation, 'Refresh', () => open(root, path, offset));
      button(navigation, 'New folder', async () => {
        const name = window.prompt('Folder name'); if (!name?.trim()) return;
        await action('local_folder_create', {root, path: [path, name.trim()].filter(Boolean).join('/')});
        if (generation === gen) await open(root, path);
      });
      const filterLabel = node('label', 'Filter this page'), filter = node('input'); filterLabel.className = 'lf-filter';
      filter.type = 'search'; filter.id = 'local-files-filter'; filter.placeholder = 'Filter by file name'; filterLabel.append(filter); navigation.append(filterLabel);
      const label = node('label', 'Upload a file or ZIP (up to 50 MB) '), upload = node('input'); label.className = 'lf-upload';
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
          if (generation === gen) await open(root, path);
        } catch (error) { if (generation === gen) status.textContent = error.message; upload.disabled = false; }
      };
      const fileList = node('div'); fileList.className = 'lf-list'; fileList.setAttribute('aria-label', 'Files in this folder'); body.append(fileList);
      if (!listing.files.length) { const empty = node('p', 'This folder is empty. Upload a file or create a folder to get started.'); empty.className = 'lf-empty'; fileList.append(empty); }
      const rows = [];
      for (const file of listing.files) {
        const row = node('article'); row.className = 'lf-row'; row.setAttribute('aria-label', file.name);
        const fileHeader = node('div'); fileHeader.className = 'lf-file-header';
        const extension = file.name.includes('.') ? file.name.split('.').pop().slice(0, 5).toUpperCase() : 'FILE';
        const badge = node('span', file.kind === 'folder' ? 'DIR' : extension); badge.className = 'lf-file-type'; badge.setAttribute('aria-hidden', 'true');
        const info = node('div'); info.className = 'lf-file-info'; info.append(node('strong', file.name));
        const metadata = [file.kind === 'folder' ? 'Folder' : sizeLabel(file.bytes)];
        if (Number.isFinite(file.modified_at)) {
          const date = new Date(file.modified_at * 1000);
          if (!Number.isNaN(date.getTime())) metadata.push('Modified ' + date.toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'}));
        }
        const meta = node('span', metadata.join(' · ')); meta.className = 'lf-file-meta'; info.append(meta);
        fileHeader.append(badge, info); row.append(fileHeader);
        const controls = node('div'); controls.className = 'lf-file-actions'; row.append(controls);
        if (file.kind === 'folder') button(controls, 'Open folder', () => open(root, file.path));
        else {
          const link = node('a', 'Download'); link.href = appPath('/v1/local-files/download?' + new URLSearchParams({root, path: file.path})); controls.append(link);
          const previewUrl = appPath('/v1/local-files/preview?' + new URLSearchParams({root, path: file.path}));
          if (/\.pdf$/i.test(file.name)) {
            const pdf = node('a', 'View PDF'); pdf.href = previewUrl; pdf.target = '_blank'; pdf.rel = 'noopener noreferrer';
            pdf.setAttribute('aria-label', `View ${file.name} in a new tab`); controls.append(pdf);
          } else if (/\.(png|jpe?g|gif|webp)$/i.test(file.name)) {
            const image = node('img'); image.className = 'local-file-image'; image.alt = file.name; image.hidden = true;
            image.onload = () => { if (generation === gen) status.textContent = `Previewing ${file.name}.`; };
            image.onerror = () => {
              image.hidden = true;
              if (generation === gen) status.textContent = 'This file could not be previewed. Its contents may not be a supported image, or access changed.';
            };
            row.append(image);
            button(controls, 'Preview image', () => {
              image.hidden = !image.hidden;
              if (!image.hidden && !image.getAttribute('src')) { status.textContent = 'Loading preview…'; image.src = previewUrl; }
            });
          }
          const preview = node('pre'); preview.hidden = true; row.append(preview);
          let nextOffset = null;
          const read = async start => {
            const result = await api('/v1/local-files/read?' + new URLSearchParams({root, path: file.path, offset: start}));
            if (generation !== gen) return;
            preview.textContent = result.text; preview.hidden = false; nextOffset = result.next_offset;
            more.hidden = nextOffset == null;
          };
          button(controls, 'Read', () => read(0));
          const more = button(controls, 'Next text', () => read(nextOffset)); more.hidden = true;
          button(controls, 'Edit with Simon', () => ask(root, file.path));
          if (file.name.toLowerCase().endsWith('.zip')) {
            button(controls, 'Inspect ZIP', async () => {
              const result = await action('local_zip_inspect', {root, path: file.path, offset: 0});
              if (generation !== gen) return;
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
              if (generation === gen) await open(targetRoot, destination.trim());
            });
          }
        }
        fileList.append(row); rows.push({row, name: file.name.toLocaleLowerCase()});
      }
      const emptyFilter = node('p', 'No file names match on this page.'); emptyFilter.className = 'lf-empty'; emptyFilter.hidden = true; fileList.append(emptyFilter);
      filter.oninput = () => {
        const query = filter.value.trim().toLocaleLowerCase();
        rows.forEach(({row, name}) => { row.hidden = !name.includes(query); });
        emptyFilter.hidden = !rows.length || rows.some(({row}) => !row.hidden);
      };
      const pagination = node('footer'); pagination.className = 'lf-pagination';
      const count = listing.total ?? listing.files.length;
      pagination.append(node('span', count ? `${offset + 1}–${offset + listing.files.length} of ${count} items` : '0 items'));
      if (offset) button(pagination, 'Previous files', () => open(root, path, Math.max(0, offset - 100)));
      if (listing.next_offset != null) button(pagination, 'Next files', () => open(root, path, listing.next_offset));
      body.append(pagination);
      status.textContent = 'Stored on your Simon server. Available wherever you sign in.';
    } catch (error) { if (generation === gen) status.textContent = error.message; }
  }
  window.SimonLocalFiles = {open};
  const launcher = node('button', 'Local files'); launcher.id = 'local-files-open'; launcher.type = 'button';
  launcher.onclick = () => open(); document.getElementById('work-refresh').after(launcher);
})();

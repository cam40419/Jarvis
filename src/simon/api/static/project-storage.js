/* global api */
'use strict';
(() => {
  const make = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const button = (label, action) => {
    const element = make('button', label, 'pc-button');
    element.type = 'button';
    element.onclick = action;
    return element;
  };
  const fields = [
    'id',
    'name',
    'provider',
    'connection_id',
    'account',
    'folder_id',
    'path',
    'writable',
    'linked_drive',
  ];
  const savedLocation = (location) => {
    const value = Object.fromEntries(
      fields.filter((key) => key in location).map((key) => [key, location[key]]),
    );
    if (value.linked_drive) {
      value.account = '';
      value.folder_id = null;
    }
    return value;
  };
  function render(target, project, onSaved) {
    const section = make('section', undefined, 'pc-file-section');
    section.id = 'pc-storage-locations';
    section.append(
      make('h3', 'File locations'),
      make(
        'p',
        'Choose every place this project keeps its files. Agents use available locations selected here.',
      ),
    );
    target.append(section);
    const feedback = make('p', 'Loading file locations...', 'muted');
    feedback.setAttribute('role', 'status');
    section.append(feedback);
    const endpoint = '/v1/projects/' + encodeURIComponent(project.id) + '/workspace/file-locations';
    let state, locations;
    const list = make('div');
    const browser = make('div');
    const describe = (location) => {
      if (location.provider === 'google_drive') return location.account || 'Linked Drive folder';
      const connection = state.connections.find((row) => row.id === location.connection_id);
      return (
        (connection?.name || location.provider.replaceAll('_', ' ')) +
        (location.path ? ' / ' + location.path : '')
      );
    };
    const browse = async (location, argumentsValue = {}) => {
      browser.replaceChildren(make('p', 'Loading files...'));
      try {
        const data = await api(endpoint + '/read', {
          location_id: location.id,
          operation: 'list',
          arguments: argumentsValue,
        });
        browser.replaceChildren(make('h4', location.name));
        if (Object.keys(argumentsValue).length)
          browser.append(button('Location root', () => browse(location)));
        const files = data.files || data.entries || data.items || data.value || [];
        if (!files.length) browser.append(make('p', 'This folder is empty.'));
        for (const file of files) {
          const row = make('div', undefined, 'pc-resource-card');
          row.append(make('strong', file.name || file.id || file.path || 'File'));
          const folder =
            file.kind === 'folder' ||
            file.type === 'folder' ||
            file['.tag'] === 'folder' ||
            Boolean(file.folder) ||
            file.mimeType === 'application/vnd.google-apps.folder';
          let args;
          if (location.provider === 'google_drive') args = { folder_id: file.id };
          else if (location.provider === 'box') args = { item_id: file.id };
          else
            args = {
              path: file.path || [argumentsValue.path, file.name].filter(Boolean).join('/'),
            };
          if (folder) row.append(button('Open folder', () => browse(location, args)));
          else
            row.append(
              button('Read file', async () => {
                const readArgs = location.provider === 'google_drive' ? { file_id: file.id } : args;
                try {
                  const value = await api(endpoint + '/read', {
                    location_id: location.id,
                    operation: 'read',
                    arguments: readArgs,
                  });
                  const content = make(
                    'pre',
                    value.text || value.content || JSON.stringify(value, null, 2),
                  );
                  content.className = 'pc-file-preview';
                  browser.append(content);
                } catch (error) {
                  feedback.textContent = error.message;
                }
              }),
            );
          browser.append(row);
        }
        if (data.next_page_token)
          browser.append(
            button('Next files', () =>
              browse(location, { ...argumentsValue, page_token: data.next_page_token }),
            ),
          );
        if (data.next_offset != null)
          browser.append(
            button('Next files', () =>
              browse(location, { ...argumentsValue, offset: data.next_offset }),
            ),
          );
        if (data.next_page != null)
          browser.append(
            button('Next files', () =>
              browse(location, { ...argumentsValue, page: data.next_page }),
            ),
          );
      } catch (error) {
        browser.replaceChildren(make('p', error.message, 'pc-notice error'));
      }
    };
    const renderList = () => {
      list.replaceChildren();
      for (const location of locations) {
        const row = make('div', undefined, 'pc-resource-card');
        row.append(
          make('strong', location.name),
          make('p', describe(location)),
          make(
            'p',
            location.state === 'unavailable'
              ? 'Unavailable. Agents will skip this location.'
              : location.can_write
                ? 'Read and write'
                : 'Read only',
          ),
        );
        const open = button('Browse ' + location.name, () => browse(location));
        open.disabled =
          location.state === 'unavailable' ||
          !state.locations.some((item) => item.id === location.id);
        row.append(
          open,
          button('Remove ' + location.name, () => {
            locations = locations.filter((item) => item.id !== location.id);
            renderList();
          }),
        );
        list.append(row);
      }
      if (!locations.length) list.append(make('p', 'No file locations selected.'));
    };
    api(endpoint)
      .then((data) => {
        if (!section.isConnected) return;
        state = data;
        locations = data.locations.map((location) => ({ ...location }));
        renderList();
        const form = make('form');
        form.className = 'pc-storage-form';
        const select = make('select');
        select.setAttribute('aria-label', 'Storage location');
        const option = (value, name) => {
          const item = make('option', name);
          item.value = value;
          select.append(item);
        };
        option('local', 'Project local folder');
        if (data.google_accounts.length) option('google_drive', 'Google Drive folder');
        if (project.drive?.enabled && project.drive.folder_id)
          option('linked-drive', 'Existing linked Drive folder');
        for (const connection of data.connections)
          option(connection.id, connection.name + ' (' + connection.provider + ')');
        const name = make('input');
        name.placeholder = 'Location name';
        name.maxLength = 120;
        name.setAttribute('aria-label', 'Location name');
        const path = make('input');
        path.placeholder = 'Subfolder (optional)';
        path.maxLength = 1000;
        path.setAttribute('aria-label', 'Storage subfolder');
        const writeLabel = make('label', ' Allow file writes');
        const writable = make('input');
        writable.type = 'checkbox';
        writable.checked = true;
        writeLabel.prepend(writable);
        select.onchange = () => {
          const provider =
            data.connections.find((row) => row.id === select.value)?.provider || select.value;
          path.hidden = !['local', 'dropbox', 'webdav'].includes(provider);
          writable.disabled = ['box', 'onedrive'].includes(provider);
        };
        select.onchange();
        const add = make('button', 'Add location', 'pc-button');
        add.type = 'submit';
        form.append(select, name, path, writeLabel, add);
        form.onsubmit = async (event) => {
          event.preventDefault();
          const connection = data.connections.find((row) => row.id === select.value);
          const provider =
            connection?.provider ||
            (select.value === 'linked-drive' ? 'google_drive' : select.value);
          const location = {
            id: 'location-' + crypto.randomUUID(),
            name:
              name.value.trim() ||
              connection?.name ||
              (provider === 'google_drive' ? 'Google Drive' : 'Project files'),
            provider,
            writable: writable.checked && !writable.disabled,
            path: path.hidden ? '' : path.value.trim(),
          };
          if (connection) location.connection_id = connection.id;
          const append = (chosen = {}) => {
            locations.push({ ...location, ...chosen });
            renderList();
            feedback.textContent = 'Save locations to apply these changes.';
            name.value = path.value = '';
          };
          if (select.value === 'linked-drive') {
            if (locations.some((item) => item.linked_drive)) return;
            append({ id: 'linked-drive', linked_drive: true });
          } else if (provider === 'google_drive') {
            await window.SimonWork.pickDriveFolder(project, append);
          } else append();
        };
        const save = button('Save file locations', async () => {
          save.disabled = true;
          feedback.textContent = 'Saving file locations...';
          try {
            const result = await api(
              endpoint,
              { expected_version: state.version, locations: locations.map(savedLocation) },
              'PUT',
            );
            state = { ...state, ...result };
            locations = result.locations.map((location) => ({ ...location }));
            renderList();
            feedback.textContent =
              'File locations saved. Unavailable locations are skipped by agents.';
            window.dispatchEvent(new Event('simon-connections-change'));
            onSaved?.(result);
          } catch (error) {
            feedback.textContent = error.message;
          } finally {
            save.disabled = false;
          }
        });
        section.append(list, form, save, browser);
        feedback.textContent = '';
      })
      .catch((error) => {
        feedback.textContent = error.message;
      });
    return section;
  }
  window.SimonProjectStorage = { render };
})();

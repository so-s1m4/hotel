/** Upload original ZIP bytes only to the local Hotel server; never start inference. */
export const DATASET_ZIP_LIMIT = 128 * 1024 * 1024;
const IMPORT_PATH = '/api/datasets/import';

function validationMessage(file, maxBytes) {
  if (!file || typeof file.name !== 'string' || !/\.zip$/i.test(file.name)) return 'Choose a .zip dataset archive.';
  if (!Number.isSafeInteger(file.size) || file.size <= 0) return 'This ZIP is empty. Choose a dataset archive containing the original files.';
  if (file.size > maxBytes) return `This ZIP is too large. Choose an archive no larger than ${Math.floor(maxBytes / 1024 / 1024)} MiB.`;
  return null;
}

function serverMessage(response, payload) {
  // The server returns controlled validation explanations. Render only text,
  // never HTML or an exception containing request headers or filesystem paths.
  const provided = payload?.error?.message;
  if (typeof provided === 'string' && provided.trim() && provided.length <= 1000) return provided;
  if (response.status === 413) return 'The archive exceeds the local import size limits. Choose a smaller ZIP.';
  if (response.status === 415) return 'Choose an unencrypted .zip dataset archive.';
  if ([401, 403].includes(response.status)) return 'The local server rejected this upload. Reload the Hotel page and try again.';
  if (response.status === 409) return 'The dataset cannot be imported right now. Wait for the current operation to finish, then retry.';
  if (response.status === 503) return 'The local Hotel server cannot accept imports right now. Try again shortly.';
  if (response.status >= 500) return 'The local server could not import the archive. Try again.';
  return 'The archive could not be validated. Check that it contains one complete, unencrypted Hotel dataset.';
}

/**
 * host: playbackControls.importHost; onImported receives one /api/datasets item.
 * Selecting that item is separate from pressing Prepare dataset (model work).
 * A native File body avoids base64, multipart wrapping and a second JS byte copy.
 */
export function createDatasetImport({host, onImported, captureViewIntent = () => undefined, toast = () => {}, fetchImpl = globalThis.fetch,
  documentRef = host?.ownerDocument || globalThis.document, maxBytes = DATASET_ZIP_LIMIT} = {}) {
  if (!host || typeof host.appendChild !== 'function' || !documentRef) throw new TypeError('An upload host is required');
  if (typeof onImported !== 'function' || typeof fetchImpl !== 'function' || typeof captureViewIntent !== 'function') throw new TypeError('Import callbacks are required');
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) throw new RangeError('Invalid ZIP size limit');
  const wrapper = documentRef.createElement('div'); wrapper.className = 'dataset-import';
  const button = documentRef.createElement('button'); button.type = 'button'; button.className = 'button ghost'; button.textContent = 'Upload ZIP';
  button.title = `Import a local dataset ZIP, up to ${Math.floor(maxBytes / 1024 / 1024)} MiB`;
  const input = documentRef.createElement('input'); input.type = 'file'; input.accept = '.zip,application/zip'; input.multiple = false; input.hidden = true;
  input.setAttribute('aria-label', 'Choose a Hotel dataset ZIP');
  const status = documentRef.createElement('span'); status.className = 'import-status'; status.hidden = true;
  status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); status.setAttribute('aria-atomic', 'true');
  wrapper.appendChild(button); wrapper.appendChild(input); wrapper.appendChild(status); host.appendChild(wrapper);
  let destroyed = false, busy = false, controller = null;
  let current = {stage: 'idle', message: '', datasetId: null, duplicate: false, selected: false};

  function show(stage, message, extra = {}) {
    current = {...current, ...extra, stage, message};
    wrapper.dataset.state = stage; wrapper.setAttribute('aria-busy', String(stage === 'uploading'));
    status.textContent = message; status.hidden = !message;
    button.disabled = input.disabled = busy || destroyed;
    button.textContent = busy ? 'Uploading…' : 'Upload ZIP';
  }
  function error(message) {
    show('error', message, {datasetId: null, duplicate: false, selected: false}); toast(message, true);
  }
  async function upload(file) {
    if (destroyed || busy) return null;
    const invalid = validationMessage(file, maxBytes);
    if (invalid) {error(invalid); return null;}
    busy = true; controller = new AbortController();
    show('uploading', 'Uploading and checking the ZIP locally…', {datasetId: null, duplicate: false, selected: false});
    let imported = null;
    try {
      const viewIntent = captureViewIntent();
      const response = await fetchImpl(IMPORT_PATH, {
        method: 'POST', headers: {'Content-Type': 'application/zip'}, body: file,
        credentials: 'same-origin', mode: 'same-origin', redirect: 'error', signal: controller.signal,
      });
      if (destroyed) return null;
      let payload = null;
      try {payload = await response.json();} catch { /* A non-JSON error remains a generic local error. */ }
      if (destroyed) return null;
      if (!response.ok) {error(serverMessage(response, payload)); return null;}
      const dataset = payload?.dataset;
      if (!dataset || !/^dataset-[a-f0-9]{64}$/.test(dataset.id) || typeof dataset.name !== 'string'
          || !dataset.name.trim() || dataset.available !== true || typeof payload.duplicate !== 'boolean') {
        error('The server did not confirm a usable dataset. Retry the same ZIP or reload the page.'); return null;
      }
      imported = dataset;
      const selected = await onImported(dataset, viewIntent) !== false;
      if (destroyed) return null;
      const prepared = typeof dataset.replay_id === 'string' && /^replay-[a-f0-9]{32}$/.test(dataset.replay_id);
      const next = prepared ? 'Ready to play.' : 'Choose Prepare dataset to begin.';
      const message = !selected ? 'Imported. Your current view was kept; find the new dataset in the list.'
        : payload.duplicate ? `This dataset is already imported and selected. ${next}`
          : `Dataset imported and selected. ${next}`;
      show('ready', message, {datasetId: dataset.id, duplicate: payload.duplicate, selected}); toast(message);
      return payload;
    } catch {
      if (!destroyed) error(imported
        ? 'The ZIP was imported, but could not be selected. Refresh the page to load it.'
        : 'Could not complete the upload to the local Hotel server. Keep it running and retry the same ZIP.');
      return null;
    } finally {
      busy = false; controller = null;
      if (!destroyed) show(current.stage, current.message);
    }
  }
  const choose = () => { if (!busy && !destroyed) {input.value = ''; input.click();} };
  const chosen = () => {const file = input.files?.[0]; input.value = ''; if (file) void upload(file);};
  button.addEventListener('click', choose); input.addEventListener('change', chosen);
  return {
    upload,
    state: () => ({...current}),
    destroy() {
      if (destroyed) return;
      destroyed = true; controller?.abort();
      button.removeEventListener('click', choose); input.removeEventListener('change', chosen);
      wrapper.remove(); current = {...current, stage: 'destroyed'};
    },
  };
}

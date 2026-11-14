import test from 'node:test';
import assert from 'node:assert/strict';
import {createDatasetImport, DATASET_ZIP_LIMIT} from './import.js';

class Element {
  constructor(tag, document) {this.tagName = tag; this.ownerDocument = document; this.children = []; this.dataset = {}; this.attributes = {}; this.handlers = new Map(); this.clicks = 0;}
  appendChild(child) {this.children.push(child); child.parent = this;}
  setAttribute(name, value) {this.attributes[name] = value;}
  addEventListener(name, handler) {this.handlers.set(name, handler);}
  removeEventListener(name) {this.handlers.delete(name);}
  click() {this.clicks++; if (!this.disabled) this.handlers.get('click')?.();}
  remove() {this.parent.children = this.parent.children.filter(child => child !== this);}
}
const zip = (updates = {}) => ({name: 'Hotel.zip', size: 2048, ...updates});
const dataset = {id: 'dataset-' + 'a'.repeat(64), name: 'New Hotel', available: true, imported: true, replay_id: null};
const response = (status = 200, body = {dataset, duplicate: false}) => ({ok: status >= 200 && status < 300, status, json: async () => body});
function harness(options = {}) {
  const document = {createElement(tag) {return new Element(tag, document);}};
  const host = new Element('div', document), calls = [], imported = [], notices = [];
  const ui = createDatasetImport({host, onImported: item => imported.push(item), toast: (...args) => notices.push(args),
    fetchImpl: async (...args) => {calls.push(args); return response();}, ...options});
  const [wrapper] = host.children, [button, input, status] = wrapper.children;
  return {ui, host, wrapper, button, input, status, calls, imported, notices};
}

const settle = async () => {for (let i = 0; i < 8; i++) await Promise.resolve();};

test('native ZIP picker opens without uploading; cancelling leaves state unchanged', async () => {
  const h = harness();
  assert.equal(h.input.accept, '.zip,application/zip');
  h.button.click(); assert.equal(h.input.clicks, 1); assert.equal(h.calls.length, 0);
  h.input.files = []; h.input.handlers.get('change')(); await settle();
  assert.equal(h.ui.state().stage, 'idle'); assert.equal(h.calls.length, 0);
});

test('uploads exact File body locally, selects the returned dataset, and never prepares a replay', async () => {
  const h = harness(), file = zip(); const result = await h.ui.upload(file);
  assert.equal(h.calls.length, 1); const [url, options] = h.calls[0];
  assert.equal(url, '/api/datasets/import'); assert.equal(options.method, 'POST');
  assert.equal(options.body, file); assert.deepEqual(options.headers, {'Content-Type': 'application/zip'});
  assert.equal(options.mode, 'same-origin'); assert.equal(options.redirect, 'error'); assert.equal(options.credentials, 'same-origin');
  assert.deepEqual(h.imported, [dataset]); assert.deepEqual(result.dataset, dataset);
  assert.equal(h.ui.state().stage, 'ready'); assert.match(h.status.textContent, /Choose Prepare dataset/);
  assert.equal(h.button.disabled, false); assert.equal(h.wrapper.attributes['aria-busy'], 'false');
});

test('file selection resets the native input so the same archive can be retried', async () => {
  const h = harness(); h.input.files = [zip()]; h.input.value = 'selected.zip';
  h.input.handlers.get('change')(); await settle();
  assert.equal(h.input.value, ''); assert.equal(h.calls.length, 1); assert.equal(h.ui.state().stage, 'ready');
});

for (const [name, file, message] of [
  ['wrong extension', zip({name: 'data.csv'}), /\.zip/],
  ['empty archive', zip({size: 0}), /empty/],
  ['oversized ZIP', zip({size: DATASET_ZIP_LIMIT + 1}), /128 MiB/],
  ['invalid size', zip({size: Infinity}), /empty/],
]) test(`rejects ${name} before any network or selection`, async () => {
  const h = harness(); await h.ui.upload(file);
  assert.equal(h.calls.length, 0); assert.deepEqual(h.imported, []);
  assert.equal(h.ui.state().stage, 'error'); assert.match(h.status.textContent, message);
});

test('accepts uppercase ZIP and exactly the size limit without reading its bytes into JavaScript', async () => {
  const h = harness(); const file = zip({name: 'DATA.ZIP', size: DATASET_ZIP_LIMIT,
    arrayBuffer() {throw new Error('Do not buffer ZIP');}, text() {throw new Error('Do not decode ZIP');}});
  await h.ui.upload(file); assert.equal(h.calls[0][1].body, file); assert.equal(h.ui.state().stage, 'ready');
});

test('pending upload disables choosing and ignores a second submission', async () => {
  let finish, signal; const h = harness({fetchImpl: (url, options) => {signal = options.signal; return new Promise(resolve => {finish = resolve;});}});
  const first = h.ui.upload(zip()); assert.equal(h.button.disabled, true); assert.equal(h.input.disabled, true);
  assert.equal(h.ui.state().stage, 'uploading'); assert.match(h.status.textContent, /Uploading and checking/);
  assert.equal(await h.ui.upload(zip({name: 'second.zip'})), null); h.button.click(); assert.equal(h.input.clicks, 0);
  assert.equal(signal.aborted, false); finish(response()); await first;
  assert.deepEqual(h.imported, [dataset]); assert.equal(h.button.disabled, false);
});

test('duplicate archive is a success and still selects the existing dataset', async () => {
  const h = harness({fetchImpl: async () => response(200, {dataset, duplicate: true})});
  await h.ui.upload(zip()); assert.deepEqual(h.imported, [dataset]);
  assert.equal(h.ui.state().duplicate, true); assert.match(h.status.textContent, /already imported/);
});

test('import stays successful without claiming selection while preparation is opening', async () => {
  const h = harness({onImported: async () => false});
  await h.ui.upload(zip());
  assert.equal(h.ui.state().stage, 'ready'); assert.equal(h.ui.state().datasetId, dataset.id);
  assert.equal(h.ui.state().selected, false);
  assert.equal(h.status.textContent, 'Imported. Your current view was kept; find the new dataset in the list.');
  assert.notEqual(h.notices[0][1], true);
});

test('duplicate with a saved replay is ready to play without suggesting another preparation', async () => {
  const prepared = {...dataset, replay_id: 'replay-' + 'b'.repeat(32)};
  const h = harness({fetchImpl: async () => response(200, {dataset: prepared, duplicate: true})});
  await h.ui.upload(zip());
  assert.deepEqual(h.imported, [prepared]); assert.equal(h.ui.state().selected, true);
  assert.match(h.status.textContent, /Ready to play/); assert.doesNotMatch(h.status.textContent, /Prepare dataset/);
});

for (const [code, status, message] of [
  ['encrypted_zip', 400, 'Decrypt the ZIP first, then upload the unencrypted archive.'],
  ['missing_files', 400, 'The archive is missing guest_requests.csv.'],
  ['archive_too_large', 413, 'Expanded archive exceeds 512 MiB.'],
  ['unsafe_path', 400, 'The archive contains unsafe paths.'],
  ['invalid_schema', 400, 'The dataset manifest is invalid.'],
]) test(`shows actionable ${code} response without selecting or hiding the failure`, async () => {
  const h = harness({fetchImpl: async () => response(status, {error: {code, message}})});
  await h.ui.upload(zip()); assert.deepEqual(h.imported, []); assert.equal(h.status.textContent, message);
  assert.equal(h.ui.state().stage, 'error'); assert.equal(h.notices[0][1], true); assert.equal(h.button.disabled, false);
});

test('server messages are text only, even when an archive name contains markup', async () => {
  const h = harness({fetchImpl: async () => response(400, {error: {message: 'Invalid archive <img src=x onerror=alert(1)>'}})});
  await h.ui.upload(zip()); assert.match(h.status.textContent, /<img/); assert.equal(h.status.innerHTML, undefined);
});

test('non-JSON server failures remain understandable and retryable', async () => {
  const h = harness({fetchImpl: async () => ({ok: false, status: 503, json: async () => {throw new Error('Internal trace');}})});
  await h.ui.upload(zip()); assert.match(h.status.textContent, /cannot accept imports/);
  assert.doesNotMatch(h.status.textContent, /Internal trace/); assert.equal(h.button.disabled, false);
});

test('network failure can be retried with the same ZIP and does not repeat automatically', async () => {
  let calls = 0; const h = harness({fetchImpl: async () => {calls++; if (calls === 1) throw new Error('Private network detail'); return response();}});
  await h.ui.upload(zip()); assert.equal(calls, 1); assert.equal(h.ui.state().stage, 'error'); assert.deepEqual(h.imported, []);
  assert.doesNotMatch(h.status.textContent, /Private/); await h.ui.upload(zip());
  assert.equal(calls, 2); assert.equal(h.ui.state().stage, 'ready');
});

for (const body of [{dataset}, {dataset: {...dataset, id: '../private'}, duplicate: false},
  {dataset: {...dataset, available: false}, duplicate: false}, {dataset: {...dataset, name: ''}, duplicate: false}]) {
  test(`malformed successful response cannot pretend the dataset is ready: ${JSON.stringify(body)}`, async () => {
    const h = harness({fetchImpl: async () => response(200, body)});
    await h.ui.upload(zip()); assert.equal(h.ui.state().stage, 'error'); assert.deepEqual(h.imported, []);
  });
}

test('selection failure reports that import succeeded instead of pretending bytes were rejected', async () => {
  const h = harness({onImported: () => {throw new Error('Private selection failure');}});
  await h.ui.upload(zip()); assert.equal(h.ui.state().stage, 'error'); assert.match(h.status.textContent, /was imported, but could not be selected/);
});

test('destroy aborts the request, removes controls, and suppresses late results and callbacks', async () => {
  let finish, signal; const h = harness({fetchImpl: (url, options) => {signal = options.signal; return new Promise(resolve => {finish = resolve;});}});
  const pending = h.ui.upload(zip()); h.ui.destroy(); h.ui.destroy();
  assert.equal(signal.aborted, true); assert.equal(h.host.children.length, 0);
  finish(response()); await pending; assert.deepEqual(h.imported, []); assert.deepEqual(h.notices, []);
  assert.equal(h.ui.state().stage, 'destroyed'); assert.equal(await h.ui.upload(zip()), null);
});

test('destroy suppresses a late fetch rejection', async () => {
  let fail; const h = harness({fetchImpl: () => new Promise((resolve, reject) => {fail = reject;})});
  const pending = h.ui.upload(zip()); h.ui.destroy(); fail(new Error('Aborted request detail')); await pending;
  assert.deepEqual(h.notices, []); assert.deepEqual(h.imported, []);
});

test('selection receives the view intent from upload start, not after the response', async () => {
  let finish, intent = 4; const received = [];
  const h = harness({captureViewIntent: () => intent,
    fetchImpl: () => new Promise(resolve => {finish = resolve;}),
    onImported: (item, startedIntent) => {received.push([item, startedIntent]); return startedIntent === intent;}});
  const upload = h.ui.upload(zip()); intent = 5;
  finish(response()); await upload;
  assert.deepEqual(received, [[dataset, 4]]);
  assert.equal(h.ui.state().selected, false);
  assert.match(h.status.textContent, /current view was kept/);
});

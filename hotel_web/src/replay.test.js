import test from 'node:test';
import assert from 'node:assert/strict';
import {createReplayControls} from './replay.js';

const id = `replay-${'a'.repeat(32)}`;
const manifest = {job_id: id, dataset_id: 'dev', start_utc: '2026-01-01T00:00:00Z',
  end_utc: '2026-01-02T00:00:00Z', total_events: 20, total_guests: 2};
const baseProgress = {events_processed: 0, total_events: 20, guest_processed: 0, total_guests: 2,
  decisions: 0, autonomous_count: 0, pending_approval_count: 0, llm_count: 0, fallback_count: 0};
const job = {id, dataset_id: 'dev', status: 'completed', complete: true, artifacts: {},
  progress: {...baseProgress, events_processed: 20, guest_processed: 2, decisions: 100, autonomous_count: 99}};
const frame = (count = 0) => ({events_consumed: count, source_ordinal: count, journal_total: count,
  progress: {...baseProgress, events_processed: count, autonomous_count: count},
  as_of_utc: count ? manifest.end_utc : manifest.start_utc,
  snapshot: {rooms: [], people: [], recent_messages: count ? [{id: 'future-guest'}] : [], pending_approvals: [], decisions: []}});
const settle = () => new Promise(resolve => setImmediate(resolve));

function setup(t, overrides = {}) {
  const elements = new Map(), calls = [], views = [], journals = [], errors = [], demos = [], raf = new Map();
  const browserLocation = new URL(overrides.url || 'http://127.0.0.1:8765/');
  const stored = new Map();
  function element(key) {
    if (!elements.has(key)) elements.set(key, {value: key === '#dataset-select' ? 'dev' : key === '#playback-speed' ? 'tour' : '0',
      options: [{value: 'dev'}, {value: 'quickstart'}], textContent: '', hidden: false, disabled: false,
      attributes: {}, setAttribute(name, value) {this.attributes[name] = value;},
      append(option) {this.options.push(option);}, remove() {}, before() {}, querySelector: element});
    return elements.get(key);
  }
  const globals = {
    document: {createElement: tag => tag === 'option' ? {} : element('panel'), querySelector: element},
    location: browserLocation,
    history: {state: {existing: true}, replaceState(state, title, url) {browserLocation.href = url;}},
    localStorage: {getItem: key => stored.get(key) || null, setItem: (key, value) => stored.set(key, value)},
    requestAnimationFrame: callback => {const key = Symbol(); raf.set(key, callback); return key;},
    cancelAnimationFrame: key => raf.delete(key),
  };
  const previous = new Map(Object.keys(globals).map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, {value, configurable: true});
  const api = async (path, body) => {
    calls.push({path, body});
    if (body) return overrides.post ? overrides.post(path, body) : {job};
    if (path === '/api/datasets') return overrides.catalog ? overrides.catalog() : {datasets: overrides.datasets || [{id: 'dev', name: 'Dev', available: true, replay_id: id}]};
    if (path.endsWith('/playback')) return overrides.manifest ? overrides.manifest(path) : manifest;
    if (path.includes('/frame?')) return overrides.frame ? overrides.frame(path) : frame();
    return {job: overrides.job ? overrides.job(path) : job, snapshot: {recent_messages: [{id: 'must-not-leak-final-state'}]}, total: 999};
  };
  const start = () => createReplayControls({api, escape: value => value, toast: message => errors.push(message),
    onView: value => views.push(value), onJournal: (...args) => journals.push(args), onDemo() {demos.push(true);}});
  let control = start();
  t.after(() => {
    control.destroy();
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  });
  return {element, calls, views, journals, errors, demos, location: browserLocation, get control() {return control;},
    reload() {control.destroy(); control = start();}};
}

test('fresh browser opens the saved Dev run paused at zero without preparation', async t => {
  const h = setup(t); await settle();
  assert.ok(h.calls.some(call => call.path.endsWith('/frame?cursor=0')));
  assert.ok(h.calls.every(call => call.body === undefined));
  assert.equal(h.control.getPlayback().playing, false);
  assert.equal(h.control.getPlayback().before_first, true);
  assert.ok(h.views.length >= 2);
  assert.ok(h.views.every(view => view.job.progress.autonomous_count === 0));
  assert.ok(h.views.every(view => !view.snapshot.recent_messages?.length));
  assert.ok(h.journals.every(([, count]) => count === 0));
  assert.equal(h.element('#playback-toggle').disabled, false);
});

test('end seek clears future UI and stays Finishing until final frame arrives', async t => {
  let finish;
  const h = setup(t, {frame: path => path.endsWith('cursor=0') ? frame() : new Promise(resolve => {finish = resolve;})});
  await settle();
  const slider = h.element('#playback-range'); slider.value = '10000'; slider.oninput();
  assert.equal(h.views.at(-1).job.progress.autonomous_count, 0);
  assert.equal(h.element('#playback-status').textContent, 'Finishing…');
  assert.equal(h.element('#playback-toggle').disabled, true);
  finish(frame(20)); await settle();
  assert.equal(h.element('#playback-status').textContent, 'Complete');
  assert.equal(h.views.at(-1).job.progress.autonomous_count, 20);
  assert.equal(h.element('#playback-toggle').disabled, false);
  h.element('#playback-toggle').onclick();
  assert.equal(h.views.at(-1).job.progress.autonomous_count, 0);
  assert.deepEqual(h.views.at(-1).snapshot, {});
});

test('manifest read failure offers retry that does not start a model run', async t => {
  let attempts = 0;
  const h = setup(t, {manifest: () => {if (++attempts === 1) throw new Error('Temporarily unavailable'); return manifest;}});
  await settle();
  assert.equal(h.element('#playback-retry').hidden, false);
  assert.equal(h.element('#playback-toggle').disabled, true);
  h.element('#playback-retry').onclick(); await settle();
  assert.equal(h.element('#playback-toggle').disabled, false);
  assert.equal(attempts, 2);
  assert.ok(h.calls.every(call => call.body === undefined));
});

test('preparation disables source switching while its POST is outstanding', async t => {
  let finish;
  const h = setup(t, {post: () => new Promise(resolve => {finish = resolve;})});
  await settle();
  const pending = h.element('#run-dataset').onclick();
  assert.equal(h.element('#dataset-select').disabled, true);
  assert.equal(h.element('#run-dataset').disabled, true);
  assert.equal(h.element('#playback-toggle').disabled, true);
  finish({job}); await pending; await settle();
  assert.equal(h.element('#dataset-select').disabled, false);
});

test('source switch updates a deep link and reload preserves it plus other URL state', async t => {
  const quickstartId = `replay-${'b'.repeat(32)}`;
  const datasetFor = path => path.includes(quickstartId) ? 'quickstart' : 'dev';
  const h = setup(t, {
    url: `http://127.0.0.1:8765/?replay=${id}&presentation=1#hotel`,
    datasets: [{id: 'dev', name: 'Dev', available: true, replay_id: id},
      {id: 'quickstart', name: 'Quickstart', available: true, replay_id: quickstartId}],
    job: path => ({...job, id: datasetFor(path) === 'quickstart' ? quickstartId : id, dataset_id: datasetFor(path)}),
    manifest: path => ({...manifest, job_id: datasetFor(path) === 'quickstart' ? quickstartId : id, dataset_id: datasetFor(path)}),
  });
  await settle();
  h.element('#dataset-select').value = 'quickstart'; h.element('#dataset-select').onchange();
  await settle();
  assert.equal(h.location.searchParams.get('replay'), quickstartId);
  assert.equal(h.location.searchParams.get('presentation'), '1');
  assert.equal(h.location.hash, '#hotel');
  assert.deepEqual(history.state, {existing: true});
  h.calls.length = 0; h.reload(); await settle();
  assert.equal(h.element('#dataset-select').value, 'quickstart');
  assert.ok(h.calls.some(call => call.path.includes(`${quickstartId}/frame?cursor=0`)));
  assert.ok(!h.calls.some(call => call.path.includes(`${id}/frame`)));
});

test('switching to an unprepared dataset removes only the replay URL parameter', async t => {
  const h = setup(t, {url: `http://127.0.0.1:8765/?replay=${id}&presentation=1#hotel`});
  await settle();
  h.element('#dataset-select').value = 'quickstart'; h.element('#dataset-select').onchange();
  assert.equal(h.location.searchParams.has('replay'), false);
  assert.equal(h.location.searchParams.get('presentation'), '1');
  assert.equal(h.location.hash, '#hotel');
  assert.equal(h.control.isViewing(), true);
  assert.equal(h.demos.length, 0);
  assert.deepEqual(h.views.at(-1).snapshot, {});
  assert.equal(h.views.at(-1).job.dataset_id, 'quickstart');
  assert.equal(h.views.at(-1).job.progress.autonomous_count, 0);
  assert.equal(h.element('#replay-progress').hidden, false);
  assert.equal(h.element('#replay-progress').textContent, 'Prepare this dataset once to enable Play.');
  assert.ok(h.calls.every(call => call.body === undefined));
});

test('opening a saved timeline displays loading without losing its preparation status', async t => {
  let finish;
  const h = setup(t, {manifest: () => new Promise(resolve => {finish = resolve;})});
  await settle();
  assert.equal(h.element('#playback-toggle').textContent, '◌ Loading…');
  assert.equal(h.element('#playback-toggle').attributes['aria-busy'], 'true');
  assert.equal(h.element('#replay-progress').hidden, false);
  assert.equal(h.element('#run-dataset').disabled, true);
  finish({...manifest, operational_start_utc: '2026-01-01T06:00:00Z'}); await settle();
  assert.equal(h.element('#playback-toggle').attributes['aria-busy'], 'false');
  assert.equal(h.element('#playback-phase').textContent, 'Early reservations');
});

test('manual sandbox choice during Prepare POST survives the delayed response', async t => {
  let finish;
  const h = setup(t, {post: () => new Promise(resolve => {finish = resolve;})});
  await settle();
  const pending = h.element('#run-dataset').onclick();
  h.element('#sandbox-mode').onclick();
  const shown = h.views.length;
  assert.equal(h.control.isViewing(), false);
  finish({job}); await pending; await settle();
  assert.equal(h.control.isViewing(), false);
  assert.equal(h.views.length, shown);
  assert.equal(h.element('#revisit-dataset').hidden, false);
});

test('imported dataset is selected by its own name without automatically preparing it', async t => {
  const h = setup(t); await settle();
  const imported = {id: `dataset-${'c'.repeat(64)}`, name: 'Spring hotel data', available: true};
  const selected = h.control.addDataset(imported);
  assert.equal(selected, true);
  assert.equal(h.element('#dataset-select').value, imported.id);
  assert.equal(h.element('#source-caption').textContent, 'Spring hotel data · prepare to begin');
  assert.equal(h.control.getPlayback(), null);
  assert.equal(h.control.isViewing(), true);
  assert.equal(h.demos.length, 0);
  assert.deepEqual(h.views.at(-1).snapshot, {});
  assert.equal(h.views.at(-1).job.dataset_id, imported.id);
  assert.equal(h.views.at(-1).job.progress.autonomous_count, 0);
  assert.equal(h.journals.at(-1)[0], null);
  assert.equal(h.journals.at(-1)[1], 0);
  assert.equal(h.element('#replay-progress').hidden, false);
  assert.equal(h.element('#replay-progress').textContent, 'Prepare this dataset once to enable Play.');
  assert.ok(h.calls.every(call => call.body === undefined));
});

test('import during Prepare POST registers the dataset without replacing the pending source', async t => {
  let finish;
  const h = setup(t, {post: () => new Promise(resolve => {finish = resolve;})});
  await settle();
  const pending = h.element('#run-dataset').onclick();
  const imported = {id: `dataset-${'d'.repeat(64)}`, name: 'Next hotel data', available: true};
  assert.equal(h.control.addDataset(imported), false);
  assert.ok(h.element('#dataset-select').options.some(option => option.value === imported.id));
  assert.equal(h.element('#dataset-select').value, 'dev');
  finish({job}); await pending; await settle();
  assert.equal(h.element('#dataset-select').value, 'dev');
});

test('degraded archive retains a preparation warning without revealing future operation counters', async t => {
  const h = setup(t, {job: () => ({...job, status: 'degraded', progress: {...job.progress, fallback_count: 2}})});
  await settle();
  assert.equal(h.element('.replay-quality').hidden, false);
  assert.equal(h.element('.replay-quality').textContent, 'Prepared with 2 fallbacks.');
  assert.equal(h.views.at(-1).job.progress.fallback_count, 0);
});

test('late initial catalog cannot replace an explicit Sandbox choice', async t => {
  let finish;
  const h = setup(t, {catalog: () => new Promise(resolve => {finish = resolve;})});
  h.element('#sandbox-mode').onclick();
  assert.equal(h.control.isViewing(), false);
  finish({datasets: [{id: 'dev', name: 'Dev', available: true, replay_id: id}]});
  await settle();
  assert.equal(h.control.isViewing(), false);
  assert.equal(h.control.getPlayback(), null);
  assert.equal(h.views.length, 0);
  assert.equal(h.demos.length, 1);
  assert.deepEqual(h.calls.map(call => call.path), ['/api/datasets']);
});

test('fresh catalog restores prepared and unprepared imports without selecting or preparing them', async t => {
  const imported = {id: `dataset-${'e'.repeat(64)}`, name: 'Saved spring data', available: true};
  const prepared = {id: `dataset-${'f'.repeat(64)}`, name: 'Saved winter data', available: true, replay_id: `replay-${'b'.repeat(32)}`};
  const h = setup(t, {datasets: [{id: 'dev', name: 'Dev', available: true}, imported, prepared]});
  await settle();
  const select = h.element('#dataset-select');
  assert.equal(select.value, 'dev');
  assert.equal(select.options.filter(option => option.value === imported.id).length, 1);
  assert.equal(select.options.find(option => option.value === prepared.id).textContent, prepared.name);
  assert.deepEqual(h.calls.map(call => call.path), ['/api/datasets']);
  select.value = imported.id; select.onchange();
  assert.equal(h.views.at(-1).job.dataset_id, imported.id);
  assert.equal(h.element('#playback-status').textContent, 'Preparation required');
  assert.ok(h.calls.every(call => call.body === undefined));
});

test('deep-linked import has a valid selector before a delayed catalog and Prepare sends its ID', async t => {
  const imported = {id: `dataset-${'e'.repeat(64)}`, name: 'Restored hotel', available: true, replay_id: id};
  let finishCatalog;
  const h = setup(t, {url: `http://127.0.0.1:8765/?replay=${id}`,
    catalog: () => new Promise(resolve => {finishCatalog = resolve;}),
    job: () => ({...job, dataset_id: imported.id}), post: () => ({job: {...job, dataset_id: imported.id}})});
  await settle();
  const select = h.element('#dataset-select');
  assert.ok(select.options.some(option => option.value === imported.id));
  assert.equal(select.value, imported.id);
  finishCatalog({datasets: [{id: 'dev', name: 'Dev', available: true}, imported]}); await settle();
  assert.equal(select.options.filter(option => option.value === imported.id).length, 1);
  assert.equal(select.options.find(option => option.value === imported.id).textContent, imported.name);
  assert.equal(select.value, imported.id);
  await h.element('#run-dataset').onclick(); await settle();
  assert.equal(h.calls.find(call => call.path === '/api/replays').body.dataset_id, imported.id);
});

for (const choice of ['sandbox', 'dataset', 'archive']) {
  test(`late upload registers its dataset but preserves a newer ${choice} choice`, async t => {
    const h = setup(t); await settle();
    const uploadIntent = h.control.viewIntent();
    if (choice === 'dataset') {
      h.element('#dataset-select').value = 'quickstart'; h.element('#dataset-select').onchange();
    } else {
      h.element('#sandbox-mode').onclick();
      if (choice === 'archive') h.element('#revisit-dataset').onclick();
    }
    const selection = h.element('#dataset-select').value, shown = h.views.length;
    const viewing = h.control.isViewing(), calls = h.calls.length;
    const imported = {id: `dataset-${'d'.repeat(64)}`, name: 'Late upload', available: true, replay_id: `replay-${'c'.repeat(32)}`};
    assert.equal(h.control.addDataset(imported, {viewIntent: uploadIntent}), false);
    assert.ok(h.element('#dataset-select').options.some(option => option.value === imported.id));
    assert.equal(h.element('#dataset-select').value, selection);
    assert.equal(h.control.isViewing(), viewing);
    assert.equal(h.views.length, shown);
    assert.equal(h.calls.length, calls);
  });
}

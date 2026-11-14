import {createPlaybackClock, createFrameLoader, PLAYBACK_SPEEDS} from './playback.js';

/** Prepare once, then play the archived causal history without new inference. */
export function createReplayControls({api, escape, toast, onView, onDemo, onJournal,
  onClock = () => {}}) {
  const panel = document.createElement('section');
  panel.className = 'dataset-panel playback-panel panel';
  panel.innerHTML = `
    <div class="playback-header dataset-intro"><span class="eyebrow">HOTEL TIMELINE</span>
      <strong id="source-caption">Choose a dataset to explore</strong></div>
    <div class="playback-actions dataset-actions">
      <label class="sr-only" for="dataset-select">Dataset</label>
      <select id="dataset-select"><option value="dev">Dev</option><option value="quickstart">Quickstart</option></select>
      <button class="button primary" id="playback-toggle" disabled>▶ Play</button>
      <button class="button ghost" id="playback-restart" disabled aria-label="Restart from the first event">↺ Restart</button>
      <label class="sr-only" for="playback-speed">Playback speed</label>
      <select id="playback-speed"><option value="tour">3-minute tour</option>${PLAYBACK_SPEEDS.map(item => `<option value="${item.value}">${item.label}</option>`).join('')}</select>
    </div>
    <div class="playback-clock"><time id="playback-clock">—:—:—</time><span id="playback-date">Ready when you are</span></div>
    <div class="playback-track">
      <label class="sr-only" for="playback-range">Position in the dataset</label>
      <input class="playback-range" id="playback-range" type="range" min="0" max="10000" step="1" value="0" disabled>
      <div class="playback-progress"><span id="playback-event-count">0 events</span><span id="playback-phase">Ready</span><span id="playback-current-event">Before the first event</span><span id="playback-status">Paused</span></div>
    </div>
    <div class="playback-tools dataset-actions">
      <button class="button ghost" id="run-dataset">Prepare dataset</button>
      <button class="button ghost" id="cancel-dataset" hidden>Stop preparation</button>
      <button class="button ghost" id="playback-retry" hidden>Retry opening</button>
      <button class="button ghost" id="sandbox-mode">Interactive sandbox</button>
      <button class="button ghost" id="revisit-dataset" hidden>Back to timeline</button>
    </div>
    <div id="replay-progress" class="replay-preparation" hidden></div>
    <div class="replay-quality" hidden></div>
    <details class="replay-downloads" hidden><summary>Saved results</summary><div class="replay-artifacts"></div></details>`;
  document.querySelector('.metrics').before(panel);
  const $ = selector => panel.querySelector(selector);
  const select = $('#dataset-select'), play = $('#playback-toggle'), restart = $('#playback-restart');
  const speed = $('#playback-speed'), range = $('#playback-range'), prepare = $('#run-dataset');
  const cancel = $('#cancel-dataset'), progress = $('#replay-progress'), revisit = $('#revisit-dataset');
  const dateFormat = new Intl.DateTimeFormat('en-GB', {timeZone: 'Europe/Vienna', day: '2-digit', month: 'short', year: 'numeric'});
  const timeFormat = new Intl.DateTimeFormat('en-GB', {timeZone: 'Europe/Vienna', hour: '2-digit', minute: '2-digit', second: '2-digit'});
  const archives = new Map();
  let jobId = null, latest = null, active = false, viewing = false, epoch = 0, pollTimer = null;
  let manifest = null, clock = null, loader = null, lastFrame = null, frameError = false;
  let manifestLoading = false, manifestError = false, opening = false, destroyed = false;
  let viewIntent = 0, preparing = false;
  const datasetName = value => [...select.options].find(option => option.value === value)?.textContent || value;
  const running = job => ['queued', 'running', 'finalizing'].includes(job?.status);
  const ready = job => ['completed', 'degraded'].includes(job?.status) && job.complete;

  function registerDataset(dataset) {
    if (!dataset || typeof dataset.id !== 'string' || !dataset.id || typeof dataset.name !== 'string') throw new TypeError('Invalid dataset');
    const selected = select.value;
    let option = [...select.options].find(item => item.value === dataset.id);
    if (!option) {option = document.createElement('option'); option.value = dataset.id; select.append(option);}
    option.textContent = `${dataset.name}${dataset.available === false ? ' · unavailable' : ''}`;
    option.disabled = dataset.available === false;
    if (/^replay-[0-9a-f]{32}$/.test(dataset.replay_id || '')) archives.set(dataset.id, dataset.replay_id);
    // Catalog hydration and late uploads register choices without taking over
    // the view the user selected while the network request was outstanding.
    select.value = selected;
  }

  function remember() {
    try {
      localStorage.setItem('hotel-replay-id', jobId || '');
      if (jobId && latest?.job?.dataset_id) localStorage.setItem(`hotel-replay-${latest.job.dataset_id}`, jobId);
    } catch {}
    try {
      const url = new URL(location.href);
      if (jobId) url.searchParams.set('replay', jobId);
      else url.searchParams.delete('replay');
      if (url.href !== location.href) history.replaceState(history.state, '', url.href);
    } catch {}
  }
  function stopPlayback() {
    clock?.destroy(); loader?.destroy(); clock = null; loader = null;
    manifest = null; lastFrame = null; frameError = false; manifestLoading = false; manifestError = false;
  }
  function zeroProgress() {
    return {events_processed: 0, total_events: manifest?.total_events || latest?.job?.progress?.total_events || 0,
      guest_processed: 0, total_guests: manifest?.total_guests || latest?.job?.progress?.total_guests || 0,
      decisions: 0, audit_rows: 0, autonomous_count: 0, pending_approval_count: 0,
      llm_count: 0, fallback_count: 0, source_label_count: 0, parser_errors: 0, source_errors: 0,
      needs_review_count: 0, safety_rejections: 0, as_of_utc: null};
  }
  function playbackState(tick = clock?.state()) {
    const caughtUp = lastFrame?.events_consumed === manifest?.total_events;
    return {...tick, status: frameError ? 'unavailable' : tick?.playing ? 'playing' : tick?.ended ? caughtUp ? 'finished' : 'loading' : 'paused',
      historical: true, events_consumed: lastFrame?.events_consumed || 0,
      total_events: manifest?.total_events || 0, journal_total: lastFrame?.journal_total || 0,
      source_ordinal: lastFrame?.source_ordinal || 0, current_event: lastFrame?.current_event || null,
      next_event: lastFrame?.next_event || null, frame_as_of_utc: lastFrame?.as_of_utc || null};
  }
  function emitView(frame = lastFrame) {
    if (!viewing || !latest) return;
    const playback = playbackState();
    const causalProgress = frame?.progress || zeroProgress();
    onJournal(jobId, frame?.journal_total || 0, playback);
    onView({job: {...latest.job, progress: causalProgress}, snapshot: frame?.snapshot || {}, playback});
  }
  function updateClock(tick) {
    const playback = playbackState(tick), date = new Date(tick.at_utc);
    $('#playback-clock').textContent = timeFormat.format(date);
    $('#playback-clock').dateTime = tick.at_utc;
    $('#playback-date').textContent = dateFormat.format(date);
    $('#playback-phase').textContent = tick.phase === 'reservations' ? 'Early reservations' : 'Hotel operations';
    range.value = String(Math.round(tick.fraction * 10000));
    play.textContent = tick.playing ? 'Ⅱ Pause' : tick.ended ? playback.status === 'finished' ? '↻ Play again' : 'Finishing…' : '▶ Play';
    play.setAttribute('aria-pressed', String(tick.playing));
    $('#playback-status').textContent = frameError ? 'Could not load this moment' : tick.playing ? 'Playing' : tick.ended ? playback.status === 'finished' ? 'Complete' : 'Finishing…' : 'Paused';
    if (viewing) onClock(playback);
  }
  function requestFrame(tick, {invalidate = false, immediate = false} = {}) {
    if (!loader || frameError) return;
    const query = tick.before_first ? 'cursor=0' : `at=${encodeURIComponent(tick.at_utc)}`;
    loader.request({key: query}, {invalidate, immediate});
  }
  function handleTick(tick) {
    const reset = ['seek', 'restart'].includes(tick.reason) || (tick.before_first && lastFrame?.events_consumed > 0);
    if (reset) {
      // Clear future guests, approvals and occupancy while a past seek loads.
      lastFrame = null;
      emitView(null);
      $('#playback-event-count').textContent = `0 / ${manifest.total_events.toLocaleString()} events`;
      $('#playback-current-event').textContent = tick.before_first ? 'Before the first event' : 'Opening this moment…';
    }
    updateClock(tick);
    requestFrame(tick, {invalidate: reset, immediate: tick.reason !== 'tick'});
    if (tick.ended) refreshButtons();
  }
  function refreshButtons() {
    const loading = opening || manifestLoading;
    const finishing = clock?.state().ended && lastFrame?.events_consumed !== manifest?.total_events;
    play.disabled = !clock || manifestLoading || opening || frameError || finishing;
    restart.disabled = !clock;
    range.disabled = !clock;
    select.disabled = active || opening;
    prepare.disabled = active || manifestLoading || opening;
    prepare.textContent = clock ? 'Prepare again' : 'Prepare dataset';
    cancel.hidden = !active;
    $('#playback-retry').hidden = !frameError && !manifestError;
    revisit.hidden = viewing || !latest;
    play.setAttribute('aria-busy', String(loading));
    if (loading) play.textContent = '◌ Loading…';
    else if (!clock) play.textContent = '▶ Play';
  }
  function showArchiveLinks(job) {
    const entries = Object.entries(job.artifacts || {}).filter(([, url]) => typeof url === 'string' && url.startsWith(`/api/replays/${job.id}/artifacts/`));
    $('.replay-downloads').hidden = !entries.length;
    $('.replay-artifacts').innerHTML = entries.map(([kind, url]) => `<a href="${escape(url)}" download>${escape(kind)} ↓</a>`).join('');
  }
  async function loadPlayback(own) {
    if (clock || manifestLoading || !ready(latest?.job)) return;
    manifestLoading = true; manifestError = false;
    refreshButtons();
    $('#source-caption').textContent = 'Opening saved timeline…';
    progress.hidden = false;
    progress.textContent = 'Opening saved results…';
    try {
      const result = await api(`/api/replays/${encodeURIComponent(jobId)}/playback`);
      if (destroyed || own !== epoch) return;
      if (!Number.isFinite(Date.parse(result.start_utc)) || !Number.isFinite(Date.parse(result.end_utc)) || !Number.isInteger(result.total_events) || result.total_events < 1) throw new Error('This archive has no playable timeline.');
      manifest = result;
      const selectedSpeed = speed.value === 'tour' ? 86400 : Number(speed.value);
      loader = createFrameLoader({
        load: item => api(`/api/replays/${encodeURIComponent(jobId)}/frame?${item.key}`),
        onFrame(frame) {
          if (own !== epoch || destroyed) return;
          lastFrame = frame; frameError = false;
          $('#playback-event-count').textContent = `${frame.events_consumed.toLocaleString()} / ${manifest.total_events.toLocaleString()} events`;
          const event = frame.current_event;
          const labels = {booking_created: 'Reservation created', roster_update: 'Team update',
            clock_tick: 'Hotel observations', guest_request: 'Guest message',
            operational_event: 'Hotel update', bms_alarm: 'Equipment alert',
            work_order_created: 'Maintenance request', unknown: 'Hotel record'};
          $('#playback-current-event').textContent = event
            ? `${labels[event.event_type] || 'Hotel record'}${event.room_id && event.room_id !== 'BUILDING' ? ` · ${event.room_id}` : ''}`
            : 'Before the first event';
          emitView(frame);
          updateClock(clock.state());
          refreshButtons();
        },
        onError(error) {
          if (own !== epoch || destroyed) return;
          frameError = true;
          clock?.pause();
          refreshButtons();
          toast(error.message, true);
        },
      });
      clock = createPlaybackClock({start: result.start_utc, end: result.end_utc, speed: selectedSpeed,
        operationalStart: Number.isFinite(Date.parse(result.operational_start_utc)) ? result.operational_start_utc : null,
        tour: speed.value === 'tour', onTick: handleTick});
      $('#source-caption').textContent = `${datasetName(latest.job.dataset_id)} · ${dateFormat.format(new Date(result.start_utc))} — ${dateFormat.format(new Date(result.end_utc))}`;
      progress.hidden = true;
      $('#playback-event-count').textContent = `0 / ${result.total_events.toLocaleString()} events`;
      updateClock(clock.state());
      emitView(null);
      requestFrame(clock.state(), {immediate: true});
    } catch (error) {
      if (own !== epoch || destroyed) return;
      manifestError = true;
      progress.hidden = false;
      progress.textContent = error.message;
      toast(error.message, true);
    } finally {
      if (own === epoch) {manifestLoading = false; refreshButtons();}
    }
  }
  function showPreparation(result) {
    latest = result; opening = false; active = running(result.job);
    if (![...select.options].some(option => option.value === result.job.dataset_id)) {
      registerDataset({id: result.job.dataset_id, name: 'Imported dataset', available: true});
    }
    select.value = result.job.dataset_id;
    const quality = $('.replay-quality'), fallbacks = result.job.progress.fallback_count || 0;
    quality.hidden = !ready(result.job) || (result.job.status !== 'degraded' && !fallbacks);
    quality.textContent = fallbacks ? `Prepared with ${fallbacks} fallback${fallbacks === 1 ? '' : 's'}.` : 'Prepared with warnings.';
    archives.set(result.job.dataset_id, result.job.id);
    remember(); showArchiveLinks(result.job); refreshButtons();
    if (active) {
      const p = result.job.progress;
      progress.hidden = false;
      progress.textContent = `Preparing ${p.events_processed.toLocaleString()} / ${p.total_events.toLocaleString()} events · ${p.guest_processed.toLocaleString()} / ${p.total_guests.toLocaleString()} guest messages`;
      $('#source-caption').textContent = 'Preparing the saved timeline';
      $('#playback-status').textContent = 'Preparing';
      emitView(null);
    } else if (!ready(result.job)) {
      progress.hidden = false;
      progress.textContent = result.job.error?.message || (result.job.status === 'cancelled' ? 'Preparation stopped. Saved results are available below.' : 'This run did not finish. Prepare the dataset to try again.');
      $('#playback-status').textContent = result.job.status === 'cancelled' ? 'Stopped' : 'Unavailable';
      emitView(null);
    } else loadPlayback(epoch);
  }
  async function poll() {
    const own = epoch, id = jobId;
    if (!id || destroyed) return;
    clearTimeout(pollTimer);
    try {
      const result = await api(`/api/replays/${encodeURIComponent(id)}?after=0&limit=1`);
      if (own !== epoch || destroyed) return;
      showPreparation(result);
      if (running(result.job)) pollTimer = setTimeout(poll, 1800);
    } catch (error) {
      if (own !== epoch || destroyed) return;
      if (error.status === 404) {
        jobId = null; active = false; opening = false; viewing = false; latest = null; remember(); stopPlayback();
        refreshButtons(); onDemo();
      } else pollTimer = setTimeout(poll, 4000);
      toast(error.message, true);
    }
  }
  function openArchive(id) {
    epoch++; clearTimeout(pollTimer); stopPlayback();
    jobId = id; latest = null; viewing = true; active = false; opening = true;
    play.textContent = '▶ Play'; range.value = '0';
    $('#playback-event-count').textContent = '0 events';
    $('#playback-current-event').textContent = 'Before the first event';
    $('#playback-clock').textContent = '—:—:—';
    $('#playback-date').textContent = 'Opening timeline';
    progress.hidden = false;
    progress.textContent = 'Opening saved results…';
    $('.replay-quality').hidden = true;
    onJournal(id, 0, {journal_total: 0, before_first: true});
    onView({job: {id, dataset_id: select.value, progress: zeroProgress()}, snapshot: {},
      playback: {status: 'loading', playing: false, historical: true, before_first: true, events_consumed: 0, journal_total: 0}});
    refreshButtons(); remember(); poll();
  }
  async function prepareDataset() {
    viewIntent++;
    const own = epoch, requestedView = viewIntent;
    opening = true; preparing = true; clock?.pause(); refreshButtons();
    try {
      const {job} = await api('/api/replays', {dataset_id: select.value});
      if (own !== epoch || destroyed) return;
      epoch++; clearTimeout(pollTimer); stopPlayback();
      jobId = job.id; viewing = requestedView === viewIntent ? true : viewing; active = true;
      showPreparation({job, snapshot: {}, total: 0});
      await poll();
    } catch (error) {if (own === epoch && !destroyed) {opening = false; toast(error.message, true); refreshButtons();}}
    finally {preparing = false;}
  }
  play.onclick = () => {
    if (!clock) return;
    viewIntent++;
    viewing = true;
    if (clock.state().playing) clock.pause(); else clock.play();
    refreshButtons();
  };
  restart.onclick = () => {viewIntent++; frameError = false; clock?.restart(); refreshButtons();};
  speed.onchange = () => {
    if (!clock || !manifest) return;
    if (speed.value === 'tour') clock.setTour(); else clock.setSpeed(Number(speed.value));
  };
  range.oninput = () => {
    if (!clock || !manifest) return;
    viewIntent++;
    frameError = false;
    const fraction = Number(range.value) / 10000;
    const first = Date.parse(manifest.start_utc), last = Date.parse(manifest.end_utc);
    clock.seek(first + fraction * (last - first), {before: fraction === 0});
    refreshButtons();
  };
  prepare.onclick = prepareDataset;
  $('#playback-retry').onclick = () => {
    if (clock) {frameError = false; requestFrame(clock.state(), {invalidate: true, immediate: true}); refreshButtons();}
    else loadPlayback(epoch);
  };
  cancel.onclick = async () => {
    cancel.disabled = true;
    try {await api(`/api/replays/${encodeURIComponent(jobId)}/cancel`, {}); toast('Stopping after the current event.');}
    catch (error) {toast(error.message, true);}
    finally {cancel.disabled = false;}
  };
  $('#sandbox-mode').onclick = () => {
    viewIntent++; clock?.pause(); viewing = false; refreshButtons(); onDemo();
  };
  revisit.onclick = () => {viewIntent++; viewing = true; refreshButtons(); emitView(); if (clock) updateClock(clock.state());};
  select.onchange = () => {
    viewIntent++;
    const dataset = select.value;
    let saved = archives.get(dataset);
    try {saved ||= localStorage.getItem(`hotel-replay-${dataset}`);} catch {}
    if (saved && /^replay-[0-9a-f]{32}$/.test(saved)) {openArchive(saved); return;}
    epoch++; clearTimeout(pollTimer); stopPlayback(); latest = null; jobId = null; active = false; opening = false;
    viewing = true;
    const initialPlayback = {status: 'unprepared', playing: false, historical: true, before_first: true,
      events_consumed: 0, total_events: 0, journal_total: 0};
    latest = {job: {id: null, dataset_id: dataset, status: 'unprepared', progress: zeroProgress()}, snapshot: {}};
    remember(); refreshButtons();
    onJournal(null, 0, initialPlayback);
    onView({...latest, playback: initialPlayback});
    $('#source-caption').textContent = `${datasetName(dataset)} · prepare to begin`;
    $('#playback-clock').textContent = '—:—:—'; $('#playback-date').textContent = 'Ready when you are';
    $('#playback-event-count').textContent = '0 events'; range.value = '0';
    $('#playback-status').textContent = 'Preparation required';
    $('#playback-phase').textContent = 'Not prepared';
    $('#playback-current-event').textContent = 'Before the first event';
    progress.hidden = false;
    progress.textContent = 'Prepare this dataset once to enable Play.';
    $('.replay-downloads').hidden = true;
    $('.replay-quality').hidden = true;
  };

  const initialViewIntent = viewIntent;
  api('/api/datasets').then(({datasets}) => {
    if (destroyed) return;
    for (const item of datasets) registerDataset(item);
    if (viewIntent === initialViewIntent && !jobId && !opening && archives.has(select.value)) openArchive(archives.get(select.value));
  }).catch(error => {if (!destroyed) toast(error.message, true);});
  const linked = new URLSearchParams(location.search).get('replay');
  let initial = linked && /^replay-[0-9a-f]{32}$/.test(linked) ? linked : null;
  if (!initial) {try {initial = localStorage.getItem('hotel-replay-id');} catch {}}
  if (initial && /^replay-[0-9a-f]{32}$/.test(initial)) openArchive(initial);
  return {isViewing: () => viewing, refresh: poll, getPlayback: () => clock ? playbackState() : null,
    viewIntent: () => viewIntent,
    importHost: $('.playback-tools'),
    addDataset(dataset, {viewIntent: requestedView = viewIntent} = {}) {
      if (destroyed) return false;
      registerDataset(dataset);
      if (requestedView !== viewIntent || preparing || active || dataset.available === false) return false;
      select.value = dataset.id;
      select.onchange();
      return true;
    },
    destroy() {destroyed = true; epoch++; clearTimeout(pollTimer); stopPlayback(); panel.remove();}};
}

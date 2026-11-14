/** A virtual UTC clock. Advancing it never runs inference or changes an archive. */
export const PLAYBACK_SPEEDS = [
  {value: 60, label: '1 min / sec'},
  {value: 900, label: '15 min / sec'},
  {value: 3600, label: '1 hour / sec'},
  {value: 21600, label: '6 hours / sec'},
  {value: 86400, label: '1 day / sec'},
  {value: 604800, label: '7 days / sec'},
];

function timestamp(value) {
  const result = typeof value === 'number' ? value : Date.parse(value);
  if (!Number.isFinite(result)) throw new TypeError('Invalid playback time');
  return result;
}

export function createPlaybackClock({start, end, speed = 86400, operationalStart = null, tour = false, onTick = () => {},
  now = () => performance.now(), requestFrame = callback => requestAnimationFrame(callback),
  cancelFrame = handle => cancelAnimationFrame(handle)}) {
  const first = timestamp(start), last = timestamp(end);
  const operational = operationalStart === null ? null : timestamp(operationalStart);
  if (last < first) throw new RangeError('Playback ends before it starts');
  if (!Number.isFinite(speed) || speed <= 0) throw new RangeError('Invalid playback speed');
  let current = first, playing = false, beforeFirst = true, destroyed = false;
  let multiplier = speed, anchor = null, handle = null;
  let mode = tour ? 'tour' : 'linear', tourPlan = makeTour(typeof tour === 'object' ? tour : {});

  function makeTour({duration = 180, prelude = 5} = {}) {
    if (!Number.isFinite(duration) || duration <= 0 || !Number.isFinite(prelude) || prelude <= 0) throw new RangeError('Invalid tour duration');
    const split = operational !== null && first < operational && operational < last ? operational : null;
    return {durationMs: duration * 1000, preludeMs: Math.min(prelude, duration / 2) * 1000, split};
  }
  function tourPosition(time) {
    const {split, durationMs, preludeMs} = tourPlan;
    if (last === first) return 0;
    if (split === null) return (time - first) / (last - first) * durationMs;
    if (time <= split) return (time - first) / (split - first) * preludeMs;
    return preludeMs + (time - split) / (last - split) * (durationMs - preludeMs);
  }
  function tourTime(position) {
    const {split, durationMs, preludeMs} = tourPlan;
    if (position >= durationMs) return last;
    if (split === null) return first + position / durationMs * (last - first);
    if (position <= preludeMs) return first + position / preludeMs * (split - first);
    return split + (position - preludeMs) / (durationMs - preludeMs) * (last - split);
  }
  function currentSpeed() {
    if (mode === 'linear') return multiplier;
    const {split, durationMs, preludeMs} = tourPlan;
    if (split === null) return (last - first) / durationMs;
    return current < split ? (split - first) / preludeMs : (last - split) / (durationMs - preludeMs);
  }

  function state() {
    return {at_utc: new Date(current).toISOString(), start_utc: new Date(first).toISOString(),
      end_utc: new Date(last).toISOString(), time_ms: current, speed: currentSpeed(), playing, mode,
      phase: operational !== null && current < operational ? 'reservations' : 'operations',
      duration_seconds: mode === 'tour' ? tourPlan.durationMs / 1000 : (last - first) / multiplier / 1000,
      remaining_seconds: mode === 'tour' ? (tourPlan.durationMs - tourPosition(current)) / 1000 : (last - current) / multiplier / 1000,
      before_first: beforeFirst, ended: !beforeFirst && current >= last,
      fraction: last === first ? Number(!beforeFirst) : (current - first) / (last - first)};
  }
  function emit(reason) { onTick({...state(), reason}); }
  function advance(at) {
    if (!playing) return;
    // A hidden tab may resume much later. Account for real elapsed time once,
    // without creating one callback or network request per skipped event.
    const elapsed = Math.max(0, at - anchor);
    current = Math.min(last, mode === 'tour' ? tourTime(tourPosition(current) + elapsed) : current + elapsed * multiplier);
    anchor = at;
    beforeFirst = false;
    if (current >= last) playing = false;
  }
  function schedule() {
    if (playing && handle === null && !destroyed) handle = requestFrame(tick);
  }
  function tick(at) {
    handle = null;
    if (destroyed || !playing) return;
    advance(at);
    emit('tick');
    schedule();
  }
  function stopFrame() {
    if (handle !== null) cancelFrame(handle);
    handle = null;
  }
  return {
    state,
    play() {
      if (destroyed || playing) return state();
      if (!beforeFirst && current >= last) {current = first; beforeFirst = true;}
      playing = true;
      anchor = now();
      emit('play');
      schedule();
      return state();
    },
    pause() {
      if (destroyed) return state();
      if (playing) advance(now());
      playing = false;
      stopFrame();
      emit('pause');
      return state();
    },
    seek(value, {before = false} = {}) {
      if (destroyed) return state();
      current = Math.min(last, Math.max(first, timestamp(value)));
      beforeFirst = before && current === first;
      anchor = now();
      if (current >= last && !beforeFirst) {playing = false; stopFrame();}
      emit('seek');
      schedule();
      return state();
    },
    restart() {
      if (destroyed) return state();
      playing = false;
      stopFrame();
      current = first;
      beforeFirst = true;
      anchor = null;
      emit('restart');
      return state();
    },
    setSpeed(value) {
      if (!Number.isFinite(value) || value <= 0) throw new RangeError('Invalid playback speed');
      if (destroyed) return state();
      if (playing) advance(now());
      mode = 'linear';
      multiplier = value;
      emit('speed');
      schedule();
      return state();
    },
    setTour(options = {}) {
      const next = makeTour(options);
      if (destroyed) return state();
      if (playing) advance(now());
      tourPlan = next;
      mode = 'tour';
      emit('speed');
      schedule();
      return state();
    },
    destroy() {destroyed = true; playing = false; stopFrame();},
  };
}

/** Coalesce animation ticks into bounded, serial, causally ordered frame reads. */
export function createFrameLoader({load, onFrame, onError = () => {}, interval = 200,
  now = () => performance.now(), setTimer = setTimeout, clearTimer = clearTimeout}) {
  let generation = 0, pending = null, inFlight = false, timer = null, destroyed = false;
  let lastStarted = -Infinity, lastDelivered = null;
  function schedule(immediate = false) {
    if (destroyed || inFlight || !pending) return;
    if (timer !== null) {clearTimer(timer); timer = null;}
    const delay = immediate ? 0 : Math.max(0, interval - (now() - lastStarted));
    if (delay) timer = setTimer(() => {timer = null; run();}, delay);
    else run();
  }
  async function run() {
    if (destroyed || inFlight || !pending) return;
    const item = pending, own = generation;
    pending = null;
    inFlight = true;
    lastStarted = now();
    try {
      const result = await load(item);
      if (!destroyed && own === generation) {
        lastDelivered = item.key;
        onFrame(result, item);
      }
    } catch (error) {
      if (!destroyed && own === generation) onError(error);
    } finally {
      inFlight = false;
      if (pending?.key === lastDelivered) pending = null;
      schedule();
    }
  }
  return {
    request(item, {invalidate = false, immediate = false} = {}) {
      if (destroyed) return;
      if (invalidate) {generation++; lastDelivered = null;}
      if (!invalidate && item.key === lastDelivered) return;
      pending = item;
      schedule(immediate);
    },
    destroy() {
      destroyed = true; generation++; pending = null;
      if (timer !== null) clearTimer(timer);
      timer = null;
    },
  };
}

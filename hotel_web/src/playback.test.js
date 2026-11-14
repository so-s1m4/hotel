import test from 'node:test';
import assert from 'node:assert/strict';
import {createPlaybackClock, createFrameLoader} from './playback.js';

function harness(options = {}) {
  let time = 0, next = 0;
  const callbacks = new Map(), ticks = [];
  const clock = createPlaybackClock({start: 0, end: 100000, speed: 10, ...options,
    now: () => time, onTick: state => ticks.push(state),
    requestFrame: callback => {callbacks.set(++next, callback); return next;},
    cancelFrame: id => callbacks.delete(id)});
  return {clock, ticks, callbacks,
    elapse(ms, frame = true) {time += ms; if (frame) {
      const waiting = [...callbacks.values()]; callbacks.clear(); waiting.forEach(fn => fn(time));
    }},
  };
}

test('starts paused before the first event and advances by real elapsed time', () => {
  const h = harness();
  assert.equal(h.clock.state().before_first, true);
  assert.equal(h.clock.state().playing, false);
  h.clock.play(); h.elapse(50); h.elapse(150);
  assert.equal(h.clock.state().time_ms, 2000);
  assert.equal(h.clock.state().before_first, false);
  assert.equal(h.callbacks.size, 1);
});

test('pause freezes time and resume excludes time spent paused', () => {
  const h = harness();
  h.clock.play(); h.elapse(100); h.clock.pause(); h.elapse(10000);
  assert.equal(h.clock.state().time_ms, 1000);
  assert.equal(h.callbacks.size, 0);
  h.clock.play(); h.elapse(50);
  assert.equal(h.clock.state().time_ms, 1500);
});

test('speed changes account for elapsed time at the old speed', () => {
  const h = harness();
  h.clock.play(); h.elapse(100, false); h.clock.setSpeed(20); h.elapse(100);
  assert.equal(h.clock.state().time_ms, 3000);
});

test('seek backward removes future clock state and restart returns to zero consumed', () => {
  const h = harness();
  h.clock.seek(80000); h.clock.seek(20000);
  assert.equal(h.clock.state().time_ms, 20000);
  h.clock.restart();
  assert.equal(h.clock.state().time_ms, 0);
  assert.equal(h.clock.state().before_first, true);
  assert.equal(h.clock.state().playing, false);
});

test('long frame jumps clamp exactly at the last event and stop scheduling', () => {
  const h = harness();
  h.clock.play(); h.elapse(1000000);
  assert.equal(h.clock.state().time_ms, 100000);
  assert.equal(h.clock.state().ended, true);
  assert.equal(h.clock.state().playing, false);
  assert.equal(h.callbacks.size, 0);
});

test('equal first and last timestamp is consumed once and remains restartable', () => {
  const h = harness({start: 42, end: 42});
  assert.equal(h.clock.state().fraction, 0);
  h.clock.play(); h.elapse(1);
  assert.equal(h.clock.state().fraction, 1);
  assert.equal(h.clock.state().ended, true);
  h.clock.restart();
  assert.equal(h.clock.state().fraction, 0);
  assert.equal(h.clock.state().before_first, true);
});

test('play is idempotent and destroy cancels callbacks', () => {
  const h = harness();
  h.clock.play(); h.clock.play();
  assert.equal(h.callbacks.size, 1);
  h.clock.destroy(); h.elapse(100); h.clock.play();
  assert.equal(h.callbacks.size, 0);
  assert.equal(h.clock.state().time_ms, 0);
});

test('invalid times and speeds are rejected', () => {
  assert.throws(() => harness({start: 'bad'}), TypeError);
  assert.throws(() => harness({end: -1}), RangeError);
  assert.throws(() => harness({speed: 0}), RangeError);
  assert.throws(() => harness().clock.setSpeed(Infinity), RangeError);
  assert.throws(() => harness().clock.seek('bad'), TypeError);
});

test('frame loader coalesces rapid ticks without parallel requests', async () => {
  const calls = [], shown = [];
  const loader = createFrameLoader({interval: 0,
    load: item => new Promise(resolve => calls.push({item, resolve})),
    onFrame: result => shown.push(result)});
  loader.request({key: 'one'});
  loader.request({key: 'two'});
  loader.request({key: 'three'});
  assert.equal(calls.length, 1);
  calls[0].resolve('one'); await Promise.resolve(); await Promise.resolve();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].item.key, 'three');
  calls[1].resolve('three'); await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(shown, ['one', 'three']);
  loader.request({key: 'three'});
  assert.equal(calls.length, 2);
  loader.destroy();
});

test('seeking invalidates an in-flight future response', async () => {
  const calls = [], shown = [];
  const loader = createFrameLoader({interval: 0,
    load: item => new Promise(resolve => calls.push({item, resolve})),
    onFrame: result => shown.push(result)});
  loader.request({key: 'future'});
  loader.request({key: 'past'}, {invalidate: true});
  calls[0].resolve('future'); await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(shown, []);
  calls[1].resolve('past'); await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(shown, ['past']);
  loader.destroy();
});

test('destroyed frame loader cannot publish a late response or error', async () => {
  let resolve;
  const shown = [];
  const loader = createFrameLoader({load: () => new Promise(done => {resolve = done;}),
    onFrame: result => shown.push(result), onError: error => shown.push(error)});
  loader.request({key: 'old'}); loader.destroy(); resolve('old');
  await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(shown, []);
});

test('three-minute tour consumes the full reservation interval in five seconds, then all operations', () => {
  const h = harness({start: 0, operationalStart: 1_000_000, end: 3_000_000, tour: true});
  h.clock.play(); h.elapse(4999);
  assert.equal(h.clock.state().phase, 'reservations');
  assert.ok(h.clock.state().time_ms < 1_000_000);
  h.elapse(1);
  assert.equal(h.clock.state().time_ms, 1_000_000);
  assert.equal(h.clock.state().phase, 'operations');
  h.elapse(174999);
  assert.equal(h.clock.state().playing, true);
  h.elapse(1);
  assert.equal(h.clock.state().time_ms, 3_000_000);
  assert.equal(h.clock.state().playing, false);
  assert.equal(h.clock.state().duration_seconds, 180);
});

test('piecewise tour remains monotonic continuously through its boundary and terminal pause', () => {
  const h = harness({start: 0, operationalStart: 9_000_000, end: 40_000_000, tour: true});
  h.clock.play();
  let previous = 0;
  for (let frame = 0; frame < 2000; frame++) {
    h.elapse(100);
    assert.ok(h.clock.state().time_ms >= previous);
    assert.ok(h.callbacks.size <= 1);
    previous = h.clock.state().time_ms;
  }
  assert.equal(previous, 40_000_000);
  assert.equal(h.clock.state().playing, false);
  assert.equal(h.callbacks.size, 0);
});

test('tour pause, seek and switching to linear speed preserve the current instant', () => {
  const h = harness({start: 0, operationalStart: 1_000_000, end: 3_000_000, tour: true});
  h.clock.play(); h.elapse(3000); h.clock.pause();
  const paused = h.clock.state().time_ms;
  h.elapse(500000);
  assert.equal(h.clock.state().time_ms, paused);
  h.clock.play(); h.elapse(2000);
  assert.equal(h.clock.state().time_ms, 1_000_000);
  h.clock.pause(); h.clock.seek(500000);
  assert.equal(h.clock.state().remaining_seconds, 177.5);
  h.clock.play(); h.elapse(2500);
  assert.equal(h.clock.state().time_ms, 1_000_000);
  h.clock.setSpeed(100); h.elapse(1000);
  assert.equal(h.clock.state().mode, 'linear');
  assert.equal(h.clock.state().time_ms, 1_100_000);
  h.clock.setTour();
  assert.equal(h.clock.state().time_ms, 1_100_000);
  assert.equal(h.clock.state().mode, 'tour');
});

test('tour handles missing, boundary and zero-length operational periods without invalid rates', () => {
  for (const operationalStart of [null, 0, 100000, -1, 100001]) {
    const h = harness({start: 0, end: 100000, operationalStart, tour: true});
    h.clock.play(); h.elapse(90000);
    assert.equal(h.clock.state().time_ms, 50000);
    assert.ok(Number.isFinite(h.clock.state().speed));
    h.elapse(90000);
    assert.equal(h.clock.state().time_ms, 100000);
    assert.equal(h.clock.state().playing, false);
  }
  const zero = harness({start: 42, end: 42, operationalStart: 42, tour: true});
  zero.clock.play(); zero.elapse(1);
  assert.equal(zero.clock.state().time_ms, 42);
  assert.equal(zero.clock.state().playing, false);
  assert.ok(Number.isFinite(zero.clock.state().speed));
  assert.throws(() => harness({tour: {duration: 0}}), RangeError);
});

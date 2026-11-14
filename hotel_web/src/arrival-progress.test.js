import {test} from 'node:test';
import assert from 'node:assert/strict';
import {arrivalProgress} from './arrival-progress.js';
import {createPlaybackClock} from './playback.js';

test('arrival follows simulation speed changes and stays frozen on pause', () => {
  let now=0;
  const clock=createPlaybackClock({start:0,end:100000,speed:1,now:()=>now,requestFrame:()=>1,cancelFrame:()=>{}});
  clock.play(); now=1000; clock.pause();
  const paused=arrivalProgress(0,clock.state().time_ms,25);
  now=10000;
  assert.deepEqual(arrivalProgress(0,clock.state().time_ms,25), paused);
  clock.setSpeed(10); clock.play(); now+=600; clock.pause();
  assert.equal(clock.state().time_ms,7000);
  assert.equal(arrivalProgress(0,clock.state().time_ms,25).fraction,.54);
  clock.setSpeed(1000); clock.play(); now+=100; clock.pause();
  assert.equal(arrivalProgress(0,clock.state().time_ms,25).fraction,1);
});

test('arrival clamps backwards/invalid time and completes after a large jump', () => {
  assert.equal(arrivalProgress(1000,500,25).fraction,0);
  assert.equal(arrivalProgress(0,86400000,25).fraction,1);
  assert.equal(arrivalProgress(0,NaN,25).fraction,0);
});

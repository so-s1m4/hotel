import {test} from 'node:test';
import assert from 'node:assert/strict';
import {setStableContent} from './stable-content.js';

test('unchanged replay frames preserve nodes and changed frames preserve scroll', () => {
  let writes = 0, html = '';
  const element = {scrollTop: 42, get innerHTML(){return html;}, set innerHTML(value){writes++; html=value; this.scrollTop=0;}};
  assert.equal(setStableContent(element, '<p>one</p>'), true);
  assert.equal(element.scrollTop, 42);
  assert.equal(setStableContent(element, '<p>one</p>'), false);
  assert.equal(writes, 1);
  assert.equal(setStableContent(element, '<p>two</p>'), true);
  assert.equal(element.scrollTop, 42);
  assert.equal(writes, 2);
});

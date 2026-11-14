import {test} from 'node:test';
import assert from 'node:assert/strict';
import {requestRows, requestLabel, messageForProposal, hasQueuedAccessTask} from './request-links.js';

test('associates approvals by request, even for the same room', () => {
  const messages = [{id:'old',room_id:'R1',status:'approval_required'}, {id:'new',room_id:'R1',status:'handled_autonomously'}];
  const proposals = [{id:'p1',room_id:'R1',request_ids:['old']}];
  const rows = requestRows(messages, proposals);
  assert.equal(rows.find(r => r.m?.id === 'new').p, null);
  assert.equal(rows.find(r => r.m?.id === 'old').p.id, 'p1');
  assert.equal(messageForProposal(messages, proposals[0]).id, 'old');
});

test('access verification is only claimed when its own task was queued', () => {
  const message = {id:'req', status:'handled_autonomously', intents:[{intent:'access_issue'}]};
  const task = {request_ids:['req'], intent:'access_issue', decision_record:{action:'create_service_task'}, audit:{phase:'task_queued'}};
  assert.equal(hasQueuedAccessTask([], message), false);
  assert.equal(hasQueuedAccessTask([{...task, request_ids:['other']}], message), false);
  assert.equal(hasQueuedAccessTask([{...task, audit:{phase:'blocked'}}], message), false);
  assert.equal(hasQueuedAccessTask([task], message), true);
});

test('keeps multiple approvals and does not call uncertain requests handled', () => {
  const messages = [{id:'one',status:'needs_review'}];
  const proposals = [{id:'a',request_id:'one'}, {id:'b',request_ids:['one']}];
  assert.deepEqual(requestRows(messages, proposals).map(r => r.p.id), ['a','b']);
  assert.equal(requestLabel(messages[0]), 'Needs review');
  assert.equal(requestLabel({status:'no_action'}), 'No action');
});

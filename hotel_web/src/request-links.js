// Decisions are associated with request IDs, never merely with a shared room.
export function requestIds(proposal) {
  return Array.isArray(proposal?.request_ids) && proposal.request_ids.length
    ? proposal.request_ids : proposal?.request_id ? [proposal.request_id] : [];
}
export function proposalsForMessage(proposals, message) {
  if (!message?.id) return [];
  return proposals.filter(p => requestIds(p).includes(message.id));
}
export function messageForProposal(messages, proposal) {
  const ids = requestIds(proposal);
  return [...messages].reverse().find(m => ids.includes(m.id)) || null;
}
export function requestRows(messages, proposals) {
  const visible = [...messages].reverse().slice(0, 15);
  const rows = visible.flatMap(m => {
    const linked = proposalsForMessage(proposals, m);
    return linked.length ? linked.map(p => ({p, m})) : [{p: null, m}];
  });
  const attached = new Set(rows.filter(r => r.p).map(r => r.p.id));
  const orphan = [...proposals].reverse().filter(p => !attached.has(p.id)).map(p => ({p, m: messageForProposal(messages, p)}));
  return [...orphan, ...rows];
}
export function requestLabel(message) {
  return ({handled_autonomously:'Simulated action', no_action:'No action', clarification_needed:'Needs clarification', needs_review:'Needs review', degraded:'Needs review', classified:'Classified'})[message?.status] || 'Recorded';
}
export function hasQueuedAccessTask(decisions, message) {
  return !!message?.id && decisions.some(d => requestIds(d).includes(message.id)
    && d.intent === 'access_issue' && d.decision_record?.action === 'create_service_task'
    && d.audit?.phase === 'task_queued');
}

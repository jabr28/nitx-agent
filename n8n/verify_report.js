// Honest reporting: the reply the user gets is checked against the platform's real
// state, read AFTER the agent finished. The agent's words never overrule this block.
const prep = $('Prepare').first().json;
const agent = $('Campaign Agent').first().json;
const state = $('Read real state').first().json;
const campaignId = $('Open campaign').first().json.campaign_id;

const reply = typeof agent.output === 'string' ? agent.output : '';
const warnings = [];
if (agent.error || !reply) {
  warnings.push(`The agent stopped with an error: ${agent.error?.message ?? agent.error ?? 'no reply'}`);
}

const ok = state && state.campaign_id === campaignId;
if (!ok) warnings.push('Could not read the campaign state from the platform; nothing below is confirmed.');

const schedules = ok ? state.screen_schedules : [];
const emails = ok ? state.email_campaigns : [];
const approval = ok ? state.approval : null;
const live = [...schedules, ...emails].filter((x) => x.status === 'scheduled');

// Claims the agent must never make unless the platform agrees. Checked sentence by sentence,
// skipping negated sentences ("nothing is live", "لم يتم الإرسال") so honest replies don't trip it.
const claim = /\b(is|are|now|been)\s+(live|published|sent)\b|scheduled successfully|(^|[\s،.:*-])تم (النشر|الإرسال|الجدولة)/i;
const negation = /\b(not|nothing|no|never|until|once|after|waiting|pending|draft)\b|(^|\s)[وف]?(لم|لا|لن|ليس|غير)(?=\s|$)|بانتظار|بعد موافقة|مسود/i;
const liveClaims = reply
  .split(/(?<=[.!؟?\n])\s*/)
  .filter((sentence) => claim.test(sentence) && !negation.test(sentence));
if (liveClaims.length && live.length === 0) {
  warnings.push(`The agent reply says "${liveClaims[0].trim().slice(0, 120)}", but the platform shows nothing live: everything is still a draft or waiting for approval.`);
}
const mentionsApproval = /approve\//.test(reply);
if (mentionsApproval && !approval) {
  warnings.push('The agent reply contains an approval link, but no approval request exists on the platform.');
}
if (approval && !reply.includes(approval.approval_url)) {
  warnings.push(`Approval request exists but the agent did not include its link: ${approval.approval_url}`);
}

// Compact, tool-by-tool trace for the user, the eval runner and the transcript.
const steps = (agent.intermediateSteps ?? []).map((s, i) => {
  const obs = typeof s.observation === 'string' ? s.observation : JSON.stringify(s.observation);
  return {
    n: i + 1,
    tool: s.action?.tool,
    input: s.action?.toolInput,
    is_error: /"isError":\s*true|^Error|Error executing tool|_NOT_FOUND|_UNAVAILABLE|INVALID_|NOT confirmed/.test(obs ?? ''),
    observation: (obs ?? '').slice(0, 1500),
  };
});

const fmt = (s) => `${s.screen_ids.length} screen(s), ${s.start_date}→${s.end_date} ${s.days_of_week.join('/')} ${s.start_time}-${s.end_time} [${s.status}]`;
const verified = ok
  ? [
      `Campaign ${campaignId}: ${state.status}`,
      `Posters on screens: ${state.screen_contents.length}`,
      `Screen schedules: ${schedules.length ? schedules.map(fmt).join('; ') : 'none'}`,
      `Email campaigns: ${emails.length ? emails.map((e) => `${e.recipients} recipients at ${e.send_at} [${e.status}]`).join('; ') : 'none'}`,
      `Approval: ${approval ? `${approval.status} — ${approval.approval_url}` : 'not requested'}`,
    ].join('\n')
  : 'unavailable';

return [{ json: {
  campaign_id: campaignId,
  session_id: prep.session_id,
  reply,
  verified_by_platform: verified,
  warnings,
  approval_url: approval?.approval_url ?? null,
  state: ok ? state : null,
  steps,
}}];

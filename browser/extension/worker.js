importScripts('public-key.js', 'policy.js');
const P = StopscrollPolicy;
const TYPES = ['main_frame','sub_frame','stylesheet','script','image','font','object','xmlhttprequest','ping','csp_report','media','websocket','webtransport','webbundle','other'];
let policy = null, port = null, epoch = 0, lastSequence = -1n, lastPayload = '';
let known = {domains: P.defaults, exceptions: ['accounts.youtube.com']};
const fallbackDomains = new Set(P.defaults);
let serial = Promise.resolve(), lastRules = '', open = false, applying = false, requested = false;
const redirecting = new Set();
const publicKey = StopscrollPublicKey ? crypto.subtle.importKey('spki', decode(StopscrollPublicKey),
  {name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256'}, false, ['verify']).catch(() => null) : Promise.resolve(null);
function decode(text) {
  if (typeof text !== 'string' || text.length > 100000 || !/^[A-Za-z0-9+/]*={0,2}$/.test(text)) throw Error('invalid envelope');
  return Uint8Array.from(atob(text), c => c.charCodeAt(0));
}
async function verify(envelope) {
  try {
    if (!envelope || Object.keys(envelope).sort().join(',') !== 'payload,signature') return null;
    const bytes = decode(envelope.payload), signature = decode(envelope.signature), key = await publicKey;
    if (!key || bytes.length > 65536 || signature.length > 1024 ||
        !await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, signature, bytes)) return null;
    const data = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes));
    return P.valid(data) ? data : null;
  } catch { return null; }
}
function loseTrust() {
  ++epoch;
  policy = null;
  open = false;
  known = {domains: [...fallbackDomains], exceptions: ['accounts.youtube.com']};
  reconcile();
}
async function receive(envelope) {
  const generation = ++epoch;
  const data = await verify(envelope);
  if (generation !== epoch) return;
  if (!data) return loseTrust();
  const sequence = BigInt(data.sequence);
  if (sequence < lastSequence || (sequence === lastSequence && envelope.payload !== lastPayload)) return loseTrust();
  lastSequence = sequence;
  lastPayload = envelope.payload;
  policy = data;
  for (const domain of data.domains) fallbackDomains.add(domain);
  reconcile();
}
async function closeTab(id) {
  if (redirecting.has(id)) return;
  redirecting.add(id);
  try { await chrome.tabs.update(id, {url: chrome.runtime.getURL('blocked.html')}); }
  finally { redirecting.delete(id); }
}
function blocked(url) {
  try { const host = new URL(url).hostname; return P.matches(host, known.domains) && !P.matches(host, known.exceptions); }
  catch { return false; }
}
function snapshot() {
  return {allowed: P.allowed(policy), leaseUntil: policy?.lease_until * 1000 || 0,
    allowedUntil: policy?.allowed_until * 1000 || 0, blockedUrl: chrome.runtime.getURL('blocked.html')};
}
async function guardTabs() {
  const tabs = await chrome.tabs.query({url: P.defaults.map(d => `*://*.${d}/*`)});
  for (const tab of tabs) {
    if (!blocked(tab.url)) continue;
    if (!P.allowed(policy)) await closeTab(tab.id).catch(() => {});
    else await chrome.tabs.sendMessage(tab.id, {type: 'policy', ...snapshot()}).catch(() => {});
  }
}
function condition(domains) { return {requestDomains: domains, resourceTypes: TYPES}; }
function desiredState() {
  known = P.valid(policy) ? {domains: policy.domains, exceptions: policy.exceptions}
    : {domains: [...fallbackDomains], exceptions: ['accounts.youtube.com']};
  const nextOpen = P.allowed(policy);
  return {map: known, nextOpen, signature: JSON.stringify([known, nextOpen])};
}
async function revoke() {
  const rules = await chrome.declarativeNetRequest.getSessionRules();
  await chrome.declarativeNetRequest.updateSessionRules({removeRuleIds: rules.map(r => r.id)});
}
async function applyState(target) {
  // Mark actual rules unknown BEFORE the first mutation. An interrupted update
  // must never reuse the cache from a configuration that no longer exists.
  lastRules = '';
  await revoke();
  await chrome.declarativeNetRequest.updateEnabledRulesets({enableRulesetIds: ['closed']});
  const rules = [];
  if (target.map.domains.length) rules.push({id: 1, priority: 1, action: {type: 'block'}, condition: condition(target.map.domains)});
  if (target.map.exceptions.length) rules.push({id: 2, priority: 2, action: {type: 'allow'}, condition: condition(target.map.exceptions)});
  const current = await chrome.declarativeNetRequest.getDynamicRules();
  await chrome.declarativeNetRequest.updateDynamicRules({removeRuleIds: current.map(r => r.id), addRules: rules});
  if (target.signature !== desiredState().signature) return false;
  if (target.nextOpen && target.map.domains.length)
    await chrome.declarativeNetRequest.updateSessionRules({addRules: [{id: 1, priority: 3, action: {type: 'allow'}, condition: condition(target.map.domains)}]});
  if (target.signature !== desiredState().signature) { await revoke(); return false; }
  await chrome.declarativeNetRequest.updateEnabledRulesets({disableRulesetIds: ['closed']});
  if (target.signature !== desiredState().signature) { await revoke(); return false; }
  lastRules = target.signature;
  open = target.nextOpen;
  await guardTabs();
  return true;
}
function reconcile() {
  requested = true;
  if (applying) return serial;
  applying = true;
  serial = (async () => {
    while (requested) {
      requested = false;
      const target = desiredState();
      if (target.signature === lastRules) continue;
      // Epochs cancel signature verification, not revocation transactions.
      // New packets/timer ticks coalesce behind this operation; they cannot
      // starve removal of an already-installed temporary allow.
      const complete = await applyState(target);
      if (!complete || target.signature !== desiredState().signature) requested = true;
    }
  })().catch(async () => {
    ++epoch; policy = null; lastRules = ''; open = false; requested = false;
    await revoke().catch(() => {});
    await chrome.declarativeNetRequest.updateEnabledRulesets({enableRulesetIds: ['closed']}).catch(() => {});
    desiredState();
    await guardTabs().catch(() => {});
    setTimeout(reconcile, 1000);
  }).finally(() => {
    applying = false;
    if (requested) reconcile();
  });
  return serial;
}
function connect() {
  if (port) return;
  try {
    const connected = chrome.runtime.connectNative('org.stopscroll.youtube');
    port = connected;
    connected.onMessage.addListener(data => { if (port === connected) receive(data); });
    connected.onDisconnect.addListener(() => {
      void chrome.runtime.lastError;
      if (port !== connected) return;
      port = null; loseTrust(); setTimeout(connect, 1000);
    });
  } catch { loseTrust(); setTimeout(connect, 1000); }
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message?.type !== 'guard') return;
  if (!P.valid(policy)) known = {domains: [...fallbackDomains], exceptions: ['accounts.youtube.com']};
  if (sender.url && blocked(sender.url)) reply(snapshot());
  else reply({allowed: true, leaseUntil: Date.now() + 4000, allowedUntil: Date.now() + 4000});
});
chrome.tabs.onUpdated.addListener((id, change) => {
  if (change.url && blocked(change.url) && !P.allowed(policy)) closeTab(id).catch(() => {});
});
chrome.runtime.onStartup.addListener(() => { loseTrust(); connect(); });
chrome.runtime.onInstalled.addListener(() => { reconcile(); connect(); });
setInterval(() => {
  if (!P.valid(policy) && (policy || open)) loseTrust();
  else if (open && !P.allowed(policy)) reconcile();
}, 200);
// Restore additional persisted denies before touching dynamic rules at startup.
chrome.declarativeNetRequest.getDynamicRules().then(rules => {
  for (const rule of rules) if (rule.action.type === 'block')
    for (const domain of rule.condition.requestDomains || []) fallbackDomains.add(domain);
  reconcile(); connect();
}).catch(() => { reconcile(); connect(); });

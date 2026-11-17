importScripts('policy.js');
const P = StopscrollPolicy;
let policy = null;
let known = {domains: P.defaults, exceptions: ['accounts.youtube.com']};
let port = null;
let serial = Promise.resolve();
let lastRules = '';
let open = false;
const redirecting = new Set();
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
  // URL-filtered query touches YouTube tabs only; do not enumerate unrelated URLs.
  const patterns = P.defaults.map(d => `*://*.${d}/*`);
  const tabs = await chrome.tabs.query({url: patterns});
  for (const tab of tabs) {
    if (!blocked(tab.url)) continue;
    if (!P.allowed(policy)) await closeTab(tab.id).catch(() => {});
    else await chrome.tabs.sendMessage(tab.id, {type: 'policy', ...snapshot()}).catch(() => {});
  }
}
function reconcile() {
  serial = serial.catch(() => {}).then(async () => {
    const active = P.valid(policy);
    const nextOpen = P.allowed(policy);
    if (active) known = {domains: policy.domains, exceptions: policy.exceptions};
    const signature = JSON.stringify([known, nextOpen]);
    if (signature !== lastRules) {
      const rules = [];
      if (known.domains.length) rules.push({id: 1, priority: 1, action: {type: 'block'}, condition: {requestDomains: known.domains}});
      if (known.exceptions.length) rules.push({id: 2, priority: 2, action: {type: 'allow'}, condition: {requestDomains: known.exceptions}});
      const current = await chrome.declarativeNetRequest.getDynamicRules();
      await chrome.declarativeNetRequest.updateDynamicRules({removeRuleIds: current.map(r => r.id), addRules: rules});
      const session = await chrome.declarativeNetRequest.getSessionRules();
      const allows = nextOpen && known.domains.length ? [{id: 1, priority: 3, action: {type: 'allow'}, condition: {requestDomains: known.domains}}] : [];
      await chrome.declarativeNetRequest.updateSessionRules({removeRuleIds: session.map(r => r.id), addRules: allows});
      // Persisted dynamic denies now provide startup protection independently of
      // the worker. Session allows disappear when the browser session ends.
      await chrome.declarativeNetRequest.updateEnabledRulesets({disableRulesetIds: ['closed']});
      lastRules = signature;
      open = nextOpen;
      await guardTabs();
    }
  }).catch(async () => {
    // An API failure must never leave a previous temporary allow in place.
    policy = null;
    lastRules = '';
    const rules = await chrome.declarativeNetRequest.getSessionRules().catch(() => []);
    await chrome.declarativeNetRequest.updateSessionRules({removeRuleIds: rules.map(r => r.id)}).catch(() => {});
    await guardTabs().catch(() => {});
  });
  return serial;
}
function connect() {
  if (port) return;
  try {
    port = chrome.runtime.connectNative('org.stopscroll.youtube');
    port.onMessage.addListener(data => { policy = P.valid(data) ? data : null; reconcile(); });
    port.onDisconnect.addListener(() => { void chrome.runtime.lastError; port = null; policy = null; reconcile(); setTimeout(connect, 1000); });
  } catch { policy = null; reconcile(); setTimeout(connect, 1000); }
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message?.type !== 'guard') return;
  if (sender.url && blocked(sender.url)) reply(snapshot());
  else reply({allowed: true, leaseUntil: Date.now() + 4000, allowedUntil: Date.now() + 4000});
});
chrome.tabs.onUpdated.addListener((id, change, tab) => {
  if (change.url && blocked(change.url) && !P.allowed(policy))
    closeTab(id).catch(() => {});
});
chrome.runtime.onStartup.addListener(() => { policy = null; lastRules = ''; reconcile(); connect(); });
chrome.runtime.onInstalled.addListener(() => { reconcile(); connect(); });
setInterval(() => { if (open && !P.allowed(policy)) reconcile(); }, 200);
reconcile();
connect();

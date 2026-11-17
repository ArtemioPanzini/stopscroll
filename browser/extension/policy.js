/* Shared policy validation and hostname matching; no browsing data is retained. */
(() => {
  const defaults = ['youtube.com', 'youtu.be', 'googlevideo.com', 'youtube-nocookie.com', 'youtubei.googleapis.com', 'youtube.googleapis.com'];
  function matches(host, domains) { return domains.some(d => host === d || host.endsWith('.' + d)); }
  function valid(data, now = Date.now()) {
    if (!data || data.version !== 1 || !Array.isArray(data.domains) || !Array.isArray(data.exceptions)) return false;
    if (![data.issued_at, data.lease_until, data.allowed_until].every(Number.isFinite)) return false;
    if (data.issued_at * 1000 > now + 1000 || data.lease_until * 1000 <= now || data.lease_until - data.issued_at > 4) return false;
    return [data.domains, data.exceptions].every(a => a.length <= 100 && a.every(d => typeof d === 'string' && /^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(d)));
  }
  function allowed(data, now = Date.now()) { return valid(data, now) && data.allowed_until * 1000 > now; }
  const api = {defaults, matches, valid, allowed};
  globalThis.StopscrollPolicy = api;
  if (typeof module !== 'undefined') module.exports = api;
})();

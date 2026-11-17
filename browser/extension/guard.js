(() => {
  if (location.hostname === 'accounts.youtube.com' || location.hostname.endsWith('.accounts.youtube.com')) return;
  const cover = document.createElement('style');
  cover.textContent = 'html{visibility:hidden!important}';
  (document.documentElement || document).appendChild(cover);
  let leaseUntil = 0, allowedUntil = 0, closed = false;
  function close() {
    if (closed) return;
    closed = true;
    for (const media of document.querySelectorAll('video,audio')) { media.pause(); media.removeAttribute('src'); media.load(); }
    window.stop();
    location.replace(chrome.runtime.getURL('blocked.html'));
  }
  function receive(data) {
    if (!data?.allowed) return close();
    leaseUntil = data.leaseUntil;
    allowedUntil = data.allowedUntil;
    cover.remove();
  }
  function check() {
    chrome.runtime.sendMessage({type: 'guard'}).then(receive).catch(close);
  }
  chrome.runtime.onMessage.addListener(message => { if (message?.type === 'policy') receive(message); });
  check();
  setInterval(() => {
    if (leaseUntil && (Date.now() >= leaseUntil || Date.now() >= allowedUntil)) close();
    else check();
  }, 500);
})();

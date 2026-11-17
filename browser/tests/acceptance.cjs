const fs = require('node:fs'), https = require('node:https'), path = require('node:path'), assert = require('node:assert/strict');
// Supply PLAYWRIGHT_MODULE if Playwright is not installed in this repository.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const dir = process.env.STOPSCROLL_BROWSER_TEST_ROOT;
assert(dir?.startsWith('/tmp/stopscroll-browser-test-'));
const domains = ['youtube.com','youtu.be','googlevideo.com','youtube-nocookie.com','youtubei.googleapis.com','youtube.googleapis.com'];
let deadline = 0, heartbeat;
function publish() { const now = Date.now()/1000; fs.writeFileSync(path.join(dir,'policy/browser.json'), JSON.stringify({version:1,issued_at:now,lease_until:now+4,allowed_until:deadline,domains,exceptions:['accounts.youtube.com']})); }
const server = https.createServer({key:fs.readFileSync(path.join(dir,'key.pem')),cert:fs.readFileSync(path.join(dir,'cert.pem'))},(req,res)=>{
  res.setHeader('Access-Control-Allow-Origin','*');
  if(req.url==='/media.wav'){res.setHeader('Content-Type','audio/wav');res.end(fs.readFileSync(path.join(dir,'media.wav')));return;}
  if(req.url==='/sw.js'){res.setHeader('Content-Type','text/javascript');res.end(`self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(clients.claim()));self.addEventListener('fetch',e=>{if(new URL(e.request.url).pathname==='/cached')e.respondWith(new Response('<!doctype html><title>Cached recommendations fixture</title><p>Cached cards</p>',{headers:{'Content-Type':'text/html'}}));});`);return;}
  res.setHeader('Content-Type','text/html');res.end('<!doctype html><title>Fixture</title><p>fixture</p><video autoplay muted src="https://rr1.googlevideo.com:18897/media.wav"></video>');
});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{publish();heartbeat=setInterval(publish,200);await new Promise(r=>server.listen(18897,'127.0.0.1',r));const ctx=await chromium.launchPersistentContext(path.join(dir,'profile'),{executablePath:'/opt/google/chrome/chrome',headless:false,ignoreHTTPSErrors:true,ignoreDefaultArgs:['--disable-extensions','--disable-background-networking'],args:['--no-sandbox','--no-first-run','--ignore-certificate-errors','--host-resolver-rules=MAP *.youtube.com 127.0.0.1, MAP *.googlevideo.com 127.0.0.1, MAP x.com 127.0.0.1']});const results={};try{
  const extensions=await ctx.newPage();await extensions.goto('chrome://extensions/');const ours=JSON.parse(fs.readFileSync('/usr/local/lib/stopscroll/browser/package.json')).id;const legacy=fs.readFileSync(path.join(dir,'legacy-id'),'utf8');let installed=[];
  for(let i=0;i<40;i++){installed=await extensions.evaluate(()=>new Promise(resolve=>chrome.developerPrivate.getExtensionsInfo({includeDisabled:true},xs=>resolve(xs.map(x=>({id:x.id,state:x.state,userMayModify:x.userMayModify,mustRemainInstalled:x.mustRemainInstalled}))))));if([ours,legacy].every(id=>installed.some(x=>x.id===id&&x.state==='ENABLED'&&x.userMayModify===false&&x.mustRemainInstalled)))break;await sleep(500);}
  for(const id of [ours,legacy])assert(installed.some(x=>x.id===id&&x.state==='ENABLED'&&!x.userMayModify&&x.mustRemainInstalled),'both managed extensions must coexist');results.managedCoexistence=true;
  const account=await ctx.newPage();assert.equal((await account.goto('https://accounts.youtube.com:18897/')).status(),200);results.accountException=true;
  const other=await ctx.newPage();assert.equal((await other.goto('https://x.com:18897/')).status(),200);results.otherSiteUnaffected=true;
  let p=await ctx.newPage();await p.goto('https://www.youtube.com:18897/').catch(()=>{});await sleep(500);assert(!p.url().startsWith('https://www.youtube.com'),'closed navigation');results.closed=true;
  deadline=Date.now()/1000+60;publish();await sleep(1000);if(p.isClosed())p=await ctx.newPage();await p.goto('https://www.youtube.com:18897/',{waitUntil:'domcontentloaded'});await p.waitForFunction(()=>getComputedStyle(document.documentElement).visibility!=='hidden');await p.evaluate(async()=>{await navigator.serviceWorker.register('/sw.js');await navigator.serviceWorker.ready;});await p.reload();await p.waitForFunction(()=>document.querySelector('video')?.currentTime>0.1);results.openAndMedia=true;
  deadline=0;publish();await p.waitForURL(/chrome-extension:.*blocked.html/,{timeout:6000});results.closeStopsExistingTab=true;
  p=await ctx.newPage();await p.goto('https://www.youtube.com:18897/cached').catch(()=>{});await sleep(700);assert(!p.url().startsWith('https://www.youtube.com'),'cached shell must not remain visible');results.cachedShell=true;
  const freshFetch=await other.evaluate(async()=>{try{return {status:(await fetch('https://www.youtube.com:18897/data')).status};}catch(e){return {failed:true};}});assert(freshFetch.failed,'closed network API must be blocked');results.networkFetch=true;
  deadline=Date.now()/1000+5;publish();await sleep(1000);p=await ctx.newPage();await p.goto('https://www.youtube.com:18897/',{waitUntil:'domcontentloaded'});await p.waitForFunction(()=>document.querySelector('video')?.currentTime>0.1);await p.waitForURL(/chrome-extension:.*blocked.html/,{timeout:7000});results.expiry=true;
  deadline=Date.now()/1000+60;publish();await sleep(1000);p=await ctx.newPage();await p.goto('https://www.youtube.com:18897/',{waitUntil:'domcontentloaded'});await p.waitForFunction(()=>document.querySelector('video')?.currentTime>0.1);clearInterval(heartbeat);heartbeat=null;await p.waitForURL(/chrome-extension:.*blocked.html/,{timeout:7000});results.stalePolicy=true;
  console.log(JSON.stringify(results,null,2));
}catch(e){await sleep(1000);console.log('STATE ON ERROR',ctx.pages().map(p=>({url:p.url(),closed:p.isClosed()})));throw e;}finally{if(heartbeat)clearInterval(heartbeat);await ctx.close();server.close();}})().catch(e=>{console.error(e);if(heartbeat)clearInterval(heartbeat);server.close();process.exitCode=1;});

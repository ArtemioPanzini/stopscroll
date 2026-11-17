const fs=require('node:fs'),https=require('node:https'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const dir=process.env.STOPSCROLL_BROWSER_TEST_ROOT;assert(dir?.startsWith('/tmp/stopscroll-browser-test-'));
const counters=new Map();const control=value=>fs.writeFileSync(path.join(dir,'control.json'),JSON.stringify(value));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const envelope=()=>JSON.parse(fs.readFileSync(path.join(dir,'policy/browser.json')));
const server=https.createServer({key:fs.readFileSync(path.join(dir,'key.pem')),cert:fs.readFileSync(path.join(dir,'cert.pem'))},(req,res)=>{
 counters.set(req.url,(counters.get(req.url)||0)+1);res.setHeader('Access-Control-Allow-Origin','*');
 if(req.url==='/media.wav'){res.setHeader('Content-Type','audio/wav');res.end(fs.readFileSync(path.join(dir,'media.wav')));return;}
 if(req.url==='/sw.js'){res.setHeader('Content-Type','text/javascript');res.end(`self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(clients.claim()));self.addEventListener('fetch',e=>{if(new URL(e.request.url).pathname==='/cached')e.respondWith(new Response('<!doctype html><title>SW marker</title><p>STOPSCROLL_SW_CACHED_MARKER</p>',{headers:{'Content-Type':'text/html'}}));});`);return;}
 res.setHeader('Content-Type','text/html');res.end('<!doctype html><title>Fixture</title><p>fixture</p><video autoplay muted src="https://rr1.googlevideo.com:18897/media.wav"></video>');
});
(async()=>{await new Promise(r=>server.listen(18897,'127.0.0.1',r));const ctx=await chromium.launchPersistentContext(path.join(dir,'profile'),{executablePath:'/opt/google/chrome/chrome',headless:false,ignoreHTTPSErrors:true,ignoreDefaultArgs:['--disable-extensions','--disable-background-networking'],args:['--no-sandbox','--no-first-run','--ignore-certificate-errors','--host-resolver-rules=MAP *.youtube.com 127.0.0.1, MAP *.googlevideo.com 127.0.0.1, MAP x.com 127.0.0.1, MAP extra.example 127.0.0.1']});const results={};
 async function denied(route){const before=counters.get(route)||0;const p=await ctx.newPage();await p.goto('https://www.youtube.com:18897'+route).catch(()=>{});await sleep(300);assert.equal(counters.get(route)||0,before,'closed main frame must not reach origin');assert(!p.url().startsWith('https://www.youtube.com'),'closed document');return p;}
 async function playing(){const p=await ctx.newPage();await p.goto('https://www.youtube.com:18897/',{waitUntil:'domcontentloaded'});await p.waitForFunction(()=>document.querySelector('video')?.currentTime>0.1);return p;}
 try{
 const manager=await ctx.newPage();await manager.goto('chrome://extensions/');const ours=JSON.parse(fs.readFileSync('/usr/local/lib/stopscroll/browser/package.json')).id,legacy=fs.readFileSync(path.join(dir,'legacy-id'),'utf8');let installed=[];
 for(let i=0;i<40;i++){installed=await manager.evaluate(()=>new Promise(r=>chrome.developerPrivate.getExtensionsInfo({includeDisabled:true},xs=>r(xs.map(x=>({id:x.id,state:x.state,userMayModify:x.userMayModify,mustRemainInstalled:x.mustRemainInstalled}))))));if([ours,legacy].every(id=>installed.some(x=>x.id===id&&x.state==='ENABLED'&&!x.userMayModify&&x.mustRemainInstalled)))break;await sleep(500);}
 for(const id of [ours,legacy])assert(installed.some(x=>x.id===id&&x.state==='ENABLED'&&!x.userMayModify&&x.mustRemainInstalled));results.managedCoexistence=true;
 const account=await ctx.newPage();assert.equal((await account.goto('https://accounts.youtube.com:18897/')).status(),200);results.accountException=true;
 const other=await ctx.newPage();assert.equal((await other.goto('https://x.com:18897/')).status(),200);results.otherSiteUnaffected=true;
 await denied('/fresh-closed');results.closedMainFrameBeforeOrigin=true;
 control({mode:'signed',deadline:Date.now()/1000+60});await sleep(800);let p=await playing();assert(fs.existsSync(path.join(dir,'policy/fake-host-used')));results.userNativeHostShadowExercised=true;
 const position=await p.evaluate(()=>document.querySelector('video').currentTime);
 control({mode:'producer',token:Date.now()});await sleep(1200);
 const revisions=JSON.parse(fs.readFileSync(path.join(dir,'producer-evidence.json'))).map(x=>JSON.parse(Buffer.from(x.payload,'base64')));
 assert.equal(revisions[0].issued_at,revisions[1].issued_at);assert(BigInt(revisions[1].sequence)>BigInt(revisions[0].sequence));assert(revisions[1].allowed_until>revisions[0].allowed_until);
 assert(p.url().startsWith('https://www.youtube.com'));assert(await p.evaluate(previous=>document.querySelector('video').currentTime>previous,position));results.authorizedSameNowSessionExtension=true;
 control({mode:'signed',deadline:Date.now()/1000+60});await sleep(400);

 const open='/fresh-open';await p.goto('https://www.youtube.com:18897'+open);assert((counters.get(open)||0)>0);results.openMainFramePositive=true;
 await p.evaluate(async()=>{await navigator.serviceWorker.register('/sw.js');await navigator.serviceWorker.ready;});await p.reload();await p.goto('https://www.youtube.com:18897/cached');assert((await p.textContent('body')).includes('STOPSCROLL_SW_CACHED_MARKER'));assert.equal(counters.get('/cached')||0,0);results.swPositiveMarker=true;
 p=await playing();results.openAndMedia=true;
 control({mode:'signed',deadline:0});await p.waitForURL(/chrome-extension:.*blocked.html/,{timeout:6000});results.closeStopsExistingTab=true;await sleep(300);
 await denied('/fresh-denied');p=await ctx.newPage();await p.goto('https://www.youtube.com:18897/cached').catch(()=>{});await sleep(500);assert(!p.url().startsWith('https://www.youtube.com'));results.cachedShell=true;
 const fresh=await other.evaluate(async()=>{try{return {status:(await fetch('https://www.youtube.com:18897/data')).status};}catch{return {failed:true};}});assert(fresh.failed);results.networkFetch=true;
 const closed=envelope(),altered=JSON.parse(Buffer.from(closed.payload,'base64'));altered.allowed_until=Date.now()/1000+60;
 control({mode:'passthrough',message:altered});await sleep(600);await denied('/unsigned');results.unsignedGrantRejected=true;
 control({mode:'passthrough',message:{...closed,payload:Buffer.from(JSON.stringify(altered)).toString('base64')}});await sleep(600);await denied('/tampered');results.tamperedGrantRejected=true;
 control({mode:'signed',deadline:Date.now()/1000+60});await sleep(600);p=await playing();const oldGrant=envelope();
 control({mode:'signed',deadline:0});await p.waitForURL(/chrome-extension:.*blocked.html/,{timeout:6000});await sleep(250);
 assert(JSON.parse(Buffer.from(oldGrant.payload,'base64')).lease_until>Date.now()/1000+.5,'replayed grant must still be fresh');
 control({mode:'passthrough',message:oldGrant});await sleep(350);await denied('/reordered');results.reorderedFreshGrantRejected=true;
 control({mode:'signed',deadline:Date.now()/1000+5});await sleep(800);p=await playing();await p.waitForURL(/chrome-extension:.*blocked.html/,{timeout:7000});results.expiry=true;
 control({mode:'signed',deadline:Date.now()/1000+60,domains:['youtube.com','googlevideo.com','extra.example']});await sleep(800);p=await playing();control({mode:'freeze'});await p.waitForURL(/chrome-extension:.*blocked.html/,{timeout:7000});results.staleSignedLease=true;
 control({mode:'signed',deadline:Date.now()/1000+60,domains:[],exceptions:['youtube.com']});await sleep(800);p=await playing();control({mode:'freeze'});await p.waitForURL(/chrome-extension:.*blocked.html/,{timeout:7000});await denied('/removed-map-fallback');results.removedMapFallback=true;
 const extra='/extra-denied';const before=counters.get(extra)||0;const e=await ctx.newPage();await e.goto('https://extra.example:18897'+extra).catch(()=>{});assert.equal(counters.get(extra)||0,before);results.extraFallbackDeny=true;
 console.log(JSON.stringify(results,null,2));
 }catch(e){console.log('STATE ON ERROR',ctx.pages().map(p=>({url:p.url(),closed:p.isClosed()})));throw e;}finally{await ctx.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

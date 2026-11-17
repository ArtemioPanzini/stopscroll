const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), crypto = require('node:crypto');
const keys=crypto.generateKeyPairSync('rsa',{modulusLength:2048});
let publication=10n**18n;
const publicKey=keys.publicKey.export({type:'spki',format:'der'}).toString('base64');
function sign(data){const bytes=Buffer.from(JSON.stringify({...data,sequence:data.sequence||String(++publication)}));return {payload:bytes.toString('base64'),signature:crypto.sign('RSA-SHA256',bytes,keys.privateKey).toString('base64')};}
function fixture(){
  let now=1_000_000,dynamic=[],session=[],hold=false;const pending=[],dnrPending=[];let holdDynamic=false,holdSession=false;
  const intervals=[],updates=[],queries=[];
  const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn);},emit(...args){for(const fn of this.listeners)fn(...args);}});
  const port={onMessage:event(),onDisconnect:event()};
  const chrome={runtime:{getURL:f=>'chrome-extension://fixture/'+f,connectNative:()=>port,onMessage:event(),onStartup:event(),onInstalled:event()},
    declarativeNetRequest:{getDynamicRules:async()=>dynamic,updateDynamicRules:async c=>{dynamic=c.addRules||[];if(holdDynamic){holdDynamic=false;await new Promise(r=>dnrPending.push(r));}},getSessionRules:async()=>session,updateSessionRules:async c=>{if(holdSession){holdSession=false;await new Promise(r=>dnrPending.push(r));}session=c.addRules||[];},updateEnabledRulesets:async()=>{}},
    tabs:{query:async o=>{queries.push(o);return [{id:1,url:'https://www.youtube.com/'},{id:2,url:'https://accounts.youtube.com/'}];},update:async(id,o)=>updates.push({id,...o}),sendMessage:async()=>{},onUpdated:event()}};
  const subtle={importKey:(...a)=>crypto.webcrypto.subtle.importKey(...a),verify:async(...a)=>{const valid=await crypto.webcrypto.subtle.verify(...a);if(hold)await new Promise(r=>pending.push(r));return valid;}};
  const context=vm.createContext({chrome,crypto:{subtle},URL,Uint8Array,TextDecoder,atob,Date:{now:()=>now},setInterval:f=>intervals.push(f),setTimeout:()=>{}});
  context.importScripts=(...files)=>{for(const file of files){if(file==='public-key.js')context.StopscrollPublicKey=publicKey;else vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension',file),'utf8'),context);}};
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension/worker.js'),'utf8'),context);
  const settle=async()=>{await new Promise(setImmediate);await vm.runInContext('serial',context);};
  return {port,chrome,updates,queries,get dynamic(){return dynamic;},get session(){return session;},settle,
    policy:()=>({version:1,issued_at:now/1000,lease_until:now/1000+4,allowed_until:now/1000+60,domains:['youtube.com','googlevideo.com'],exceptions:['accounts.youtube.com']}),
    send:async envelope=>{context.envelope=envelope;await vm.runInContext('receive(envelope)',context);await settle();},
    start:envelope=>{context.envelope=envelope;return vm.runInContext('receive(envelope)',context);},
    holdDynamic:()=>{holdDynamic=true;},holdSession:()=>{holdSession=true;},dnrPending:()=>dnrPending.length,releaseDnr:()=>{for(const done of dnrPending.splice(0))done();},
    hold:()=>{hold=true;},pending:()=>pending.length,release:()=>{hold=false;for(const done of pending.splice(0))done();},
    guard:url=>{let answer;chrome.runtime.onMessage.emit({type:'guard'},{url},x=>{answer=x;});return answer;},
    advance:ms=>{now+=ms;for(const f of intervals)f();}};
}
test('signed session opens; native disconnect closes only YouTube and removes grants',async()=>{
  const f=fixture();await f.settle();assert.equal(f.session.length,0);await f.send(sign(f.policy()));assert.equal(f.session[0].action.type,'allow');
  assert(f.session[0].condition.resourceTypes.includes('main_frame'));for(const r of f.dynamic)assert(r.condition.resourceTypes.includes('main_frame'));
  f.updates.length=0;f.port.onDisconnect.emit();await f.settle();assert.equal(f.session.length,0);assert.deepEqual(f.updates.map(x=>x.id),[1]);
  assert.equal(f.guard('https://www.youtube.com/').allowed,false);for(const q of f.queries)assert(q.url.every(x=>!x.includes('x.com')&&x!=='<all_urls>'));
});
test('unsigned and altered grants never open access',async()=>{
  const f=fixture();await f.settle();for(const data of [f.policy(),null,{version:1}]){await f.send(data);assert.equal(f.session.length,0);}
  const changed=sign(f.policy());const p=f.policy();p.allowed_until+=100;changed.payload=Buffer.from(JSON.stringify(p)).toString('base64');await f.send(changed);assert.equal(f.session.length,0);
});
test('identical duplicate leases do not extend their signed expiry',async()=>{
  const f=fixture();await f.settle();const signed=sign(f.policy());await f.send(signed);f.advance(3000);await f.send(signed);assert.equal(f.session.length,1);
  f.advance(1100);await f.settle();assert.equal(f.session.length,0);
});
test('session deadline applies independently of heartbeat freshness',async()=>{
  const f=fixture();await f.settle();const p=f.policy();p.allowed_until=p.issued_at+1;await f.send(sign(p));f.advance(1100);await f.settle();assert.equal(f.session.length,0);
});
test('a legitimate removal is allowed only while trusted; fallback covers default and extra domains',async()=>{
  const f=fixture();await f.settle();const p=f.policy();p.domains.push('extra.example');await f.send(sign(p));f.advance(100);
  const removed=f.policy();removed.domains=[];removed.exceptions=['youtube.com'];await f.send(sign(removed));assert.equal(f.guard('https://www.youtube.com/').allowed,true);
  f.advance(4100);await f.settle();const deny=f.dynamic.find(x=>x.action.type==='block');assert(deny.condition.requestDomains.includes('youtube.com'));assert(deny.condition.requestDomains.includes('extra.example'));
  assert.equal(f.guard('https://www.youtube.com/').allowed,false);assert.equal(f.guard('https://accounts.youtube.com/').allowed,true);
  await f.send(sign(f.policy()));assert.equal(f.session.length,1);
});
test('older verified grant cannot roll back a newer signed deny',async()=>{
  const f=fixture();await f.settle();const old=sign(f.policy());f.advance(100);const denied=f.policy();denied.allowed_until=0;await f.send(sign(denied));await f.send(old);assert.equal(f.session.length,0);assert.equal(f.guard('https://www.youtube.com/').allowed,false);
});
for(const invalidation of ['invalid','disconnect'])test('pending signature cannot resurrect after '+invalidation,async()=>{
  const f=fixture();await f.settle();f.hold();const pending=f.start(sign(f.policy()));for(let i=0;i<100&&!f.pending();i++)await new Promise(setImmediate);assert(f.pending()>0);
  if(invalidation==='invalid')await f.send({bad:true});else {f.port.onDisconnect.emit();await f.settle();}
  f.release();await pending;await f.settle();assert.equal(f.session.length,0);assert.equal(f.guard('https://www.youtube.com/').allowed,false);
});

test('same-evaluation-time signed revisions extend an authorized session without closing its tab',async()=>{
 const f=fixture();await f.settle();const initial=f.policy();await f.send(sign(initial));f.updates.length=0;
 await f.send(sign({...initial,allowed_until:initial.allowed_until+60}));
 assert.equal(f.session.length,1);assert.equal(f.updates.length,0);assert.equal(f.guard('https://www.youtube.com/').allowed,true);
 assert.equal(f.guard('https://www.youtube.com/').allowedUntil,(initial.allowed_until+60)*1000);
});
test('interrupted relaxed-map mutation repairs actual DNR state after disconnect and repeated ticks',async()=>{
 const f=fixture();await f.settle();f.holdDynamic();
 await f.start(sign({...f.policy(),domains:[],exceptions:[]}));
 for(let i=0;i<100&&!f.dnrPending();i++)await new Promise(setImmediate);assert.equal(f.dnrPending(),1);assert.equal(f.dynamic.length,0);
 f.port.onDisconnect.emit();for(let i=0;i<5;i++){f.advance(250);await new Promise(setImmediate);}
 f.releaseDnr();await f.settle();assert.equal(f.session.length,0);
 assert(f.dynamic.some(r=>r.action.type==='block'&&r.condition.requestDomains.includes('youtube.com')));
 assert.equal(f.guard('https://www.youtube.com/').allowed,false);
});
test('a revocation lasting over 200ms converges despite repeated expired-policy timer ticks',async()=>{
 const f=fixture();await f.settle();await f.send(sign(f.policy()));assert.equal(f.session.length,1);f.holdSession();
 f.advance(4100);for(let i=0;i<100&&!f.dnrPending();i++)await new Promise(setImmediate);assert.equal(f.dnrPending(),1);
 for(let i=0;i<5;i++){f.advance(250);await new Promise(setImmediate);}
 f.releaseDnr();await f.settle();assert.equal(f.session.length,0);assert.equal(f.guard('https://www.youtube.com/').allowed,false);
});

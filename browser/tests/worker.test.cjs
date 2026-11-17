const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture() {
  let now = 1_000_000, dynamic = [], session = [];
  const intervals = [], updates = [], queries = [];
  const event = () => ({listeners: [], addListener(fn) {this.listeners.push(fn);}, emit(...args) {for (const fn of this.listeners) fn(...args);}});
  const port = {onMessage: event(), onDisconnect: event()};
  const chrome = {
    runtime: {getURL: file => 'chrome-extension://fixture/' + file, connectNative: () => port, onMessage:event(), onStartup:event(), onInstalled:event()},
    declarativeNetRequest: {
      getDynamicRules: async () => dynamic, updateDynamicRules: async change => {dynamic = change.addRules || [];},
      getSessionRules: async () => session, updateSessionRules: async change => {session = change.addRules || [];},
      updateEnabledRulesets: async () => {},
    },
    tabs: {query:async options => {queries.push(options);return [{id:1,url:'https://www.youtube.com/'},{id:2,url:'https://accounts.youtube.com/'}];},update:async(id, options)=>{updates.push({id,...options});},sendMessage:async()=>{},onUpdated:event()},
  };
  const context = vm.createContext({chrome, URL, Date:{now:()=>now}, setInterval:fn=>intervals.push(fn),setTimeout:()=>{}});
  context.importScripts = file => vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension',file),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension/worker.js'),'utf8'),context);
  return {port, chrome, updates, queries, get dynamic(){return dynamic;},get session(){return session;},
    policy:()=>({version:1,issued_at:now/1000,lease_until:now/1000+4,allowed_until:now/1000+60,domains:['youtube.com','googlevideo.com'],exceptions:['accounts.youtube.com']}),
    settle:()=>vm.runInContext('serial',context),advance:ms=>{now+=ms;for(const fn of intervals)fn();}};
}

test('default closed, valid session opens, native disconnect removes allows and closes only YouTube',async()=>{
  const f=fixture();await f.settle();assert.equal(f.session.length,0);
  assert(f.dynamic.some(r=>r.action.type==='block'));
  f.port.onMessage.emit(f.policy());await f.settle();assert.equal(f.session[0].action.type,'allow');
  f.updates.length=0;f.port.onDisconnect.emit();await f.settle();assert.equal(f.session.length,0);
  assert.deepEqual(f.updates.map(x=>x.id),[1]);
  for(const query of f.queries)assert(query.url.every(pattern=>!pattern.includes('x.com')&&pattern!=='<all_urls>'));
});

test('a stale heartbeat removes permission even when the native port stays connected',async()=>{
  const f=fixture();await f.settle();f.port.onMessage.emit(f.policy());await f.settle();assert.equal(f.session.length,1);
  f.advance(4100);await f.settle();assert.equal(f.session.length,0);
});

test('session expiry is enforced independently of a fresh policy heartbeat',async()=>{
  const f=fixture();await f.settle();const p=f.policy();p.allowed_until=p.issued_at+1;
  f.port.onMessage.emit(p);await f.settle();assert.equal(f.session.length,1);
  f.advance(1100);await f.settle();assert.equal(f.session.length,0);
});

test('malformed or future policy cannot grant access',async()=>{
  const f=fixture();await f.settle();for(const data of [null,{version:1}, {...f.policy(),issued_at:9999999}]){
    f.port.onMessage.emit(data);await f.settle();assert.equal(f.session.length,0);
  }
});

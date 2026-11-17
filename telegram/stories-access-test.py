"""Exercise extracted story navigation code; this is not a GUI/login test."""
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'tdesktop/Telegram/SourceFiles'
controller = (SOURCE / 'media/stories/media_stories_controller.cpp').read_text()
negative = sys.argv[1] if len(sys.argv) == 2 else None
MODES = ('siblings', 'watcher', 'navigation', 'entry', 'paging_boundary',
         'paging_callback', 'media_entry', 'media_watcher', 'media_navigation')
if negative not in (None, *MODES):
    raise SystemExit('Optional negative control: ' + ', '.join(MODES))
if negative == 'siblings':
    controller = controller.replace('source && !Stopscroll::Blocked(source->peer)',
                                    'source && (true || !Stopscroll::Blocked(source->peer))')
elif negative == 'watcher':
    controller = controller.replace('else if (Stopscroll::Blocked(shownPeer()))',
                                    'else if (false && Stopscroll::Blocked(shownPeer()))')
elif negative == 'navigation':
    original = 'if (_session && v::is<Data::StorySourcesList>(_context.data)) {'
    controller = controller.replace(original, 'if (false && _session && v::is<Data::StorySourcesList>(_context.data)) {')
elif negative == 'paging_boundary':
    controller = controller.replace('if (!right) {', 'if (false && !right) {')


def function(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


methods = '\n'.join(function(controller, signature).replace('Controller::', 'Fixture::')
                    for signature in ('void Controller::showSiblings(',
                                      'void Controller::showSibling(',
                                      'bool Controller::jumpFor(',
                                      'void Controller::checkStoryAccess('))
preamble = r'''
#include <cassert>
#include <algorithm>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <variant>
#include <vector>
template<class T> using not_null = T;
using PeerId = int;
struct PeerData;
struct Story { PeerData *value; PeerData *peer() { return value; } };
struct Source { PeerData *peer; };
struct Subscriber { std::function<void()> callback; };
struct Producer { std::vector<std::function<void()>> *listeners; };
inline void operator|(Producer producer,Subscriber subscriber) { producer.listeners->push_back(subscriber.callback); }
namespace rpl { template<class F,class L> Subscriber on_next(F callback,L &) { return {callback}; } }
struct Stories {
 std::map<int, Source> sources;
 std::map<int, Story*> items;
 Source *source(int id) { auto i=sources.find(id); return i==sources.end()?nullptr:&i->second; }
 std::optional<Story*> lookup(int id) { auto i=items.find(id); return i==items.end()?std::nullopt:std::optional(i->second); }
 std::vector<int> raw;
 bool hasMore=false, requestPending=false;
 int pageRequests=0;
 std::vector<std::function<void()>> listeners;
 template<class L> void loadMore(L) { if (hasMore && !requestPending) { requestPending=true; ++pageRequests; } }
 template<class L> Producer sourcesChanged(L) { return {&listeners}; }
 void completePage(const std::vector<PeerData*> &peers,bool more);
};
struct SessionData { Stories storiesValue; Stories &stories() { return storiesValue; } PeerData *peer(int id) { return stories().source(id)->peer; } };
namespace Main { struct Session { SessionData value; SessionData &data() { return value; } }; }
struct PeerData { int id; bool blocked; Main::Session *owner; Main::Session &session() { return *owner; } };
void Stories::completePage(const std::vector<PeerData*> &peers,bool more) {
 assert(requestPending); requestPending=false; hasMore=more;
 for (auto peer : peers) { sources[peer->id]={peer}; raw.push_back(peer->id); }
 auto callbacks=listeners; for (auto &callback:callbacks) callback();
}
namespace Stopscroll {
 bool Blocked(PeerData *peer) { return peer && peer->blocked; }
 bool Prevent(PeerData *peer,void *) { return Blocked(peer); }
}
namespace Data { struct StorySourcesList {}; struct OtherContext {}; }
namespace Media::View {}
struct Context { std::variant<Data::StorySourcesList,Data::OtherContext> data; };
namespace v { template<class T,class V> bool is(const V &value) { return std::holds_alternative<T>(value); } bool is_null(int data) { return data==0; } }
struct Sibling {
 PeerData *value;
 template<class T> Sibling(T *,Source source,int):value(source.peer) {}
 bool shows(Source source,int) { return source.peer==value; }
 PeerData *peer() { return value; }
 int shownId() { return value->id; }
};
struct Delegate {
 std::vector<int> jumps;
 int closes=0;
 void storiesJumpTo(Main::Session *,int id,Context) { jumps.push_back(id); }
 void storiesClose() { ++closes; }
};
struct Fixture {
 struct CachedSource { int peerId=0, shownId=0; explicit operator bool() const { return peerId!=0; } };
 std::vector<CachedSource> _cachedSourcesList;
 int _cachedSourceIndex=0, _index=0;
 Context _context;
 Main::Session *_session;
 Delegate *_delegate;
 std::unique_ptr<Sibling> _siblingLeft, _siblingRight;
 PeerData *_current=nullptr;
 bool _isShown=true;
 int repaints=0;
 bool shown() { return _isShown; }
 PeerData *shownPeer() { return _current; }
 void repaint() { ++repaints; }
 int shownCount() { return 1; }
 void markAsRead() {}
 void showSiblings(Main::Session *session);
 void showSibling(std::unique_ptr<Sibling> &,Main::Session *,CachedSource);
 bool jumpFor(int delta);
 void checkStoryAccess();
 void subscribe();
 void rebuildFromContext(PeerData *peer,[[maybe_unused]] int storyId) {
  _cachedSourcesList.clear();
  for (auto id:_session->data().stories().raw) _cachedSourcesList.push_back({id,id});
  for (int i=0;i<int(_cachedSourcesList.size());++i) if (_cachedSourcesList[i].peerId==peer->id) _cachedSourceIndex=i;
  showSiblings(_session);
 }
};
'''
overlay = (SOURCE / 'media/view/media_view_overlay_widget.cpp').read_text()
if negative == 'entry':
    overlay = overlay.replace('story && Stopscroll::Blocked(story->peer())',
                              'story && false && Stopscroll::Blocked(story->peer())')
elif negative == 'media_entry':
    overlay = overlay.replace('contextItem && Stopscroll::Blocked(contextItem->history()->peer)',
                              'contextItem && false && Stopscroll::Blocked(contextItem->history()->peer)')
elif negative == 'media_watcher':
    overlay = overlay.replace('!isHidden() && Stopscroll::Blocked(_peer)',
                              '!isHidden() && false && Stopscroll::Blocked(_peer)')
elif negative == 'media_navigation':
    overlay = overlay.replace('if (Stopscroll::Blocked(peer)) {',
                              'if (false && Stopscroll::Blocked(peer)) {')
window = (SOURCE / 'window/window_session_controller.cpp').read_text()


def prefix(source, signature, marker):
    body = function(source, signature)
    return body[body.index('{') + 1:body.index(marker)]


guards = r'''
#define Expects(value) assert(value)
struct PeerRef { PeerData *value; PeerData *get() { return value; } operator PeerData*() { return value; } };
struct History { PeerRef peer; };
struct HistoryItem { History *value; PeerData *forwardedFrom; History *history() { return value; } };
struct Request { Story *value=nullptr; HistoryItem *contextItem=nullptr; PeerData *contextPeer=nullptr; Story *story() { return value; } HistoryItem *item() { return contextItem; } PeerData *peer() { return contextPeer; } };
struct StoryKey { int value; StoryKey(int id):value(id) {} bool valid() { return value!=0; } operator int() { return value; } };
struct QTimer {
 void *parent;
 std::function<void()> callback;
 explicit QTimer(void *owner):parent(owner) {}
 static void timeout() {}
 void start(int interval) { assert(interval==1000); }
};
struct QObject {
 template<class F> static void connect(QTimer *timer,void(*)(),void *context,F callback) {
  assert(context==timer->parent); timer->callback=callback;
 }
};
struct Entries {
 int displayed=0, closed=0;
 void *_stories=nullptr;
 Main::Session *owner;
 PeerData *_peer=nullptr;
 bool hidden=false;
 void *_body=nullptr;
 std::unique_ptr<QTimer> watcher;
 bool isHidden() { return hidden; }
 Main::Session &session() { return *owner; }
 void close() { ++closed; }
 void open(Request request) {
''' + prefix(overlay, 'void OverlayWidget::show(OpenRequest request)', 'const auto document') + r'''
  ++displayed;
 }
 void jump(Main::Session *session,StoryKey id) {
''' + prefix(overlay, 'void OverlayWidget::storiesJumpTo(', 'setContext(') + r'''
  ++displayed;
 }
 void redisplay(Story *story) {
''' + prefix(overlay, 'void OverlayWidget::storiesRedisplay(', 'clearStreaming();') + r'''
  ++displayed;
 }
 void openPeerStory(PeerData *peer) {
''' + prefix(window, 'void SessionController::openPeerStory(', 'invalidate_weak_ptrs(') + r'''
  ++displayed;
 }
 void openPeerStories(int peerId) {
''' + prefix(window, 'void SessionController::openPeerStories(', 'invalidate_weak_ptrs(') + r'''
  ++displayed;
 }
 struct Entity { int data=1; HistoryItem *item=nullptr; };
 bool moveToEntity(Entity entity) {
''' + prefix(overlay, 'bool OverlayWidget::moveToEntity(', 'if (const auto item = entity.item)') + r'''
  ++displayed; return true;
 }
 void installWatcher() {
''' + overlay[overlay.index('const auto timer = new QTimer(_body);'):
              overlay.index('timer->start(1000);') + len('timer->start(1000);')] + r'''
  watcher.reset(timer);
 }
};
'''
start = controller.index('stories.sourcesChanged(list) | rpl::on_next([=] {')
end = controller.index('}, _contextLifetime);', start) + len('}, _contextLifetime);')
subscription = controller[start:end]
if negative == 'paging_callback':
    subscription = subscription.replace('rebuildFromContext(peer, storyId);',
                                         'if (false) { rebuildFromContext(peer, storyId); }')
subscriber = r'''
void Fixture::subscribe() {
 auto &stories=_session->data().stories();
 auto list=Data::StorySourcesList{};
 auto peer=_current;
 int storyId=peer->id;
 int _contextLifetime=0;
''' + subscription + '\n}\n'
main = r'''
int main() {
 Main::Session session;
 PeerData allowedA{1,false,&session}, blockedA{2,true,&session}, current{3,false,&session}, blockedB{4,true,&session}, allowedB{5,false,&session};
 for (auto peer : {&allowedA,&blockedA,&current,&blockedB,&allowedB}) session.data().stories().sources[peer->id]={peer};
 Delegate delegate;
 Fixture fixture{{{1,1},{2,2},{3,3},{4,4},{5,5}},2,0,{},&session,&delegate};
 fixture._current=&current;
 fixture.showSiblings(&session);
 assert(fixture._siblingLeft && fixture._siblingLeft->peer()==&allowedA);
 assert(fixture._siblingRight && fixture._siblingRight->peer()==&allowedB);
 assert(fixture._cachedSourcesList.size()==5);
 assert(fixture.jumpFor(-1) && delegate.jumps.back()==1);
 assert(fixture.jumpFor(1) && delegate.jumps.back()==5);
 blockedA.blocked=false;
 fixture.showSiblings(&session);
 assert(fixture._siblingLeft->peer()==&blockedA);
 blockedA.blocked=true;
 assert(fixture.jumpFor(-1) && delegate.jumps.back()==1);
 allowedA.blocked=true; allowedB.blocked=true;
 fixture.showSiblings(&session);
 assert(!fixture._siblingLeft && !fixture._siblingRight);
 assert(!fixture.jumpFor(-1) && !fixture.jumpFor(1));
 assert(fixture._cachedSourcesList.size()==5);
 allowedA.blocked=false;
 fixture.checkStoryAccess();
 assert(fixture._siblingLeft && fixture._siblingLeft->peer()==&allowedA && fixture.repaints==1);
 current.blocked=true;
 fixture.checkStoryAccess();
 assert(delegate.closes==1 && fixture.repaints==1);
 fixture._isShown=false;
 fixture.checkStoryAccess();
 assert(delegate.closes==1);
 fixture._isShown=true; current.blocked=false;
 fixture._context.data=Data::OtherContext{};
 fixture.checkStoryAccess();
 assert(fixture.repaints==1 && delegate.closes==1);
 Story allowedStory{&current}, blockedStory{&blockedA};
 session.data().stories().items[3]=&allowedStory;
 session.data().stories().items[2]=&blockedStory;
 Entries entries{0,0,&session,&session};
 entries.open({&blockedStory}); entries.jump(&session,2);
 entries.openPeerStory(&blockedA); entries.openPeerStories(2);
 assert(entries.displayed==0 && entries.closed==0);
 entries.redisplay(&blockedStory);
 assert(entries.displayed==0 && entries.closed==1);
 entries.open({&allowedStory}); entries.jump(&session,3); entries.redisplay(&allowedStory);
 entries.openPeerStory(&current); entries.openPeerStories(3);
 assert(entries.displayed==5 && entries.closed==1);
 History blockedHistory{{&blockedA}}, allowedHistory{{&current}};
 HistoryItem blockedItem{&blockedHistory,nullptr}, forwardedAllowedItem{&allowedHistory,&blockedA};
 entries.open({nullptr,&blockedItem}); entries.open({nullptr,nullptr,&blockedA});
 assert(entries.displayed==5);
 entries.open({nullptr,&forwardedAllowedItem}); entries.open({nullptr,nullptr,&current});
 assert(entries.displayed==7);
 entries._peer=&current;
 assert(!entries.moveToEntity({1,&blockedItem}) && entries.displayed==7);
 entries._peer=&blockedA;
 assert(!entries.moveToEntity({1,nullptr}) && entries.displayed==7);
 assert(entries.moveToEntity({1,&forwardedAllowedItem}) && entries.displayed==8);
 entries._body=&session; entries.installWatcher();
 entries.watcher->callback(); assert(entries.closed==2);
 entries.hidden=true; entries.watcher->callback(); assert(entries.closed==2);
 entries.hidden=false; entries._peer=&current; entries.watcher->callback(); assert(entries.closed==2);
 entries._peer=nullptr; entries.watcher->callback(); assert(entries.closed==2);
 allowedB.blocked=false;
 auto &paging=session.data().stories();
 paging.raw={1,2,4}; paging.hasMore=true;
 Fixture carousel{{{1,1},{2,2},{4,4}},0,0,{},&session,&delegate};
 carousel._current=&allowedA; carousel.subscribe(); carousel.showSiblings(&session);
 assert(!carousel._siblingRight && paging.requestPending && paging.pageRequests==1);
 assert(carousel._cachedSourcesList.size()==3 && paging.raw.size()==3);
 carousel.checkStoryAccess(); assert(paging.pageRequests==1);
 PeerData blockedC{6,true,&session};
 paging.completePage({&blockedC},true);
 assert(carousel._cachedSourcesList.size()==4 && paging.raw.size()==4);
 assert(!carousel._siblingRight && paging.requestPending && paging.pageRequests==2);
 paging.completePage({&allowedB},false);
 assert(carousel._siblingRight && carousel._siblingRight->peer()==&allowedB);
 assert(carousel._cachedSourcesList.size()==5 && paging.raw.size()==5);
 assert(carousel.jumpFor(1) && delegate.jumps.back()==5);
 carousel.checkStoryAccess(); assert(paging.pageRequests==2 && !paging.requestPending);
 std::cout << "PASS: exact story sibling/carousel methods skip blocked channels, preserve raw cache, refresh navigation and handle all-blocked neighbors\n";
 std::cout << "PASS: exact policy-check method closes active blocked stories and refreshes neighbors; exact entry prefixes reject blocked content before render\n";
 std::cout << "PASS: exact boundary helper and page subscription continue past blocked server-page suffixes and rebuild after asynchronous pages\n";
 std::cout << "PASS: exact normal-media entry/navigation guards and QObject-context watcher reject blocked histories and preserve allowed-chat forwards\n";
}
'''
with tempfile.TemporaryDirectory(prefix='stopscroll-story-test-') as temporary:
    source = Path(temporary) / 'test.cpp'
    source.write_text(preamble + methods + guards + subscriber + main)
    binary = Path(temporary) / 'test'
    subprocess.run(['g++', '-std=c++20', '-Wall', '-Wextra', '-Werror',
                    '-Wno-missing-field-initializers', '-Wno-sign-compare', '-Wno-deprecated',
                    str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)

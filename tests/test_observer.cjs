const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class FakeXHR {
  constructor() { this.events = {}; this.responseText = ''; }
  open() {}
  send() {}
  setRequestHeader() {}
  addEventListener(type, listener) { (this.events[type] ||= []).push(listener); }
  emit(type) { for (const f of this.events[type] || []) f(); }
}
const context = {window: {fetch: async () => ({})}, location: {href: 'https://app.sowork.com/s/office/floor', pathname: '/s/office/floor'},
  XMLHttpRequest: FakeXHR, URL, URLSearchParams, Headers, Request, TextDecoder};
vm.createContext(context);
vm.runInContext(fs.readFileSync('sowork_inspector/observer.js', 'utf8'), context);
const state = context.window.__soworkObserver;
assert.equal(state.groupId, 'office');
// Firestore may arrive before the first rooms request.
const stream = new FakeXHR();
stream.open('GET', 'https://firestore.googleapis.com/google.firestore.v1.Firestore/Listen/channel');
stream.send();
const document = {name: 'projects/p/databases/(default)/documents/group_membermaps/office_0', fields: {
  users: {mapValue: {fields: {u1: {mapValue: {fields: {userName: {stringValue: 'Example [with brackets]'},
    token: {stringValue: 'not-exported'}, isOnline: {booleanValue: true}}}}}}}}};
const body = '123\n' + JSON.stringify([[0, {documentChange: {document}}]]) + '\n8\n[[1,{}]]';
for (const char of body) { stream.responseText += char; stream.emit('progress'); }
stream.emit('load');
assert.equal(state.identities.u1.name, 'Example [with brackets]');
assert.deepEqual(Object.keys(state.identities.u1), ['name']);
const request = new FakeXHR();
request.open('POST', 'https://api.sowork.com/api/v1/gaia/rooms');
request.setRequestHeader('source', 'web');
request.setRequestHeader('Authorization', 'not-exported');
request.send(JSON.stringify({groupId: 'office'}));
assert.equal(state.seenRooms, true);
assert.equal(state.headers, undefined);
state.officeConnected = true;
request.status = 200;
request.responseText = JSON.stringify({roomDefinitionMap: {floor: {rooms: []}}});
request.emit('load');
assert.equal(state.rooms.groupId, 'office');
assert.equal(state.rooms.status, 200);
assert.equal(state.identities.u1.token, undefined);
console.log('Observer: fragmented Firestore, identity sanitization and request metadata passed');
(async () => {
  const script = fs.readFileSync('sowork_inspector/client.py', 'utf8').match(/FETCH_ROOMS = '''([\s\S]*?)'''/)[1];
  const observer = {seenRooms: true, officeConnected: true, groupId: 'office', memberCount: 1,
    expectedPartitions: {p: 24}, partitions: {p: {valid: true, targets: [24], members: {u1: {name: 'Example', online: true}}}},
    targets: {24: {current: true, at: Date.now()}}};
  async function read() {
    return new Promise(resolve => vm.runInNewContext('(function(){' + script + '\n}).apply(null, args)', {
      window: {__soworkObserver: observer}, args: ['office', 120000, resolve],
      fetch: () => { throw new Error('Collector must never send requests'); }
    }));
  }
  assert.equal((await read()).members.u1.online, true);
  observer.targets[24].at -= 121000;
  assert.equal((await read()).error, 'stale');
  observer.targets[24].at = Date.now(); observer.targets[24].pending = true;
  assert.equal((await read()).error, 'stale');
  observer.targets[24].pending = false; observer.memberCount = 9;
  assert.equal((await read()).members.u1.online, true);
  observer.expectedPartitions.missing = 26;
  assert.equal((await read()).error, 'incomplete');
  delete observer.expectedPartitions.missing;
  observer.memberCount = 1; observer.officeConnected = false;
  assert.equal((await read()).error, 'office_not_ready');
  console.log('Collector: passive Firestore read, stale/pending data, completeness and disconnection passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
{
  class FakeSocket extends EventTarget {
    static OPEN = 1;
    constructor() { super(); this.readyState = 1; }
  }
  const sandbox = {window: {fetch: async () => ({}), WebSocket: FakeSocket},
    location: {href: 'https://app.sowork.com/s/office/floor', pathname: '/s/office/floor'},
    XMLHttpRequest: class { open() {} send() {} setRequestHeader() {} }, URL, URLSearchParams, Headers, Request, TextDecoder};
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync('sowork_inspector/observer.js', 'utf8'), sandbox);
  const state = sandbox.window.__soworkObserver;
  const chat = new sandbox.window.WebSocket('wss://chat.sowork.com/ws');
  chat.dispatchEvent(new Event('message'));
  assert.equal(state.officeConnected, false);
  const gaia = new sandbox.window.WebSocket('wss://api.sowork.com/gaia/g1/v2/');
  assert.equal(state.officeConnected, false);
  gaia.dispatchEvent(new Event('message'));
  assert.equal(state.officeConnected, true);
  gaia.dispatchEvent(new Event('close'));
  assert.equal(state.officeConnected, false);
  console.log('Office connection: lobby/chat rejected, active Gaia accepted, disconnect detected');
}

// Server CURRENT/readTime, rather than an open Gaia socket alone, confirms freshness.
const sync = new FakeXHR();
sync.open('GET', 'https://firestore.googleapis.com/google.firestore.v1.Firestore/Listen/channel');
sync.send();
function feed(message) {
  sync.responseText += JSON.stringify([[0, message]]);
  sync.emit('progress');
}
feed({documentChange: {document, targetIds: [24]}});
assert.equal(state.targets[24].pending, true);
assert.equal(state.targets[24].current, false);
feed({targetChange: {targetChangeType: 'CURRENT', targetIds: [24], readTime: 'test'}});
assert.equal(state.targets[24].current, true);
assert.equal(state.targets[24].pending, false);
assert.ok(state.targets[24].at > 0);
feed({documentChange: {document: {name: 'projects/p/databases/(default)/documents/groups/office',
  fields: {memberCount: {integerValue: '1'}}}}});
assert.equal(state.memberCount, 1);
feed({targetChange: {targetChangeType: 'RESET', targetIds: [24]}});
assert.equal(state.targets[24].current, false);
console.log('Firestore: partition presence, member count, synchronization and reset passed');

feed({documentChange: {document: {name: 'projects/p/databases/(default)/documents/group_membermaps/office_empty',
  fields: {users: {mapValue: {}}}}, targetIds: [26]}});
const emptyPartition = state.partitions['projects/p/databases/(default)/documents/group_membermaps/office_empty'];
assert.equal(emptyPartition.valid, true);
assert.equal(Object.keys(emptyPartition.members).length, 0);
console.log('Firestore: empty partition with omitted fields accepted');

const subscribe = new FakeXHR();
subscribe.open('POST', 'https://firestore.googleapis.com/google.firestore.v1.Firestore/Listen/channel');
const memberPath = 'projects/p/databases/(default)/documents/group_membermaps/office_0';
subscribe.send(new URLSearchParams({'req0___data__': JSON.stringify({addTarget: {
  targetId: 24, documents: {documents: [memberPath]}}})}).toString());
assert.equal(state.expectedPartitions[memberPath], 24);
feed({documentChange: {document}});
assert.deepEqual(Array.from(state.partitions[memberPath].targets), [24]);
assert.equal(state.targets[24].pending, true);
console.log('Firestore: native subscriptions observed; target retained on subsequent updates');

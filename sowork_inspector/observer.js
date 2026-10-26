(() => {
  if (window.__soworkObserver) return;
  const state = window.__soworkObserver = {groupId: location.pathname.match(/^\/s\/([^/]+)/)?.[1] || null, identities: {}, rooms: null, memberCount: null, partitions: {}, expectedPartitions: {}, targets: {}, seenRooms: false, officeConnected: false};
  // A rooms request also happens in the lobby. Require an active Gaia session.
  const NativeWebSocket = window.WebSocket;
  if (NativeWebSocket) {
    const joinedSockets = new Set();
    window.WebSocket = new Proxy(NativeWebSocket, {
      construct(target, args, newTarget) {
        const socket = Reflect.construct(target, args, newTarget);
        try {
          const url = new URL(String(args[0]), location.href);
          if (url.hostname === 'api.sowork.com' && url.pathname.startsWith('/gaia/')) {
            socket.addEventListener('message', () => {
              if (socket.readyState === NativeWebSocket.OPEN) joinedSockets.add(socket);
              state.officeConnected = joinedSockets.size > 0;
            });
            const disconnected = () => {
              joinedSockets.delete(socket);
              state.officeConnected = joinedSockets.size > 0;
              if (!state.officeConnected) state.rooms = null;
            };
            socket.addEventListener('close', disconnected);
            socket.addEventListener('error', disconnected);
          }
        } catch (_) {}
        return socket;
      }
    });
  }
  function metadata(url, body) {
    try {
      const u = new URL(url, location.href);
      if (u.hostname !== 'api.sowork.com' || u.pathname !== '/api/v1/gaia/rooms') return null;
      const data = JSON.parse(body);
      if (typeof data.groupId !== 'string') return null;
      if (!state.groupId) state.groupId = data.groupId;
      state.seenRooms = true;
      return data.groupId;
    } catch (_) { return null; }
  }
  function subscriptions(url, body) {
    try {
      if (new URL(url, location.href).hostname !== 'firestore.googleapis.com') return;
      const params = new URLSearchParams(body);
      for (const [key, raw] of params) {
        if (!/^req\d+___data__$/.test(key)) continue;
        const target = JSON.parse(raw).addTarget;
        if (!Number.isInteger(target?.targetId)) continue;
        for (const name of target.documents?.documents || []) {
          const parts = name.split('/');
          if (parts.at(-2) !== 'group_membermaps' || !state.groupId ||
              !parts.at(-1).startsWith(state.groupId + '_')) continue;
          state.expectedPartitions[name] = target.targetId;
        }
      }
    } catch (_) {}
  }
  function captureRooms(groupId, status, payload) {
    if (!groupId || groupId !== state.groupId || !state.officeConnected) return;
    state.rooms = {groupId, status, payload: status >= 200 && status < 300 ? payload : null,
      receivedAt: Date.now()};
  }
  function invalidateTargets() {
    for (const target of Object.values(state.targets)) target.current = false;
  }
  function documents(value) {
    if (!value || typeof value !== 'object') return;
    const change = value.documentChange;
    const doc = change?.document;
    if (doc && state.groupId) {
      const parts = doc.name.split('/');
      if (parts.at(-2) === 'groups' && parts.at(-1) === state.groupId) {
        const count = Number(doc.fields?.memberCount?.integerValue);
        state.memberCount = Number.isInteger(count) && count >= 0 ? count : null;
      }
      if (parts.at(-2) === 'group_membermaps' && parts.at(-1).startsWith(state.groupId + '_')) {
        const members = {};
        const map = doc.fields?.users?.mapValue;
        const fields = map?.fields || {};
        let valid = !!map;
        for (const [id, member] of Object.entries(fields || {})) {
          const data = member.mapValue?.fields;
          const online = data?.isOnline?.booleanValue;
          if (typeof online !== 'boolean') valid = false;
          const name = data?.userName?.stringValue || id;
          members[id] = {name, online, roomId: data?.roomDefinitionId?.stringValue || ''};
          state.identities[id] = {name};
        }
        const targets = change.targetIds?.length ? change.targetIds : (state.partitions[doc.name]?.targets || []);
        state.partitions[doc.name] = {members, valid, targets};
        for (const id of targets) {
          const target = state.targets[id] ||= {current: false, at: 0};
          // A document change is not committed until the server's next readTime.
          target.pending = true;
        }
      }
    }
    const tc = value.targetChange;
    if (tc) {
      const ids = tc.targetIds?.length ? tc.targetIds : Object.keys(state.targets);
      for (const id of ids) {
        const target = state.targets[id] ||= {current: false, at: 0};
        if (['ADD', 'RESET', 'REMOVE'].includes(tc.targetChangeType)) {
          target.current = false; target.at = 0;
        }
        if (tc.targetChangeType === 'CURRENT') target.current = true;
        if (tc.readTime && target.current) { target.at = Date.now(); target.pending = false; }
      }
    }
    const removed = value.documentDelete || value.documentRemove;
    if (removed && Object.hasOwn(state.partitions, removed.document)) invalidateTargets();
    if (Object.hasOwn(value, 'filter') && Object.values(state.partitions).some(
      partition => partition.targets.includes(value.filter.targetId))) invalidateTargets();
    for (const child of Object.values(value)) documents(child);
  }
  // Extract complete JSON arrays incrementally, even while Listen requests stay open.
  function reader() {
    let buffer = '';
    return chunk => {
      buffer += chunk;
      while (true) {
        const start = buffer.indexOf('[');
        if (start < 0) { buffer = buffer.slice(-32); return; }
        let depth = 0, quoted = false, escape = false, end = -1;
        for (let i = start; i < buffer.length; i++) {
          const c = buffer[i];
          if (quoted) { if (escape) escape = false; else if (c === '\\') escape = true; else if (c === '"') quoted = false; }
          else if (c === '"') quoted = true;
          else if (c === '[' || c === '{') depth++;
          else if (c === ']' || c === '}') { depth--; if (!depth) { end = i + 1; break; } }
        }
        if (end < 0) { buffer = buffer.slice(start); return; }
        try { documents(JSON.parse(buffer.slice(start, end))); } catch (_) {}
        buffer = buffer.slice(end);
      }
    };
  }
  const originalFetch = window.fetch;
  window.fetch = async function(input, init) {
    const url = input instanceof Request ? input.url : String(input);
    let body = init?.body;
    if (body === undefined && input instanceof Request && ['api.sowork.com', 'firestore.googleapis.com'].includes(new URL(url, location.href).hostname)) {
      try { body = await input.clone().text(); } catch (_) {}
    }
    const groupId = metadata(url, body);
    subscriptions(url, body);
    const response = await originalFetch.apply(this, arguments);
    if (groupId) {
      const copy = response.clone();
      if (!response.ok) captureRooms(groupId, response.status, null);
      else copy.json().then(payload => captureRooms(groupId, response.status, payload))
        .catch(() => captureRooms(groupId, 0, null));
    }
    if (new URL(url, location.href).hostname === 'firestore.googleapis.com') {
      const copy = response.clone();
      (async () => {
        const stream = copy.body?.getReader(); if (!stream) return;
        const decoder = new TextDecoder(), ingest = reader();
        while (true) { const {value, done} = await stream.read(); if (done) break; ingest(decoder.decode(value, {stream: true})); }
      })().catch(invalidateTargets);
    }
    return response;
  };
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__swUrl = String(url);
    return open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    const groupId = metadata(this.__swUrl, body);
    subscriptions(this.__swUrl, body);
    if (groupId) this.addEventListener('load', () => {
      try {
        const payload = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
        captureRooms(groupId, this.status, payload);
      } catch (_) { captureRooms(groupId, this.status || 0, null); }
    });
    if (new URL(this.__swUrl, location.href).hostname === 'firestore.googleapis.com') {
      const ingest = reader(); let offset = 0;
      const read = () => {
        try { const text = this.responseText; ingest(text.slice(offset)); offset = text.length; } catch (_) {}
      };
      this.addEventListener('progress', read); this.addEventListener('load', read);
      this.addEventListener('error', invalidateTargets);
      this.addEventListener('abort', invalidateTargets);
    }
    return send.apply(this, arguments);
  };
})();

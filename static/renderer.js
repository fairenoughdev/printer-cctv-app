// Shared by the editor, overview, and token-authorized display.
export function element(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'style') Object.assign(node.style, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in node && key !== 'list') node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child != null && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function authorizedURL(url, token) {
  const result = new URL(url, location.origin);
  if (result.origin !== location.origin) throw new Error('Media must be served by PrintWatch.');
  if (token) result.searchParams.set('token', token);
  return result.href;
}

function cameraPlayer(camera, token) {
  const root = element('div', {class: 'camera-player'});
  const status = element('div', {class: 'player-status'}, element('span', {class: 'status-orbit'}), element('strong', {text: camera?.name || 'Camera unavailable'}), element('small', {text: 'Connecting to live feed…'}));
  root.append(status);
  let stopped = false, timer, retryTimer, socket, peer, objectURL, sourceBuffer, generation = 0;
  let queue = [], timeout, failures = 0;
  const message = text => { status.hidden = false; status.lastChild.textContent = text; };
  const image = element('img', {class: 'camera-media', alt: camera?.name || 'Camera snapshot'});
  const video = element('video', {class: 'camera-media', autoplay: true, muted: true, playsInline: true});
  const cleanTransport = () => {
    generation++;
    clearTimeout(timeout); clearTimeout(timer); clearTimeout(retryTimer);
    if (socket) { socket.onclose = socket.onerror = socket.onmessage = socket.onopen = null; socket.close(); socket = null; }
    if (peer) { peer.ontrack = peer.onicecandidate = peer.onconnectionstatechange = null; peer.close(); peer = null; }
    video.onplaying = video.onerror = null;
    image.onload = image.onerror = null;
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = null; sourceBuffer = null; queue = [];
    video.srcObject = null; video.removeAttribute('src');
    return generation;
  };
  const active = epoch => !stopped && generation === epoch;
  const snapshot = () => {
    if (stopped) return;
    const epoch = cleanTransport();
    video.remove();
    if (!image.isConnected) root.prepend(image);
    const poll = () => {
      if (!active(epoch)) return;
      const url = new URL(authorizedURL(`/api/cameras/${camera.id}/snapshot`, token));
      url.searchParams.set('_', Date.now());
      image.src = url.href;
    };
    image.onload = () => { if (active(epoch)) { status.hidden = true; failures = 0; timer = setTimeout(poll, 1000); } };
    image.onerror = () => { if (active(epoch)) { message('Feed unavailable · retrying automatically'); failures++; timer = setTimeout(poll, Math.min(1000 * failures, 10000)); } };
    // A temporarily unavailable gateway must not strand a video camera in snapshots.
    if (!['u1', 'snapshot', 'mjpeg'].includes(camera.kind)) retryTimer = setTimeout(() => { if (active(epoch)) rtc(); }, 30000);
    poll();
  };
  const wsURL = () => authorizedURL(`/api/cameras/${camera.id}/stream`, token).replace(/^http/, 'ws');
  const mse = () => {
    if (stopped) return;
    const epoch = cleanTransport();
    const fallback = () => { if (active(epoch)) snapshot(); };
    if (!window.MediaSource) { snapshot(); return; }
    const codecs = ['avc1.640029', 'avc1.64002A', 'avc1.640033', 'avc1.4D4029', 'avc1.42E01E', 'hvc1.1.6.L153.B0', 'mp4a.40.2'];
    const supported = codecs.filter(codec => MediaSource.isTypeSupported(`video/mp4; codecs="${codec}"`));
    if (!supported.length) { snapshot(); return; }
    const mediaSource = new MediaSource();
    objectURL = URL.createObjectURL(mediaSource);
    video.src = objectURL;
    image.remove(); root.prepend(video);
    video.onplaying = () => { if (active(epoch)) { status.hidden = true; clearTimeout(timeout); } };
    video.onerror = fallback;
    const append = () => {
      if (!active(epoch) || !sourceBuffer || sourceBuffer.updating) return;
      try {
        if (sourceBuffer.buffered.length) {
          const end = sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1);
          if (video.currentTime < end - 3) video.currentTime = Math.max(0, end - 0.2);
          const before = video.currentTime - 12;
          if (before > sourceBuffer.buffered.start(0) + 5) {
            sourceBuffer.remove(sourceBuffer.buffered.start(0), before);
            return;
          }
        }
        if (queue.length) sourceBuffer.appendBuffer(queue.shift());
      } catch { fallback(); }
    };
    mediaSource.addEventListener('sourceopen', () => {
      if (!active(epoch)) return;
      const ws = socket = new WebSocket(wsURL());
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => { if (active(epoch)) ws.send(JSON.stringify({type: 'mse', value: supported.join(',')})); };
      ws.onmessage = event => {
        if (!active(epoch)) return;
        if (typeof event.data === 'string') {
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'mse') {
              sourceBuffer = mediaSource.addSourceBuffer(data.value);
              sourceBuffer.addEventListener('updateend', append);
              append();
            } else if (data.type === 'error') fallback();
          } catch { fallback(); }
        } else {
          if (queue.length > 100 || queue.reduce((bytes, part) => bytes + part.byteLength, 0) > 20 * 1024 * 1024) { fallback(); return; }
          queue.push(event.data); append();
        }
      };
      ws.onerror = ws.onclose = fallback;
    }, {once: true});
    timeout = setTimeout(fallback, 12000);
  };
  const rtc = () => {
    if (stopped) return;
    if (!window.RTCPeerConnection) { mse(); return; }
    const epoch = cleanTransport();
    const fallback = () => { if (active(epoch)) mse(); };
    image.remove(); root.prepend(video);
    video.onplaying = () => { if (active(epoch)) { status.hidden = true; clearTimeout(timeout); } };
    video.onerror = fallback;
    try {
      const connection = peer = new RTCPeerConnection({iceServers: []});
      const remoteCandidates = [], localCandidates = [];
      let sentOffer = false;
      connection.addTransceiver('video', {direction: 'recvonly'});
      connection.ontrack = event => { if (active(epoch)) { video.srcObject = event.streams[0] || new MediaStream([event.track]); video.play().catch(() => {}); } };
      const sendCandidate = candidate => ws.send(JSON.stringify({type: 'webrtc/candidate', value: candidate}));
      connection.onicecandidate = event => {
        if (!active(epoch) || !event.candidate) return;
        if (sentOffer && ws.readyState === WebSocket.OPEN) sendCandidate(event.candidate.candidate);
        else localCandidates.push(event.candidate.candidate);
      };
      connection.onconnectionstatechange = () => { if (connection.connectionState === 'failed') fallback(); };
      const ws = socket = new WebSocket(wsURL());
      ws.onopen = async () => {
        try {
          const offer = await connection.createOffer();
          await connection.setLocalDescription(offer);
          if (!active(epoch) || ws.readyState !== WebSocket.OPEN) return;
          ws.send(JSON.stringify({type: 'webrtc/offer', value: offer.sdp}));
          sentOffer = true; localCandidates.splice(0).forEach(sendCandidate);
        } catch { fallback(); }
      };
      ws.onmessage = async event => {
        if (!active(epoch)) return;
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'webrtc/answer') {
            await connection.setRemoteDescription({type: 'answer', sdp: data.value});
            for (const candidate of remoteCandidates.splice(0)) await connection.addIceCandidate(candidate);
          } else if (data.type === 'webrtc/candidate' && data.value) {
            const candidate = {candidate: data.value, sdpMid: '0'};
            if (connection.remoteDescription) await connection.addIceCandidate(candidate);
            else remoteCandidates.push(candidate);
          } else if (data.type === 'error') fallback();
        } catch { fallback(); }
      };
      ws.onerror = ws.onclose = fallback;
      timeout = setTimeout(fallback, 12000);
    } catch { fallback(); }
  };
  if (!camera || camera.enabled === false) message(camera ? 'Camera is disabled' : 'Choose a camera in the layout editor');
  else if (['u1', 'snapshot', 'mjpeg'].includes(camera.kind)) snapshot();
  else rtc();
  return {node: root, destroy() { stopped = true; clearTimeout(timer); cleanTransport(); image.onload = image.onerror = null; image.removeAttribute('src'); }};
}

function advertisementPlayer(playlist, assets, token) {
  const direction = ['left', 'right', 'up', 'down'].includes(playlist?.slide_direction) ? playlist.slide_direction : 'left';
  const root = element('div', {class: `ad-player transition-${playlist?.transition || 'fade'} slide-${direction}`});
  root.style.setProperty('--transition-duration', `${Math.max(0, Number(playlist?.transition_ms) || 0)}ms`);
  const entries = (playlist?.items || []).filter(item => item.enabled !== false).map(item => {
    const asset = assets.find(asset => asset.id === item.asset_id && asset.status === 'ready');
    const page = asset?.pages?.find(page => page.index === Number(item.page));
    return page ? {item, page, asset} : null;
  }).filter(Boolean);
  let index = 0, timer, stopped = false, previous;
  const removals = new Set();
  // Retain every decoded page node: rotation continues without network on outages.
  const cache = new Map();
  const load = entry => {
    const url = authorizedURL(entry.page.url, token);
    let record = cache.get(url);
    if (record) return record;
    const image = element('img', {class: 'ad-page', alt: `${entry.asset.name}, page ${Number(entry.item.page) + 1}`});
    record = {image, ready: false};
    record.loaded = new Promise(resolve => {
      image.onload = () => { record.ready = true; resolve(true); };
      image.onerror = () => resolve(false);
    });
    cache.set(url, record);
    image.src = url;
    return record;
  };
  entries.forEach(load);
  const show = async () => {
    if (stopped) return;
    const entry = entries[index], record = load(entry);
    const ready = await record.loaded;
    if (stopped) return;
    const {image} = record;
    if (ready && image !== previous) {
      const old = previous;
      root.querySelector('.player-status')?.remove();
      image.classList.remove('leaving'); image.classList.add('entering');
      root.append(image);
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (stopped) return;
        image.classList.remove('entering'); old?.classList.add('leaving');
      }));
      if (old) {
        const removal = setTimeout(() => {
          removals.delete(removal);
          if (old !== previous) old.remove();
        }, Math.max(0, playlist?.transition_ms || 0) + 50);
        removals.add(removal);
      }
      previous = image;
    } else if (!ready && !previous) {
      root.replaceChildren(element('div', {class: 'player-status'}, element('strong', {text: 'Media temporarily unavailable'}), element('small', {text: 'Waiting for a display-ready page…'})));
    }
    index = (index + 1) % entries.length;
    if (!ready) cache.delete(authorizedURL(entry.page.url, token));
    if (entries.length > 1 || !ready) timer = setTimeout(show, ready ? Math.max(1, Number(entry.item.duration) || 10) * 1000 : 3000);
  };
  if (entries.length) show();
  else root.append(element('div', {class: 'player-status'}, element('span', {class: 'empty-symbol', text: '▧'}), element('strong', {text: playlist?.name || 'Advertisement'}), element('small', {text: 'No ready pages in this playlist'})));
  return {node: root, destroy() {
    stopped = true; clearTimeout(timer);
    for (const timeout of removals) clearTimeout(timeout);
    for (const {image} of cache.values()) { image.onload = image.onerror = null; image.removeAttribute('src'); }
    cache.clear();
  }};
}

export class Composition {
  constructor(host, {token = '', interactive = false, onSelect, onMove, snap = 1} = {}) {
    this.host = host; this.token = token; this.interactive = interactive;
    this.onSelect = onSelect; this.onMove = onMove; this.players = new Map();
    this.snap = snap;
    this.canvas = element('div', {class: `composition${interactive ? ' interactive' : ''}`});
    host.append(this.canvas);
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(host);
  }
  resize() {
    if (!this.layout) return;
    const width = this.layout.width || 1920, height = this.layout.height || 1080;
    const scale = Math.min(this.host.clientWidth / width, this.host.clientHeight / height);
    this.scale = scale || 1;
    Object.assign(this.canvas.style, {width: `${width}px`, height: `${height}px`, transform: `translate(-50%, -50%) scale(${scale})`});
  }
  update(layout, data, selected) {
    this.layout = layout;
    this.canvas.style.background = layout.background || '#090d14';
    const ids = new Set();
    for (const tile of layout.tiles || []) {
      ids.add(tile.id);
      const source = tile.type === 'camera' ? data.cameras?.find(camera => camera.id === tile.source_id) : data.playlists?.find(playlist => playlist.id === tile.source_id);
      // Deliberately exclude transient camera status; polling must not interrupt video.
      const signature = tile.type === 'camera' ? JSON.stringify([tile.source_id, source?.kind, source?.enabled]) : JSON.stringify([source, data.assets]);
      let record = this.players.get(tile.id);
      if (!record || record.signature !== signature) {
        if (record) { record.player.destroy(); record.node.remove(); }
        const player = tile.type === 'camera' ? cameraPlayer(source, this.token) : advertisementPlayer(source, data.assets || [], this.token);
        const node = element('div', {class: 'composition-tile', tabindex: this.interactive ? 0 : -1, 'aria-label': `${tile.type} tile: ${source?.name || 'Unassigned'}`}, player.node);
        const label = element('div', {class: 'tile-label'});
        node.append(label);
        if (this.interactive) {
          node.addEventListener('pointerdown', event => this.drag(event, tile.id, false));
          node.addEventListener('focus', () => this.onSelect?.(tile.id));
          const handle = element('button', {class: 'resize-handle', title: 'Drag to resize; use numeric controls for keyboard editing', 'aria-label': 'Resize tile'});
          handle.addEventListener('pointerdown', event => { event.stopPropagation(); this.drag(event, tile.id, true); });
          node.append(handle);
        }
        this.canvas.append(node);
        record = {node, player, signature, label}; this.players.set(tile.id, record);
      }
      Object.assign(record.node.style, {left: `${tile.x}px`, top: `${tile.y}px`, width: `${tile.width}px`, height: `${tile.height}px`, zIndex: tile.z || 0, '--tile-fit': tile.fit || 'contain'});
      record.node.classList.toggle('selected', selected === tile.id);
      record.label.hidden = !tile.label;
      record.label.textContent = source?.name || 'Unassigned';
    }
    for (const [id, record] of this.players) if (!ids.has(id)) { record.player.destroy(); record.node.remove(); this.players.delete(id); }
    this.resize();
  }
  select(id) { for (const [key, record] of this.players) record.node.classList.toggle('selected', key === id); }
  drag(event, id, resize) {
    if (event.button !== 0) return;
    event.preventDefault();
    this.onSelect?.(id);
    const tile = this.layout.tiles.find(tile => tile.id === id);
    if (!tile) return;
    const start = {...tile}, x = event.clientX, y = event.clientY, target = event.currentTarget;
    target.setPointerCapture(event.pointerId);
    const move = e => {
      const grid = e.altKey ? 1 : Math.max(1, this.snap);
      const dx = (e.clientX - x) / this.scale, dy = (e.clientY - y) / this.scale;
      const snap = value => Math.round(value / grid) * grid;
      if (resize) {
        tile.width = Math.max(32, Math.min(this.layout.width - tile.x, snap(start.width + dx)));
        tile.height = Math.max(32, Math.min(this.layout.height - tile.y, snap(start.height + dy)));
      } else {
        tile.x = Math.max(0, Math.min(this.layout.width - tile.width, snap(start.x + dx)));
        tile.y = Math.max(0, Math.min(this.layout.height - tile.height, snap(start.y + dy)));
      }
      const node = this.players.get(id).node;
      Object.assign(node.style, {left: `${tile.x}px`, top: `${tile.y}px`, width: `${tile.width}px`, height: `${tile.height}px`});
      this.onMove?.(tile);
    };
    const end = () => { target.removeEventListener('pointermove', move); target.removeEventListener('pointerup', end); target.removeEventListener('pointercancel', end); };
    target.addEventListener('pointermove', move); target.addEventListener('pointerup', end); target.addEventListener('pointercancel', end);
  }
  destroy() { this.observer.disconnect(); for (const record of this.players.values()) record.player.destroy(); this.players.clear(); this.canvas.remove(); }
}

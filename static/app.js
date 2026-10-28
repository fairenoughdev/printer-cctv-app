const frontendBuild = document.querySelector('meta[name="printwatch-build"]')?.content || '';
const buildQuery = encodeURIComponent(frontendBuild);
const [{element: h, Composition, authorizedURL}, {watchForUpdates}] = await Promise.all([
  import(`./renderer.js?v=${buildQuery}`),
  import(`./updates.js?v=${buildQuery}`),
]);

const root = document.querySelector('#app');
const modal = document.querySelector('#modal');
const modalContent = document.querySelector('#modal-content');
let state = {cameras: [], assets: [], playlists: [], layouts: [], settings: {}};
let route = location.hash.slice(1) || 'overview';
let composition, pageRefresh, layoutDraft, playlistDraft, selectedTile, dirty = false, online = false;
let layoutId, playlistId, polling = false, snapGrid = 8;
let activeMutations = 0, updateReload = false;
const clone = value => JSON.parse(JSON.stringify(value));
const icons = {overview: '◫', cameras: '◉', library: '▧', playlists: '≡', layouts: '▦', displays: '▣', settings: '⚙'};
const names = {overview: 'Overview', cameras: 'Cameras', library: 'Media library', playlists: 'Playlists', layouts: 'Layout studio', displays: 'Displays', settings: 'Settings'};
const descriptions = {
  overview: 'A clear view of your workshop. All in one place.',
  cameras: 'Connect your printers. Keep every build in sight.',
  library: 'Bring your brand to the big screen.',
  playlists: 'The right content, on repeat. Build your display rotation.',
  layouts: 'Your workshop, your composition.',
  displays: 'Turn any screen into your workshop window.',
  settings: 'Make PrintWatch feel at home on your network.'
};

async function api(path, {method = 'GET', body, ...options} = {}) {
  const mutation = method !== 'GET';
  if (mutation) activeMutations++;
  try {
    const response = await fetch(path, {
      method, credentials: 'same-origin', cache: 'no-store',
      ...(body instanceof FormData ? {body} : body !== undefined ? {body: JSON.stringify(body), headers: {'Content-Type': 'application/json'}} : {}),
      ...options
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  } finally { if (mutation) activeMutations--; }
}

function hasUnsavedWork() {
  return dirty || modal.open || activeMutations > 0
    || !!document.querySelector('form[data-unsaved="true"]');
}
function refreshForUpdate() {
  if (activeMutations > 0) { toast('Wait for the current save or upload to finish.', true); return; }
  if (hasUnsavedWork() && !confirm('Reload the updated app and discard unsaved changes?')) return;
  updateReload = true;
  location.reload();
}
function showAppUpdate() {
  if (document.querySelector('#app-update-banner')) return;
  document.body.append(h('aside', {id: 'app-update-banner', class: 'app-update-banner', role: 'status'},
    h('div', {}, h('strong', {text: 'Update ready'}),
      h('p', {text: 'Your work is safe. Save your edits or close the dialog to update automatically, or refresh now.'})),
    button('Refresh now', refreshForUpdate, 'primary')));
}

// Catch typing before a change/blur event, including camera and settings forms.
document.addEventListener('input', event => {
  const target = event.target;
  if (!target.matches('input, select, textarea') || target.readOnly || target.disabled) return;
  const form = target.closest('form');
  if (form) form.dataset.unsaved = 'true';
  else if (route === 'playlists' || route === 'layouts') dirty = true;
});
function toast(message, error = false) {
  const node = h('div', {class: `toast${error ? ' error' : ''}`, text: message});
  document.querySelector('#toasts').append(node);
  setTimeout(() => node.remove(), error ? 8000 : 4200);
}
function button(text, action, kind = '', props = {}) {
  return h('button', {type: 'button', class: `button ${kind}`, onclick: async event => {
    const node = event.currentTarget;
    if (node.disabled) return;
    node.disabled = true;
    try { await action?.(event); } catch (error) { toast(error.message, true); }
    finally { node.disabled = false; }
  }, ...props}, text);
}
function badge(text, kind = '') { return h('span', {class: `badge ${kind}`, text}); }
function field(label, input, hint) {
  const id = input.id || `field-${tileID()}`;
  input.id = id;
  return h('div', {class: 'field'}, h('label', {htmlFor: id, text: label}), input, hint ? h('small', {text: hint}) : null);
}
function input(value = '', props = {}) { return h('input', {value, ...props}); }
function secondsInput(value, min, max, commit) {
  const node = input(value, {
    type: 'text', inputMode: 'decimal', class: 'playlist-seconds', required: true,
    autocomplete: 'off', spellcheck: false,
  });
  const validate = () => {
    const text = node.value.trim().replace(',', '.');
    const number = Number(text);
    const valid = /^(?:\d+(?:\.\d*)?|\.\d+)$/.test(text)
      && Number.isFinite(number) && number >= min && number <= max;
    node.setCustomValidity(valid ? '' : `Enter seconds between ${min} and ${max}.`);
    node.setAttribute('aria-invalid', String(!valid));
    if (valid) commit(number);
  };
  // Do not rewrite the input while typing: clearing, pasting, and decimals stay natural.
  node.addEventListener('input', validate);
  node.addEventListener('change', validate);
  return node;
}
function select(options, value, onchange) {
  const node = h('select', {onchange}, options.map(option => h('option', {value: typeof option === 'string' ? option : option[0], text: typeof option === 'string' ? option : option[1]})));
  node.value = value ?? '';
  return node;
}
function toggle(label, checked, change) {
  const node = input('', {type: 'checkbox', checked, onchange: event => change?.(event.target.checked)});
  return h('label', {class: 'check'}, node, h('span', {text: label}));
}
function empty(title, description, action) {
  return h('div', {class: 'empty-state'}, h('span', {class: 'empty-symbol', text: '◫'}), h('h3', {text: title}), h('p', {text: description}), action);
}
function section(title, subtitle, ...children) {
  return h('section', {class: 'panel'}, h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: title}), subtitle ? h('p', {text: subtitle}) : null)), ...children);
}
function closeModal() { modal.close(); modalContent.replaceChildren(); }
function openModal(title, description, content) {
  modalContent.replaceChildren(h('div', {class: 'modal-heading'}, h('div', {}, h('h2', {text: title}), h('p', {text: description})), button('×', closeModal, 'icon', {'aria-label': 'Close dialog'})), content);
  modal.showModal();
}
function confirmDelete(title, action) {
  openModal(`Delete ${title}?`, 'This cannot be undone. Layouts or playlists using this item may need updating.',
    h('div', {class: 'actions end'}, button('Cancel', closeModal, 'secondary'), button('Delete permanently', async () => { await action(); closeModal(); }, 'danger')));
}
async function refresh(render = true) {
  const next = await api('/api/state');
  const changed = JSON.stringify(next) !== JSON.stringify(state);
  state = next; online = true; updateConnection();
  if (render && changed) pageRefresh?.();
}
function updateConnection() {
  const node = document.querySelector('#connection');
  if (node) { node.textContent = online ? 'System connected' : 'Connection interrupted'; node.className = `connection ${online ? 'connected' : ''}`; }
}
function navigate(next) {
  if (next === route && document.querySelector('.workspace')) return;
  if (dirty && !confirm('You have unsaved changes. Leave this editor?')) { history.replaceState(null, '', `#${route}`); return; }
  dirty = false; layoutDraft = null; playlistDraft = null;
  route = names[next] ? next : 'overview'; location.hash = route; renderShell();
}
function renderShell() {
  composition?.destroy(); composition = null; pageRefresh = null;
  document.title = `${names[route]} · PrintWatch`;
  const nav = h('nav', {'aria-label': 'Main menu'}, h('div', {class: 'nav-label', text: 'WORKSPACE'}),
    Object.keys(names).map(key => button([h('span', {class: 'nav-icon', text: icons[key]}), h('span', {text: names[key]})], () => navigate(key), `nav-item${route === key ? ' active' : ''}`, {'aria-current': route === key ? 'page' : null, 'aria-label': names[key], title: names[key]})));
  const sidebar = h('aside', {class: 'sidebar'},
    h('a', {class: 'brand', href: '#overview'}, h('span', {class: 'brand-mark', text: 'P'}), h('span', {}, 'Print', h('b', {text: 'Watch'}))),
    h('div', {class: 'workspace-label'}, h('span', {class: 'tiny-dot'}), h('span', {text: state.settings.shop_name || 'Your workshop'})),
    nav,
    h('div', {class: 'sidebar-footer'}, h('div', {class: 'local-pill', text: 'LOCAL FIRST · ALWAYS YOURS'}), h('p', {text: 'Your printers. Your network.'}), h('span', {id: 'connection', class: 'connection'})));
  const workspace = h('main', {class: 'workspace'},
    h('div', {class: 'topbar'}, h('span', {text: 'WORKSHOP CONTROL'}), h('span', {class: 'topbar-right'}, h('span', {class: 'tiny-dot'}), 'Private network')),
    h('header', {class: 'page-header'}, h('div', {}, h('div', {class: 'eyebrow', text: 'PRINTWATCH / ' + names[route].toUpperCase()}), h('h1', {text: names[route]}), h('p', {text: descriptions[route]})), h('div', {id: 'page-actions', class: 'actions'})),
    h('div', {id: 'page-content'}));
  root.replaceChildren(sidebar, workspace); updateConnection();
  ({overview: renderOverview, cameras: renderCameras, library: renderLibrary, playlists: renderPlaylists, layouts: renderLayouts, displays: renderDisplays, settings: renderSettings})[route]();
}
function content(...children) { document.querySelector('#page-content').replaceChildren(...children); }
function actions(...children) { document.querySelector('#page-actions').replaceChildren(...children); }
function goButton(text, next, kind = 'primary') { return button(text, () => navigate(next), kind); }

function renderOverview() {
  actions(goButton('Open layout studio ↗', 'layouts'));
  const cameraCount = state.cameras.filter(camera => camera.enabled).length;
  const stats = h('div', {class: 'stats'},
    stat('Connected cameras', cameraCount, `${state.cameras.length} configured`, '◉'),
    stat('Media assets', state.assets.filter(asset => asset.status === 'ready').length, 'Ready for your displays', '▧'),
    stat('Published layouts', state.layouts.filter(layout => layout.published).length, 'Designed for the big screen', '▦'),
    stat('Playlists', state.playlists.length, 'Your content in motion', '≡'));
  const preview = h('div', {class: 'preview-host overview-preview'});
  const layout = state.layouts.find(layout => layout.published?.tiles?.length) || state.layouts[0];
  const selected = layout?.published || layout;
  const header = h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: 'Your workshop, live'}), h('p', {text: layout?.name || 'Create your first layout'})), badge(selected?.tiles?.length ? 'LIVE PREVIEW' : 'READY WHEN YOU ARE', selected?.tiles?.length ? 'success' : ''));
  const previewPanel = h('section', {class: 'panel preview-panel'}, header);
  if (selected?.tiles?.length) {
    previewPanel.append(preview);
    composition = new Composition(preview); composition.update(selected, state);
  } else previewPanel.append(empty('Let’s bring your workshop into view', 'Connect a printer camera, add your media, and create a display that works for you.', goButton('+ Connect your first camera', 'cameras')));
  const guide = section('A few steps. A better view.', 'Build your workshop’s command center.',
    h('div', {class: 'setup-steps'},
      step('01', 'Connect your cameras', 'Bambu, U1, RTSP and more.', 'cameras', !!state.cameras.length),
      step('02', 'Add something worth sharing', 'Upload images or multipage PDFs.', 'library', !!state.assets.length),
      step('03', 'Make it yours', 'Arrange, publish, and open a display.', 'layouts', state.layouts.some(layout => layout.published))));
  content(stats, previewPanel, guide);
  pageRefresh = () => {
    // Preserve existing video connections when the published composition is unchanged.
    if (composition && selected?.tiles?.length) {
      const fresh = state.layouts.find(item => item.id === layout.id);
      if (fresh) composition.update(fresh.published || fresh, state);
    } else { composition?.destroy(); composition = null; renderOverview(); }
  };
}
function stat(label, value, note, icon) {
  return h('div', {class: 'stat-card'}, h('div', {class: 'stat-label'}, label, h('span', {text: icon})), h('strong', {text: value}), h('small', {text: note}));
}
function step(number, title, description, next, done) {
  return h('button', {class: 'setup-step', onclick: () => navigate(next)}, h('span', {class: `step-number${done ? ' complete' : ''}`, text: done ? '✓' : number}), h('span', {}, h('strong', {text: title}), h('small', {text: description})), h('span', {class: 'step-arrow', text: '↗'}));
}

function renderCameras() {
  actions(button('+ Add camera', () => cameraForm(), 'primary'));
  const cards = state.cameras.map(camera => {
    const status = camera.enabled === false ? 'disabled' : camera.status || 'unknown';
    const live = ['online', 'connected', 'ready'].includes(status);
    return h('article', {class: 'camera-card panel'},
      h('div', {class: 'camera-card-top'}, h('span', {class: 'camera-glyph', text: '◉'}), badge(status, live ? 'success' : status === 'error' || status === 'offline' ? 'warning' : '')),
      h('h2', {text: camera.name}), h('p', {text: `${camera.kind.toUpperCase()}${camera.model ? ` · ${camera.model}` : ''}`}),
      h('div', {class: 'camera-host', text: camera.host || 'Custom camera endpoint'}),
      camera.error ? h('p', {class: 'inline-error', text: camera.error}) : null,
      h('div', {class: 'card-footer'}, button('Edit camera', () => cameraForm(camera), 'secondary'), button('Test connection', () => testCamera(camera), 'ghost'), button('×', () => confirmDelete(camera.name, async () => { await api(`/api/cameras/${camera.id}`, {method: 'DELETE'}); await refresh(false); renderCameras(); }), 'icon', {'aria-label': `Delete ${camera.name}`})));
  });
  content(h('div', {class: 'notice'}, h('span', {text: '◈'}), h('span', {text: 'Connections stay on your network. Camera credentials are stored on the server, never on a display.'})),
    cards.length ? h('div', {class: 'camera-grid'}, cards) : section('Your cameras', '', empty('Every great view starts with a camera', 'Connect a Bambu printer, Snapmaker U1, or any compatible network camera.', button('+ Add camera', () => cameraForm(), 'primary'))));
  pageRefresh = renderCameras;
}
async function testCamera(camera) {
  toast(`Testing ${camera.name}…`);
  const result = await api(`/api/cameras/${camera.id}/test`, {method: 'POST'});
  toast(result.message || (result.ok ? 'Camera connected' : 'Connection failed'), !result.ok);
  await refresh();
}
function cameraForm(camera) {
  const name = input(camera?.name || '', {required: true, maxlength: 100, placeholder: 'e.g. Workshop H2D'});
  const kind = select([['bambu', 'Bambu Lab printer'], ['u1', 'Snapmaker U1'], ['rtsp', 'RTSP camera'], ['mjpeg', 'MJPEG camera'], ['snapshot', 'Snapshot camera']], camera?.kind || 'bambu', updateHints);
  const model = input(camera?.model || '', {placeholder: 'e.g. H2D, H2S, P2S, U1'});
  const host = input(camera?.host || '', {placeholder: '192.168.1.42', autocomplete: 'off'});
  const url = input(camera?.url || '', {placeholder: 'rtsp://192.168.1.42:554/stream', autocomplete: 'off'});
  const username = input(camera?.username || '', {autocomplete: 'off', placeholder: 'Optional'});
  const password = input('', {type: 'password', autocomplete: 'new-password', placeholder: camera?.has_password ? 'Saved · leave blank to keep' : 'Access code or password'});
  const passwordField = field('Password / access code', password);
  const verify = input('', {type: 'checkbox', checked: camera?.verify_tls ?? true});
  const enabled = input('', {type: 'checkbox', checked: camera?.enabled ?? true});
  const help = h('p', {class: 'notice compact'});
  function updateHints() {
    const isBambu = kind.value === 'bambu', isU1 = kind.value === 'u1';
    host.required = isBambu || isU1;
    url.required = !isBambu && !isU1;
    url.placeholder = kind.value === 'rtsp' || isBambu ? 'rtsp://192.168.1.42:554/stream' : 'http://192.168.1.42/camera/snapshot';
    passwordField.querySelector('label').textContent = isU1 ? 'Fluidd websocket token' : isBambu ? 'LAN access code' : 'Camera password';
    help.textContent = isBambu ? 'For H2D, H2S and P2S: enable LAN live view on your printer and enter its LAN access code. LAN live view is independent of LAN-only mode. A local video gateway is required; older proprietary Bambu camera protocols are not supported.' : isU1 ? 'Open your printer’s local Fluidd interface and copy the token used by its /websocket connection into the token field. The server uses it to keep the U1 camera awake. Images refresh about once per second.' : 'Use a camera URL without credentials. Enter the username and password separately.';
  }
  const form = h('form', {onsubmit: async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    try {
      if (url.value) { const parsed = new URL(url.value); if (parsed.username || parsed.password) throw new Error('Remove credentials from the camera URL and use the separate credential fields.'); }
      const data = {name: name.value.trim(), kind: kind.value, model: model.value.trim(), host: host.value.trim(), url: url.value.trim(), username: username.value, verify_tls: verify.checked, enabled: enabled.checked};
      if (password.value || !camera) data.password = password.value;
      const submit = form.querySelector('[type="submit"]'); submit.disabled = true;
      try {
        await api(`/api/cameras${camera ? `/${camera.id}` : ''}`, {method: camera ? 'PUT' : 'POST', body: data});
        closeModal(); toast(camera ? 'Camera updated' : 'Camera added'); await refresh(false); renderCameras();
      } finally { submit.disabled = false; }
    } catch (error) { toast(error.message, true); }
  }},
  h('div', {class: 'form-grid'}, field('Camera name', name), field('Camera type', kind), field('Printer model', model), field('Host / IP address', host), h('div', {class: 'span-2'}, field('Camera URL', url, 'No username or password in this URL. Leave blank for printer defaults.')), field('Username', username), passwordField),
  help, h('div', {class: 'actions'}, h('label', {class: 'check'}, verify, 'Verify TLS certificate'), h('label', {class: 'check'}, enabled, 'Camera enabled')),
  h('div', {class: 'modal-footer'}, button('Cancel', closeModal, 'secondary'), h('button', {type: 'submit', class: 'button primary', text: camera ? 'Save camera' : 'Add camera'})));
  updateHints(); openModal(camera ? 'Edit camera' : 'Connect a camera', 'One more window into your workshop.', form);
}

function renderLibrary() {
  const fileInput = input('', {type: 'file', accept: '.png,.jpg,.jpeg,.webp,.pdf', multiple: true, class: 'visually-hidden', onchange: event => uploadFiles(event.target.files)});
  actions(button('↑ Upload media', () => fileInput.click(), 'primary'));
  const drop = h('div', {class: 'upload-zone', tabindex: 0, role: 'button', 'aria-label': 'Upload media files', onclick: () => fileInput.click(), onkeydown: event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); fileInput.click(); } }, ondragover: event => { event.preventDefault(); drop.classList.add('dragover'); }, ondragleave: () => drop.classList.remove('dragover'), ondrop: event => { event.preventDefault(); drop.classList.remove('dragover'); uploadFiles(event.dataTransfer.files); }},
    h('span', {class: 'upload-icon', text: '↑'}), h('strong', {text: 'Drop your next big idea here'}), h('p', {text: 'JPEG, PNG, WebP, and PDF · or click to browse'}), h('small', {text: 'PDFs are converted into display-ready pages on your server.'}));
  const cards = state.assets.map(asset => {
    const page = asset.pages?.[0];
    return h('article', {class: 'asset-card panel'},
      h('div', {class: 'asset-preview'}, page ? h('img', {src: authorizedURL(page.url), alt: asset.name, loading: 'lazy'}) : h('span', {class: asset.status === 'processing' ? 'spinner' : 'empty-symbol', text: asset.status === 'processing' ? '' : '▧'})),
      h('div', {class: 'asset-info'}, h('div', {class: 'spread'}, h('h3', {text: asset.name}), badge(asset.status, asset.status === 'ready' ? 'success' : asset.status === 'error' ? 'warning' : '')),
        h('p', {text: asset.error || (asset.status === 'processing' ? 'Preparing pages…' : `${asset.pages?.length || 0} display-ready page${asset.pages?.length === 1 ? '' : 's'}`)}),
        h('div', {class: 'actions'}, button('View pages', () => assetPages(asset), 'secondary', {disabled: !page}), button('Delete', () => confirmDelete(asset.name, async () => { await api(`/api/assets/${asset.id}`, {method: 'DELETE'}); await refresh(false); renderLibrary(); }), 'ghost'))));
  });
  content(fileInput, drop, h('div', {class: 'section-heading'}, h('h2', {text: 'Your media'}), h('span', {class: 'muted', text: `${state.assets.length} assets`})), cards.length ? h('div', {class: 'asset-grid'}, cards) : empty('A blank canvas for your brand', 'Upload a menu, a promotion, a project gallery, or something that makes your workshop yours.'));
  pageRefresh = renderLibrary;
}
async function uploadFiles(files) {
  for (const file of Array.from(files)) {
    const data = new FormData(); data.append('file', file);
    try { toast(`Uploading ${file.name}…`); await api('/api/assets', {method: 'POST', body: data}); toast(`${file.name} uploaded. Preparing pages…`); }
    catch (error) { toast(`${file.name}: ${error.message}`, true); }
  }
  try { await refresh(false); if (route === 'library') renderLibrary(); }
  catch (error) { toast(error.message, true); }
}
function assetPages(asset) {
  openModal(asset.name, `${asset.pages.length} pages · select individual pages in the playlist editor.`,
    h('div', {class: 'page-gallery'}, asset.pages.map(page => h('figure', {}, h('img', {src: authorizedURL(page.url), alt: `Page ${page.index + 1}`, loading: 'lazy'}), h('figcaption', {text: `Page ${page.index + 1} · ${page.width} × ${page.height}`})))));
}

function renderPlaylists() {
  if (!playlistDraft || !state.playlists.some(item => item.id === playlistDraft.id)) {
    const original = state.playlists.find(item => item.id === playlistId) || state.playlists[0];
    playlistDraft = original ? clone(original) : null; playlistId = original?.id;
  }
  actions(button('+ New playlist', newPlaylist, 'secondary'), button('Save playlist', savePlaylist, 'primary', {disabled: !playlistDraft}));
  if (!playlistDraft) { content(empty('Give your content a rhythm', 'Create a playlist to rotate your media automatically.', button('+ New playlist', newPlaylist, 'primary'))); return; }
  const draft = playlistDraft;
  const picker = select(state.playlists.map(item => [item.id, item.name]), draft.id, event => {
    if (dirty && !confirm('Discard unsaved playlist changes?')) { event.target.value = draft.id; return; }
    dirty = false; playlistDraft = null; playlistId = event.target.value; renderPlaylists();
  });
  const playlistName = input(draft.name, {required: true, oninput: event => { draft.name = event.target.value; dirty = true; }});
  const transition = select([['fade', 'Crossfade'], ['slide', 'Slide'], ['cut', 'Cut · instant']], draft.transition, event => {
    draft.transition = event.target.value; dirty = true; updateTransitionControls();
  });
  // Keep the existing millisecond storage format; people edit all timings in seconds.
  const duration = secondsInput((draft.transition_ms ?? 600) / 1000, 0, 3, seconds => {
    draft.transition_ms = Math.round(seconds * 1000); dirty = true;
  });
  const direction = select([['left', '← Left'], ['right', '→ Right'], ['up', '↑ Up'], ['down', '↓ Down']], draft.slide_direction || 'left', event => {
    draft.slide_direction = event.target.value; dirty = true;
  });
  const directionField = field('Slide direction', direction, 'Direction the pages move. The next page enters from the opposite edge.');
  const animationField = field('Animation length (seconds)', duration, 'How long the fade or slide takes, not how long a page stays on screen.');
  function updateTransitionControls() {
    directionField.hidden = draft.transition !== 'slide';
    duration.disabled = draft.transition === 'cut';
    animationField.hidden = draft.transition === 'cut';
  }
  updateTransitionControls();
  const metadata = section('Playlist settings', 'Choose the effect here. Set the seconds until the next transition for each page below.',
    h('div', {class: 'form-grid playlist-meta'}, field('Playlist', picker), field('Playlist name', playlistName), field('Transition', transition), animationField, directionField));
  const items = h('div', {class: 'playlist-items'});
  draft.items.forEach((item, index) => {
    const asset = state.assets.find(asset => asset.id === item.asset_id);
    const pagePicker = select((asset?.pages || []).map(page => [page.index, `Page ${page.index + 1}`]), item.page, event => { item.page = Number(event.target.value); dirty = true; renderPlaylists(); });
    const thumbnail = asset?.pages?.find(page => page.index === item.page);
    items.append(h('div', {class: `playlist-row${item.enabled === false ? ' disabled' : ''}`, ondragover: event => {
      if (event.dataTransfer.types.includes('application/x-printwatch-playlist-index')) event.preventDefault();
    }, ondrop: event => {
      const dragged = event.dataTransfer.getData('application/x-printwatch-playlist-index');
      if (!dragged) return;
      event.preventDefault(); const from = Number(dragged);
      if (!Number.isInteger(from) || from < 0 || from >= draft.items.length) return;
      const [moved] = draft.items.splice(from, 1); draft.items.splice(index, 0, moved); dirty = true; renderPlaylists();
    }},
    h('span', {class: 'drag-grip', title: 'Drag to reorder', draggable: true, text: '⠿', ondragstart: event => event.dataTransfer.setData('application/x-printwatch-playlist-index', String(index))}),
    thumbnail ? h('img', {class: 'playlist-thumb', src: authorizedURL(thumbnail.url), alt: ''}) : h('div', {class: 'playlist-thumb missing', text: '▧'}),
    h('div', {class: 'playlist-item-title'}, h('strong', {text: asset?.name || 'Missing asset'}), h('small', {text: `Item ${index + 1}${asset?.status !== 'ready' ? ' · Media not ready' : ''}`})),
    field('Page', pagePicker),
    field('Show for (seconds)', secondsInput(item.duration, 1, 3600, seconds => {
      item.duration = seconds; dirty = true;
    })),
    toggle('Enabled', item.enabled !== false, checked => { item.enabled = checked; dirty = true; }),
    h('div', {class: 'reorder-buttons'}, button('↑', () => moveItem(index, -1), 'icon', {disabled: index === 0, 'aria-label': 'Move item up'}), button('↓', () => moveItem(index, 1), 'icon', {disabled: index === draft.items.length - 1, 'aria-label': 'Move item down'})),
    button('×', () => { draft.items.splice(index, 1); dirty = true; renderPlaylists(); }, 'icon', {'aria-label': 'Remove playlist item'})));
  });
  const available = state.assets.filter(asset => asset.status === 'ready' && asset.pages?.length);
  const addSelect = select([['', 'Choose an asset…'], ...available.map(asset => [asset.id, `${asset.name} (${asset.pages.length} pages)`])], '');
  const addButton = button('+ Add page', () => {
    if (!addSelect.value) throw new Error('Choose a ready asset first.');
    draft.items.push({asset_id: addSelect.value, page: 0, duration: 10, enabled: true}); dirty = true; renderPlaylists();
  }, 'secondary');
  const allButton = button('Add all pages', () => {
    const asset = available.find(asset => asset.id === addSelect.value);
    if (!asset) throw new Error('Choose a ready asset first.');
    draft.items.push(...asset.pages.map(page => ({asset_id: asset.id, page: page.index, duration: 10, enabled: true}))); dirty = true; renderPlaylists();
  }, 'ghost');
  const total = draft.items.filter(item => item.enabled !== false).reduce((sum, item) => sum + Number(item.duration), 0);
  content(metadata, section('The rotation', `${draft.items.length} items · ${total}s total · “Show for” is the seconds until the next transition starts. Drag to reorder, or use the arrows.`,
    draft.items.length ? items : empty('Good things are worth repeating', 'Add a media page below to start your rotation.'),
    h('div', {class: 'playlist-add'}, addSelect, addButton, allButton)),
    h('div', {class: 'actions end'}, button('Delete playlist', () => confirmDelete(draft.name, async () => { await api(`/api/playlists/${draft.id}`, {method: 'DELETE'}); dirty = false; playlistDraft = null; await refresh(false); renderPlaylists(); }), 'danger-quiet')));
  pageRefresh = () => { /* Draft and focused controls intentionally survive background state updates. */ };
}
function moveItem(index, offset) {
  const items = playlistDraft.items; [items[index], items[index + offset]] = [items[index + offset], items[index]]; dirty = true; renderPlaylists();
}
async function newPlaylist() {
  if (dirty && !confirm('Discard unsaved changes?')) return;
  const created = await api('/api/playlists', {method: 'POST', body: {name: 'New playlist', transition: 'fade', transition_ms: 600, slide_direction: 'left', items: []}});
  await refresh(false); playlistDraft = clone(created); playlistId = created.id; dirty = false; renderPlaylists();
}
async function savePlaylist() {
  for (const control of document.querySelectorAll('.playlist-seconds:not(:disabled)')) {
    if (!control.reportValidity()) throw new Error(control.validationMessage);
  }
  if (!playlistDraft.name.trim()) throw new Error('Give this playlist a name.');
  const {name, transition, transition_ms, items} = playlistDraft;
  const slide_direction = playlistDraft.slide_direction || 'left';
  playlistDraft = await api(`/api/playlists/${playlistDraft.id}`, {method: 'PUT', body: {name, transition, transition_ms, slide_direction, items}});
  dirty = false; await refresh(false); toast('Playlist saved'); renderPlaylists();
}

function renderLayouts() {
  composition?.destroy(); composition = null;
  if (!layoutDraft || !state.layouts.some(item => item.id === layoutDraft.id)) {
    const original = state.layouts.find(item => item.id === layoutId) || state.layouts[0];
    layoutDraft = original ? clone(original) : null; layoutId = original?.id; selectedTile = null;
  }
  actions(button('+ New layout', newLayout, 'ghost'), button('Save draft', () => saveLayout(false), 'secondary', {disabled: !layoutDraft}), button('Publish layout ↗', () => saveLayout(true), 'primary', {disabled: !layoutDraft}));
  if (!layoutDraft) { content(empty('Design your workshop window', 'Create a layout to arrange cameras and advertisements on a screen.', button('+ New layout', newLayout, 'primary'))); return; }
  const draft = layoutDraft;
  const picker = select(state.layouts.map(item => [item.id, item.name]), draft.id, event => {
    if (dirty && !confirm('Discard unsaved layout changes?')) { event.target.value = draft.id; return; }
    layoutDraft = null; layoutId = event.target.value; dirty = false; renderLayouts();
  });
  const preset = select([['', 'Apply a preset…'], ['grid', 'Camera grid'], ['showcase', 'Cameras + advertisement'], ['hero', 'Single camera spotlight']], '', event => {
    if (event.target.value && (!draft.tiles.length || confirm('Replace the current tiles with this preset?'))) applyPreset(event.target.value);
  });
  const gridSelect = select([[1, 'Snap: off'], [8, 'Snap: 8px'], [16, 'Snap: 16px'], [32, 'Snap: 32px']], snapGrid, event => { snapGrid = Number(event.target.value); composition.snap = snapGrid; });
  gridSelect.setAttribute('aria-label', 'Snap to grid');
  picker.setAttribute('aria-label', 'Select layout'); preset.setAttribute('aria-label', 'Layout preset');
  const toolbar = h('div', {class: 'editor-toolbar'}, picker, badge(draft.published ? 'PUBLISHED + DRAFT' : 'DRAFT', draft.published ? 'success' : ''), h('span', {class: 'toolbar-spacer'}), preset, gridSelect, button('+ Camera', () => addTile('camera'), 'secondary'), button('+ Ad zone', () => addTile('advertisement'), 'secondary'));
  const host = h('div', {class: 'preview-host editor-preview'});
  const canvasNote = h('div', {class: 'canvas-note'}, h('span', {text: `${draft.width} × ${draft.height} · ${draft.tiles.length} tiles`}), h('span', {id: 'draft-status', text: dirty ? '● Unsaved changes' : 'Draft saved'}));
  const previewArea = h('div', {class: 'editor-stage'}, host, canvasNote,
    h('p', {class: 'editor-help', text: 'Drag to position. Drag the corner to resize. Hold Alt to bypass grid snapping. Use the inspector for precise keyboard editing, alignment, and duplication.'}));
  const inspector = h('aside', {class: 'inspector panel', id: 'inspector'});
  content(toolbar, h('div', {class: 'editor-grid'}, previewArea, inspector));
  composition = new Composition(host, {interactive: true, snap: snapGrid, onSelect: id => { if (selectedTile !== id) { selectedTile = id; composition.select(id); renderInspector(); } }, onMove: tile => {
    markDirty(); for (const key of ['x', 'y', 'width', 'height']) { const node = document.querySelector(`#tile-${key}`); if (node) node.value = tile[key]; }
  }});
  composition.update(draft, state, selectedTile);
  if (!draft.tiles.length) host.append(h('div', {class: 'canvas-empty'}, h('span', {class: 'empty-symbol', text: '▦'}), h('strong', {text: 'Room for your next great view'}), h('small', {text: 'Add a camera or choose a preset to begin.'})));
  renderInspector();
  pageRefresh = () => composition?.update(layoutDraft, state, selectedTile);
}
function markDirty() { dirty = true; const status = document.querySelector('#draft-status'); if (status) status.textContent = '● Unsaved changes'; }
function renderInspector() {
  const host = document.querySelector('#inspector'); if (!host) return;
  const draft = layoutDraft;
  const tile = draft.tiles.find(tile => tile.id === selectedTile);
  const name = input(draft.name, {oninput: event => { draft.name = event.target.value; markDirty(); }});
  const resolution = select([['1920x1080', '1920 × 1080 · Full HD'], ['3840x2160', '3840 × 2160 · 4K'], ['1080x1920', '1080 × 1920 · Portrait'], ['1280x720', '1280 × 720 · HD']], `${draft.width}x${draft.height}`, event => {
    const [width, height] = event.target.value.split('x').map(Number);
    const sx = width / draft.width, sy = height / draft.height;
    draft.tiles.forEach(tile => { tile.x = Math.round(tile.x * sx); tile.y = Math.round(tile.y * sy); tile.width = Math.round(tile.width * sx); tile.height = Math.round(tile.height * sy); });
    draft.width = width; draft.height = height; markDirty(); renderLayouts();
  });
  host.replaceChildren(h('div', {class: 'inspector-heading'}, h('h2', {text: 'Canvas settings'}), h('span', {text: '▦'})),
    field('Layout name', name), field('Screen resolution', resolution),
    field('Background', input(draft.background, {type: 'color', oninput: event => { draft.background = event.target.value; markDirty(); composition.update(draft, state, selectedTile); }})),
    h('div', {class: 'inspector-divider'}), h('h2', {text: tile ? 'Selected tile' : 'Tile inspector'}));
  if (!tile) host.append(h('p', {class: 'muted', text: 'Select a tile on the canvas to adjust its source, position, and size.'}));
  else {
    const sources = tile.type === 'camera' ? state.cameras : state.playlists;
    host.append(field(tile.type === 'camera' ? 'Camera source' : 'Playlist source', select([['', 'Select a source…'], ...sources.map(source => [source.id, source.name])], tile.source_id, event => { tile.source_id = event.target.value; markDirty(); composition.update(draft, state, selectedTile); })),
      h('div', {class: 'form-grid compact-grid'}, ['x', 'y', 'width', 'height', 'z'].map(key => field(({x: 'X position', y: 'Y position', width: 'Width', height: 'Height', z: 'Layer'})[key], input(tile[key] || 0, {id: `tile-${key}`, type: 'number', min: key === 'width' || key === 'height' ? 32 : 0, max: key === 'z' ? 100 : ['x', 'width'].includes(key) ? draft.width : draft.height, onchange: event => {
        tile[key] = Math.max(key === 'width' || key === 'height' ? 32 : 0, Math.round(Number(event.target.value) || 0));
        tile.z = Math.min(100, tile.z);
        tile.width = Math.min(tile.width, draft.width); tile.height = Math.min(tile.height, draft.height);
        tile.x = Math.min(tile.x, draft.width - tile.width); tile.y = Math.min(tile.y, draft.height - tile.height);
        markDirty(); composition.update(draft, state, selectedTile); renderInspector();
      }})))),
      field('Image fit', select([['contain', 'Contain · show entire image'], ['cover', 'Cover · fill tile']], tile.fit, event => { tile.fit = event.target.value; markDirty(); composition.update(draft, state, selectedTile); })),
      toggle('Show source label', tile.label, checked => { tile.label = checked; markDirty(); composition.update(draft, state, selectedTile); }),
      h('div', {class: 'alignment-tools'}, h('span', {class: 'muted', text: 'Align to canvas'}),
        h('div', {class: 'actions'}, [['Left', 'left'], ['Center', 'center'], ['Right', 'right'], ['Top', 'top'], ['Middle', 'middle'], ['Bottom', 'bottom']].map(([title, alignment]) => button(title, () => alignTile(alignment), 'secondary')))),
      button('Duplicate tile', () => {
        if (draft.tiles.length >= 64) throw new Error('A layout supports up to 64 tiles.');
        const copy = {...tile, id: tileID(), x: Math.min(tile.x + 24, draft.width - tile.width), y: Math.min(tile.y + 24, draft.height - tile.height), z: Math.min(100, tile.z + 1)};
        draft.tiles.push(copy); selectedTile = copy.id; markDirty(); renderLayouts();
      }, 'secondary'),
      button('Remove tile', () => { draft.tiles = draft.tiles.filter(item => item.id !== tile.id); selectedTile = null; markDirty(); renderLayouts(); }, 'danger-quiet'));
  }
  host.append(h('div', {class: 'inspector-divider'}), button('Delete layout', () => confirmDelete(draft.name, async () => { await api(`/api/layouts/${draft.id}`, {method: 'DELETE'}); dirty = false; layoutDraft = null; await refresh(false); renderLayouts(); }), 'danger-quiet'));
}
function tileID() { return crypto.randomUUID?.() || `tile-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
function alignTile(alignment) {
  const tile = layoutDraft.tiles.find(tile => tile.id === selectedTile); if (!tile) return;
  if (alignment === 'left') tile.x = 0;
  else if (alignment === 'center') tile.x = Math.round((layoutDraft.width - tile.width) / 2);
  else if (alignment === 'right') tile.x = layoutDraft.width - tile.width;
  else if (alignment === 'top') tile.y = 0;
  else if (alignment === 'middle') tile.y = Math.round((layoutDraft.height - tile.height) / 2);
  else if (alignment === 'bottom') tile.y = layoutDraft.height - tile.height;
  markDirty(); composition.update(layoutDraft, state, selectedTile); renderInspector();
}
function addTile(type) {
  const sources = type === 'camera' ? state.cameras : state.playlists;
  if (!sources.length) { toast(type === 'camera' ? 'Connect a camera first.' : 'Create a playlist first.', true); return; }
  if (layoutDraft.tiles.length >= 64) { toast('A layout supports up to 64 tiles.', true); return; }
  const draft = layoutDraft, tile = {id: tileID(), type, source_id: sources[0].id, x: 40, y: 40, width: Math.round(draft.width / 2), height: Math.round(draft.height / 2), fit: 'contain', label: type === 'camera', z: draft.tiles.length};
  draft.tiles.push(tile); selectedTile = tile.id; markDirty(); renderLayouts();
}
function applyPreset(kind) {
  const draft = layoutDraft, cameras = state.cameras.filter(camera => camera.enabled !== false), playlist = state.playlists[0];
  if (!cameras.length && !playlist) { toast('Add a camera or playlist before applying a preset.', true); return; }
  const gap = 12, w = draft.width, ht = draft.height, ad = kind === 'showcase' && playlist;
  const cameraWidth = ad ? Math.round(w * 0.73) : w;
  const list = kind === 'hero' ? cameras.slice(0, 1) : cameras.slice(0, ad ? 63 : 64);
  const cols = Math.max(1, Math.ceil(Math.sqrt(list.length * (cameraWidth / ht) / (16 / 9)))), rows = Math.max(1, Math.ceil(list.length / cols));
  draft.tiles = list.map((camera, index) => ({
    id: tileID(), type: 'camera', source_id: camera.id, x: gap + (index % cols) * (cameraWidth - gap) / cols,
    y: gap + Math.floor(index / cols) * (ht - gap) / rows,
    width: Math.round((cameraWidth - gap) / cols - gap), height: Math.round((ht - gap) / rows - gap), fit: 'contain', label: true, z: index
  }));
  if (ad) draft.tiles.push({id: tileID(), type: 'advertisement', source_id: playlist.id, x: cameraWidth + gap, y: gap, width: w - cameraWidth - gap * 2, height: ht - gap * 2, fit: 'contain', label: false, z: list.length});
  selectedTile = draft.tiles[0]?.id; markDirty(); renderLayouts();
}
async function newLayout() {
  if (dirty && !confirm('Discard unsaved changes?')) return;
  const layout = await api('/api/layouts', {method: 'POST', body: {name: 'New layout', width: 1920, height: 1080, background: '#090d14', tiles: []}});
  await refresh(false); layoutDraft = clone(layout); layoutId = layout.id; dirty = false; selectedTile = null; renderLayouts();
}
async function saveLayout(publish) {
  if (!layoutDraft.name.trim()) throw new Error('Give your layout a name.');
  const {name, width, height, background, tiles} = layoutDraft;
  let saved = await api(`/api/layouts/${layoutDraft.id}`, {method: 'PUT', body: {name, width, height, background, tiles}});
  if (publish) saved = await api(`/api/layouts/${saved.id}/publish`, {method: 'POST'});
  layoutDraft = clone(saved); dirty = false; await refresh(false); renderLayouts(); toast(publish ? 'Layout published. Connected displays will update shortly.' : 'Layout draft saved');
}

function displayURL(layout) {
  const url = new URL(`/display/${encodeURIComponent(layout.display_token)}`, location.origin);
  const lanHost = state.settings?.lan_host?.trim();
  if (lanHost) {
    try {
      const configured = new URL(lanHost.includes('://') ? lanHost : `${location.protocol}//${lanHost}`);
      if (!['http:', 'https:'].includes(configured.protocol)) throw new Error();
      url.protocol = configured.protocol; url.hostname = configured.hostname;
      url.port = configured.port || (lanHost.includes('://') ? '' : location.port);
    } catch { /* Keep the known current origin if LAN host is malformed. */ }
  }
  return url.href;
}
function renderDisplays() {
  actions(goButton('Design a layout ↗', 'layouts', 'secondary'));
  content(h('div', {class: 'notice'}, h('span', {text: '◈'}), h('span', {text: 'Open a published display link on a TV, tablet, or browser on your network. Links grant view-only access. Keep them private.'})),
    state.layouts.length ? h('div', {class: 'display-grid'}, state.layouts.map(layout => {
      const url = displayURL(layout), published = !!layout.published;
      return h('section', {class: 'panel display-card'}, h('div', {class: 'spread'}, h('span', {class: 'display-glyph', text: '▣'}), badge(published ? 'PUBLISHED' : 'NOT PUBLISHED', published ? 'success' : '')),
        h('h2', {text: layout.name}), h('p', {text: published ? `${layout.published.width} × ${layout.published.height} · ${layout.published.tiles.length} tiles` : 'Publish this layout before opening a display.'}),
        field('View-only display link', input(url, {readOnly: true, class: 'display-url', onclick: event => event.target.select()})),
        h('div', {class: 'actions'}, h('a', {class: `button primary${!published ? ' disabled' : ''}`, href: published ? url : null, target: '_blank', rel: 'noopener', 'aria-disabled': !published}, 'Open display ↗'),
          button('Copy link', async () => { if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(url); toast('Display link copied'); } else { openModal('Copy display link', 'Select and copy this view-only link.', input(url, {readOnly: true, class: 'display-url', onfocus: event => event.target.select()})); } }, 'secondary')),
        button('Regenerate private link', () => {
          openModal('Regenerate display link?', 'The old link will stop working immediately. Update any screens using it.', h('div', {class: 'actions end'}, button('Cancel', closeModal, 'secondary'), button('Regenerate link', async () => { await api(`/api/layouts/${layout.id}/rotate-token`, {method: 'POST'}); closeModal(); await refresh(false); renderDisplays(); toast('New display link created'); }, 'danger')));
        }, 'ghost'));
    })) : empty('A screen for every corner', 'Create and publish a layout, then its view-only display link will appear here.', goButton('Create a layout', 'layouts')));
  pageRefresh = renderDisplays;
}

function renderSettings() {
  actions();
  const shopName = input(state.settings.shop_name || '', {placeholder: 'Your workshop'});
  const lanHost = input(state.settings.lan_host || '', {placeholder: '192.168.1.10:8080'});
  const form = h('form', {onsubmit: async event => {
    event.preventDefault();
    try {
      await api('/api/settings', {method: 'PUT', body: {shop_name: shopName.value.trim(), lan_host: lanHost.value.trim()}});
      await refresh(false); toast('Settings saved'); renderShell();
    } catch (error) { toast(error.message, true); }
  }}, h('div', {class: 'form-grid'}, field('Workshop name', shopName, 'A name for your corner of the making world.'), field('LAN display host', lanHost, 'Address other devices use to reach this server, including port if needed.')),
  h('div', {class: 'actions end'}, h('button', {class: 'button primary', type: 'submit', text: 'Save settings'})));
  const diagnostics = h('div', {id: 'diagnostics'}, h('p', {class: 'muted', text: 'Checking local capabilities…'}));
  content(section('Workshop', 'The details that make it yours.', form), section('System diagnostics', 'A transparent look at your local setup.', diagnostics),
    h('div', {class: 'notice'}, h('span', {text: '◈'}), h('span', {text: 'Administration is limited to this server’s host / loopback interface. Display links work across the LAN. PrintWatch does not send your camera credentials to a browser.'})));
  const load = async () => {
    try {
      const [health, capabilities] = await Promise.all([api('/api/health'), api('/api/diagnostics')]);
      if (!diagnostics.isConnected) return;
      diagnostics.replaceChildren(h('div', {class: 'diagnostic-summary'}, badge(health.status || 'Unknown', health.status === 'ok' ? 'success' : 'warning'), h('span', {text: `PrintWatch ${health.version || ''}`})),
        diagnosticTree(capabilities), h('details', {}, h('summary', {text: 'Gateway details'}), h('pre', {text: JSON.stringify(health.gateway, null, 2)})));
    } catch (error) { diagnostics.replaceChildren(h('p', {class: 'inline-error', text: error.message})); }
  };
  diagnostics.after(h('div', {class: 'actions'}, button('Refresh diagnostics', load, 'secondary'))); load();
  pageRefresh = () => {};
}
function diagnosticTree(value) {
  if (!value || typeof value !== 'object') return h('span', {text: String(value)});
  return h('dl', {class: 'diagnostic-list'}, Object.entries(value).map(([key, data]) => h('div', {}, h('dt', {text: key.replaceAll('_', ' ')}), h('dd', {}, typeof data === 'object' && data !== null ? diagnosticTree(data) : typeof data === 'boolean' ? badge(data ? 'Available' : 'Unavailable', data ? 'success' : 'warning') : h('span', {text: String(data ?? 'Not configured')})))));
}

async function runDisplay(token) {
  document.body.classList.add('display-mode');
  const host = h('main', {class: 'display-host', 'aria-label': 'Workshop display'});
  const status = h('div', {class: 'display-notice', role: 'status', text: 'Connecting to your workshop…'});
  const fullscreen = button('⛶ Fullscreen', async () => {
    if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen();
  }, 'fullscreen-button');
  root.replaceChildren(host, status, fullscreen);
  composition = new Composition(host, {token});
  let first = true;
  async function poll() {
    try {
      const data = await api(`/api/display/${encodeURIComponent(token)}`);
      if (!data.layout) {
        const error = new Error('This layout has not been published yet.'); error.status = 409; throw error;
      }
      if (!composition) composition = new Composition(host, {token});
      composition.update(data.layout, data);
      status.hidden = data.layout.tiles?.length > 0;
      if (!status.hidden) status.textContent = 'This published layout has no tiles yet.';
      first = false;
      document.title = `${data.layout.name || 'Workshop display'} · PrintWatch`;
    } catch (error) {
      const denied = error.status >= 400 && error.status < 500;
      if (denied) { composition?.destroy(); composition = null; host.replaceChildren(); first = true; }
      status.hidden = false;
      status.textContent = first ? error.message : `Connection interrupted · keeping the last published view · ${error.message}`;
    } finally { setTimeout(poll, 5000); }
  }
  document.addEventListener('pointermove', () => { fullscreen.classList.add('visible'); clearTimeout(fullscreen.hideTimer); fullscreen.hideTimer = setTimeout(() => fullscreen.classList.remove('visible'), 2500); });
  document.addEventListener('focusin', () => fullscreen.classList.add('visible'));
  poll();
}

const displayMatch = location.pathname.match(/^\/display\/([^/]+)\/?$/);
if (displayMatch) runDisplay(decodeURIComponent(displayMatch[1]));
else {
  root.append(h('div', {class: 'startup'}, h('span', {class: 'brand-mark', text: 'P'}), h('h1', {text: 'PrintWatch'}), h('p', {text: 'Connecting to your workshop…'}), h('span', {class: 'spinner'})));
  async function start() {
    try { await refresh(false); route = names[route] ? route : 'overview'; renderShell(); }
    catch (error) {
      root.replaceChildren(h('div', {class: 'startup'}, h('span', {class: 'brand-mark', text: 'P'}), h('h1', {text: 'Your workshop is out of reach'}), h('p', {text: error.message}), h('p', {class: 'muted', text: 'Open administration from the server’s local browser. Display devices should use their private display link.'}), button('Try again', start, 'primary')));
    }
  }
  window.addEventListener('hashchange', () => navigate(location.hash.slice(1) || 'overview'));
  window.addEventListener('beforeunload', event => { if (!updateReload && hasUnsavedWork()) { event.preventDefault(); event.returnValue = ''; } });
  modal.addEventListener('click', event => { if (event.target === modal) { const rect = modal.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeModal(); } });
  start();
  setInterval(async () => {
    if (polling || document.hidden) return;
    polling = true;
    try { await refresh(); } catch { online = false; updateConnection(); } finally { polling = false; }
  }, 5000);
}

watchForUpdates({
  version: frontendBuild,
  hasUnsavedChanges: () => !displayMatch && hasUnsavedWork(),
  onAvailable: showAppUpdate,
  reload: () => { updateReload = true; location.reload(); },
});

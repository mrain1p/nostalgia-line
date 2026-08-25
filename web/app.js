/* Nostalgia Line - browser UI. No build step, no framework. */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
const posterUrl = (path, size = 'w185') =>
  path ? `/api/poster?path=${encodeURIComponent(path)}&size=${size}` : '';
const logoUrl = (number) => `/api/channel-logo/${number}`;

/** "12 minutes ago" — provenance is only useful if it reads at a glance. */
function ago(epochSeconds) {
  if (!epochSeconds) return 'never';
  const secs = Math.max(0, Date.now() / 1000 - epochSeconds);
  const steps = [[31536000, 'y'], [2592000, 'mo'], [86400, 'd'], [3600, 'h'], [60, 'm']];
  for (const [size, label] of steps) {
    if (secs >= size) return `${Math.floor(secs / size)}${label} ago`;
  }
  return 'just now';
}

const splitList = (value) => value.split(',').map((s) => s.trim()).filter(Boolean);

const state = {
  offset: 0,
  limit: 100,
  sort: 'title',
  direction: 'asc',
  total: 0,
  channels: [],
  selected: new Set(),
  networkFilter: '',
  poll: null,
  lastItems: [],
  logoVersion: 0,
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.raw ? {} : { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep statusText */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

let bannerTimer;
let bannerHoldUntil = 0;

/** Transient messages win: the background status poll must not wipe out the
 *  "saved" confirmation the user just earned. */
function banner(message, kind = '', autoHide = 0) {
  const el = $('banner');
  clearTimeout(bannerTimer);
  if (!message) { el.classList.add('is-hidden'); bannerHoldUntil = 0; return; }
  el.className = `banner ${kind}`;
  el.textContent = message;
  el.classList.remove('is-hidden');
  bannerHoldUntil = autoHide ? Date.now() + autoHide : 0;
  if (autoHide) bannerTimer = setTimeout(() => banner(''), autoHide);
}

/** A standing condition (e.g. "not configured"). Never overwrites a transient. */
function standingBanner(message, kind = '') {
  if (Date.now() < bannerHoldUntil) return;
  banner(message, kind);
}

/* ── tabs ──────────────────────────────────────────────────── */
const loaders = {
  networks: () => loadNetworks(),
  channels: () => loadChannels(),
  review: () => loadReview(),
  stations: () => loadStations(),
  settings: () => loadSettings(),
};

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('is-active'));
    document.querySelectorAll('.panel').forEach((p) => p.classList.remove('is-active'));
    tab.classList.add('is-active');
    $(`tab-${tab.dataset.tab}`).classList.add('is-active');
    loaders[tab.dataset.tab]?.();
  });
});

function showTab(name) {
  document.querySelector(`.tab[data-tab="${name}"]`)?.click();
}

/* ── status polling ────────────────────────────────────────── */
async function refreshStatus() {
  let status;
  try { status = await api('/api/status'); } catch { return; }

  const pill = $('scan-state');
  const phase = status.progress?.phase ?? 'idle';
  const { done = 0, total = 0 } = status.progress ?? {};

  if (status.scanning) {
    pill.dataset.state = 'run';
    pill.textContent = total ? `${phase} ${done}/${total}` : phase;
    $('scan-btn').disabled = true;
  } else {
    $('scan-btn').disabled = false;
    if (status.last_error) {
      pill.dataset.state = 'err';
      pill.textContent = 'error';
      standingBanner(status.last_error, 'err');
    } else if (status.stats) {
      pill.dataset.state = 'ok';
      pill.textContent = 'ready';
    } else {
      pill.dataset.state = 'idle';
      pill.textContent = 'idle';
    }
    if (state.poll) {
      clearInterval(state.poll);
      state.poll = null;
      if (!status.last_error) {
        state.selected.clear();
        loadLibrary(); loadReview();
        banner('Scan complete.', 'ok', 3000);
      }
    }
  }

  if (!status.configured) {
    standingBanner('Set your Plex URL, Plex token and TMDB key on the Settings tab to begin.');
  }
  $('cancel-btn').classList.toggle('is-hidden', !status.scanning);

  // A routing input changed after the scan ran, so what is on screen no longer
  // matches what an export would produce.
  const stale = status.stale && !status.scanning;
  $('stale-bar').classList.toggle('is-hidden', !stale);
  if (stale) {
    $('stale-text').textContent =
      `These results are out of date — ${status.stale_reason}. Re-scan to apply it.`;
  }

  renderStats(status.stats);
  renderDiagnostics(status.diagnostics);
  renderProvenance(status);
  $('tab-library-count').textContent = status.stats ? status.stats.total : '';
  $('tab-review-count').textContent = status.stats ? status.stats.needs_review : '';
  if (status.defaults) {
    $('defaults-summary').innerHTML = [
      ['Assignments', status.defaults.rows],
      ['Distinct titles', status.defaults.titles],
      ['On 2+ channels', status.defaults.multi_channel_titles],
      ['Sanctioned pairings', status.defaults.sanctioned_pairs],
      ['Imported', status.baseline?.at ? ago(status.baseline.at) : 'shipped defaults'],
    ].map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
  }
}

/** What is loaded, how far it has drifted, and when it last left the app. */
function renderProvenance(status) {
  const el = $('provenance');
  if (!status.stats) { el.innerHTML = ''; return; }
  const pending = status.pending || {};
  const changed = (pending.additions || 0) + (pending.overrides || 0);
  const sep = '<span class="prov-sep"></span>';

  const scanAge = status.scan_at ? ago(status.scan_at) : null;
  const scanStale = status.scan_at && (Date.now() / 1000 - status.scan_at) > 86400;
  const parts = [
    `<span class="prov-item ${scanStale ? 'is-dirty' : ''}">Scan <b>${
      scanAge ? esc(scanAge) : 'none'}</b>${scanStale ? ' — may be out of date' : ''}</span>`,
    `<span class="prov-item">Lineup <b>${esc(status.defaults.rows)}</b> rows${
      status.baseline?.at ? ` · imported ${esc(ago(status.baseline.at))}` : ' · shipped defaults'}</span>`,
    `<span class="prov-item ${changed ? 'is-dirty' : ''}">${
      changed ? `<b>${changed}</b> change${changed === 1 ? '' : 's'} pending` : 'No pending changes'}</span>`,
  ];
  if (pending.held_for_review) {
    parts.push(`<span class="prov-item">${pending.held_for_review} held for review</span>`);
  }
  parts.push(`<span class="prov-item">Last export <b>${esc(ago(status.last_export_at))}</b></span>`);
  if (status.posters?.count) {
    parts.push(`<span class="prov-item">${status.posters.count} posters cached</span>`);
  }
  el.innerHTML = parts.join(sep);
}

/* Each tile is also the filter for what it counts - the number you are looking
   at is usually the set you want to work on next. */
const STAT_TILES = [
  { label: 'Titles', key: 'total', cls: '', filter: {} },
  { label: 'Already assigned', key: 'already_assigned', cls: '', filter: { status: 'already_assigned' } },
  { label: 'Placed by Line', key: 'assigned_by_line', cls: 'good', filter: { status: 'assigned' } },
  { label: 'Unassigned', key: 'unassigned', cls: 'bad', filter: { status: 'unassigned' } },
  { label: 'Needs review', key: 'needs_review', cls: 'warn', filter: { review: true } },
  { label: 'Coverage', key: 'coverage_pct', cls: '', suffix: '%' },
];

function activeTileIndex() {
  const status = $('status-filter').value;
  const review = $('review-filter').checked;
  if (review) return 4;
  return STAT_TILES.findIndex((t, i) => i > 0 && t.filter && t.filter.status === status && status);
}

function renderStats(stats) {
  if (!stats) { $('stats').innerHTML = ''; return; }
  const active = activeTileIndex();
  $('stats').innerHTML = STAT_TILES.map((tile, i) => {
    const value = `${stats[tile.key]}${tile.suffix || ''}`;
    const zero = !tile.filter || stats[tile.key] === 0;
    const cls = [
      'stat', tile.cls,
      tile.filter && !zero ? 'is-clickable' : '',
      i === active ? 'is-on' : '',
    ].filter(Boolean).join(' ');
    return `<div class="${cls}" ${tile.filter && !zero ? `data-tile="${i}"` : ''}
      ${tile.filter && !zero ? 'title="Filter the library to these"' : ''}>
      <b>${esc(value)}</b><span>${esc(tile.label)}</span></div>`;
  }).join('');
}

$('stats').addEventListener('click', (event) => {
  const tile = event.target.closest('[data-tile]');
  if (!tile) return;
  const spec = STAT_TILES[Number(tile.dataset.tile)];
  const alreadyOn = tile.classList.contains('is-on');
  $('status-filter').value = alreadyOn ? '' : (spec.filter.status || '');
  $('review-filter').checked = alreadyOn ? false : Boolean(spec.filter.review);
  state.offset = 0;
  showTab('library');
  loadLibrary();
  refreshStatus();
});

function renderDiagnostics(diag) {
  const el = $('diagnostics');
  if (!diag || (!diag.no_tmdb_id && !diag.no_network)) { el.innerHTML = ''; return; }
  const parts = [];
  if (diag.no_tmdb_id) {
    parts.push(`<strong>${diag.no_tmdb_id}</strong> item(s) have no TMDB id in Plex, so nothing
      can route them. Fix the match in Plex, then re-scan. e.g.
      ${diag.no_tmdb_samples.map(esc).join(', ')}`);
  }
  if (diag.no_network) {
    parts.push(`<strong>${diag.no_network}</strong> show(s) have a TMDB record with no network
      listed. e.g. ${diag.no_network_samples.map(esc).join(', ')}`);
  }
  el.innerHTML = `<div class="notice-block">${parts.join('<br><br>')}</div>`;
}

/* ── scan ──────────────────────────────────────────────────── */
$('scan-btn').addEventListener('click', async () => {
  banner('');
  try {
    await api('/api/scan', { method: 'POST' });
    $('scan-btn').disabled = true;
    if (!state.poll) state.poll = setInterval(refreshStatus, 700);
  } catch (err) {
    banner(err.message, 'err');
  }
});

$('cancel-btn').addEventListener('click', async () => {
  try {
    await api('/api/scan/cancel', { method: 'POST' });
    banner('Scan cancelled.', '', 3000);
  } catch (err) { banner(err.message, 'err'); }
});

$('stale-rescan').addEventListener('click', () => $('scan-btn').click());

/* ── library ───────────────────────────────────────────────── */
function libraryParams(extra = {}) {
  const params = new URLSearchParams({
    sort: state.sort,
    direction: state.direction,
    ...extra,
  });
  if ($('q').value.trim()) params.set('q', $('q').value.trim());
  if ($('status-filter').value) params.set('status_filter', $('status-filter').value);
  if ($('confidence-filter').value) params.set('confidence', $('confidence-filter').value);
  if ($('source-filter').value) params.set('source', $('source-filter').value);
  if ($('section-filter').value) params.set('section', $('section-filter').value);
  if ($('review-filter').checked) params.set('review_only', 'true');
  if (state.networkFilter) params.set('network', state.networkFilter);
  return params;
}

async function loadLibrary() {
  const params = libraryParams({ offset: state.offset, limit: state.limit });

  let data;
  try { data = await api(`/api/library?${params}`); } catch (err) { banner(err.message, 'err'); return; }

  state.total = data.total;
  state.lastItems = data.items;
  const body = $('library-body');

  if (!data.scanned) {
    body.innerHTML = '';
    $('library-empty').textContent = 'Run a scan to populate the library.';
    $('library-empty').classList.remove('is-hidden');
  } else if (!data.items.length) {
    body.innerHTML = '';
    $('library-empty').textContent = 'No titles match these filters.';
    $('library-empty').classList.remove('is-hidden');
  } else {
    $('library-empty').classList.add('is-hidden');
    body.innerHTML = data.items.map(rowHtml).join('');
  }

  const sectionSelect = $('section-filter');
  if (data.sections?.length && sectionSelect.options.length <= 1) {
    sectionSelect.innerHTML = '<option value="">All libraries</option>' +
      data.sections.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
  }

  const from = state.total ? state.offset + 1 : 0;
  const to = Math.min(state.offset + state.limit, state.total);
  $('page-info').textContent = `${from}–${to} of ${state.total}`;
  $('prev').disabled = state.offset === 0;
  $('next').disabled = state.offset + state.limit >= state.total;

  document.querySelectorAll('th.sortable').forEach((th) => {
    th.classList.toggle('sorted', th.dataset.sort === state.sort);
    th.classList.toggle('asc', th.dataset.sort === state.sort && state.direction === 'asc');
  });

  renderActiveFilter();
  syncSelectionUi();
}

function renderActiveFilter() {
  const el = $('active-filter');
  if (!state.networkFilter) { el.classList.add('is-hidden'); return; }
  el.classList.remove('is-hidden');
  el.innerHTML = `Showing titles from <strong>${esc(state.networkFilter)}</strong>
    <button class="btn-link" id="drop-network-filter">clear</button>`;
  $('drop-network-filter').addEventListener('click', () => {
    state.networkFilter = ''; state.offset = 0; loadLibrary();
  });
}

function channelChips(item) {
  if (!item.channels.length) {
    return `<button class="chip chip-add" data-assign="${esc(item.uid)}">+ assign</button>`;
  }
  const primary = new Set((item.assignments || []).filter((a) => a.primary).map((a) => a.channel_number));
  return item.channels.map((c) => {
    const secondary = item.assignments?.length && !primary.has(c.number);
    return `<button class="chip ${secondary ? 'chip-secondary' : ''}" data-assign="${esc(item.uid)}"
      title="${esc(c.name)} (${c.number}) — click to change">${c.number} ${esc(c.name)}</button>`;
  }).join('');
}

function artCell(item) {
  if (!$('show-posters').checked) return '';
  // lazy so a 500-row page does not fire 500 requests up front; the server
  // caches each poster to disk on first hit, so scrolling stays cheap.
  return item.poster_path
    ? `<img class="art" loading="lazy" decoding="async" alt=""
         src="${esc(posterUrl(item.poster_path, 'w92'))}">`
    : '<div class="art"></div>';
}

function rowHtml(item) {
  const why = (item.assignments || []).map((a) => a.reason).join('; ')
    || item.review_reason
    || (item.mapping_source === 'lineup' ? 'already in your channels.csv' : '');
  const flag = item.needs_review ? `<span class="flag" title="${esc(item.review_reason)}">⚑</span>` : '';
  const over = item.overridden ? '<span class="overridden" title="assigned by hand">✎</span>' : '';
  const checked = state.selected.has(item.uid) ? 'checked' : '';
  const net = item.network
    ? `<button class="btn-link" data-network="${esc(item.network)}">${esc(item.network)}</button>`
    : '';
  return `<tr class="is-clickable ${checked ? 'is-selected' : ''}" data-detail="${esc(item.uid)}">
    <td class="col-tick"><input type="checkbox" class="box" data-uid="${esc(item.uid)}" ${checked}></td>
    <td class="col-art">${artCell(item)}</td>
    <td class="title-cell">${esc(item.title)}${flag}${over}</td>
    <td class="num col-year">${item.year ?? ''}</td>
    <td class="num col-sn">${item.season_count || ''}</td>
    <td class="num col-eps">${item.episode_count || ''}</td>
    <td>${net}</td>
    <td>${channelChips(item)}</td>
    <td class="col-conf"><span class="conf conf-${esc(item.confidence)}">${esc(item.confidence)}</span></td>
    <td><span class="src src-${esc(item.mapping_source)}"
      title="${esc(SOURCE_LABEL[item.mapping_source] || '')}">${esc(item.status.replace(/_/g, ' '))}</span></td>
    <td class="why col-why">${esc(why)}</td>
  </tr>`;
}

document.querySelectorAll('th.sortable').forEach((th) => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    state.direction = state.sort === key && state.direction === 'asc' ? 'desc' : 'asc';
    state.sort = key;
    state.offset = 0;
    loadLibrary();
  });
});

let searchTimer;
$('q').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.offset = 0; loadLibrary(); }, 220);
});
$('show-posters').addEventListener('change', loadLibrary);
['status-filter', 'section-filter', 'review-filter', 'confidence-filter', 'source-filter'].forEach((id) => {
  $(id).addEventListener('change', () => { state.offset = 0; loadLibrary(); });
});
$('clear-filters').addEventListener('click', () => {
  $('q').value = '';
  ['status-filter', 'section-filter', 'confidence-filter', 'source-filter'].forEach((id) => { $(id).value = ''; });
  $('review-filter').checked = false;
  state.networkFilter = '';
  state.offset = 0;
  loadLibrary();
});
$('prev').addEventListener('click', () => { state.offset = Math.max(0, state.offset - state.limit); loadLibrary(); });
$('next').addEventListener('click', () => { state.offset += state.limit; loadLibrary(); });
$('page-size').addEventListener('change', (e) => {
  state.limit = Number(e.target.value); state.offset = 0; loadLibrary();
});

/* ── selection & bulk actions ──────────────────────────────── */
$('library-body').addEventListener('change', (event) => {
  const uid = event.target.dataset?.uid;
  if (!uid) return;
  if (event.target.checked) state.selected.add(uid); else state.selected.delete(uid);
  event.target.closest('tr')?.classList.toggle('is-selected', event.target.checked);
  syncSelectionUi();
});

$('check-page').addEventListener('change', (event) => {
  state.lastItems.forEach((item) => {
    if (event.target.checked) state.selected.add(item.uid); else state.selected.delete(item.uid);
  });
  document.querySelectorAll('#library-body input[data-uid]').forEach((box) => {
    box.checked = event.target.checked;
    box.closest('tr')?.classList.toggle('is-selected', event.target.checked);
  });
  syncSelectionUi();
});

function syncSelectionUi() {
  const n = state.selected.size;
  $('bulk-bar').classList.toggle('is-hidden', n === 0);
  $('bulk-count').textContent = `${n} selected`;
  const pageUids = state.lastItems.map((i) => i.uid);
  $('check-page').checked = pageUids.length > 0 && pageUids.every((u) => state.selected.has(u));
}

$('bulk-clear').addEventListener('click', () => {
  state.selected.clear();
  document.querySelectorAll('#library-body input[data-uid]').forEach((b) => { b.checked = false; });
  document.querySelectorAll('#library-body tr').forEach((r) => r.classList.remove('is-selected'));
  syncSelectionUi();
});

$('bulk-select-all').addEventListener('click', async () => {
  // Pull every uid matching the current filters, not just this page.
  const params = libraryParams({ offset: 0, limit: 1000 });
  let all = [];
  for (let offset = 0; ; offset += 1000) {
    params.set('offset', offset);
    const data = await api(`/api/library?${params}`);
    all = all.concat(data.items.map((i) => i.uid));
    if (all.length >= data.total || !data.items.length) break;
  }
  all.forEach((uid) => state.selected.add(uid));
  document.querySelectorAll('#library-body input[data-uid]').forEach((b) => {
    b.checked = true; b.closest('tr')?.classList.add('is-selected');
  });
  syncSelectionUi();
  banner(`${all.length} titles selected.`, 'ok', 2500);
});

$('bulk-assign').addEventListener('click', () => openAssign(null, 'replace'));
$('bulk-add').addEventListener('click', () => openAssign(null, 'add'));

$('bulk-unassign').addEventListener('click', async () => {
  if (!confirm(`Unassign ${state.selected.size} title(s)?`)) return;
  try {
    const result = await api('/api/override/bulk', {
      method: 'POST',
      body: JSON.stringify({ uids: [...state.selected], channels: [] }),
    });
    banner(`Unassigned ${result.updated} title(s).`, 'ok', 3000);
    state.selected.clear();
    loadLibrary(); loadReview(); refreshStatus();
  } catch (err) { banner(err.message, 'err'); }
});

/* ── assignment dialog (single + bulk) ─────────────────────── */
let assignTarget = null;      // uid, or null for a bulk assign
let assignMode = 'replace';
let assignSelection = new Set();

document.addEventListener('click', (event) => {
  const detailTrigger = event.target.closest('[data-detail]');
  if (detailTrigger && !event.target.closest('input, .chip')) {
    openDetail(detailTrigger.dataset.detail);
    return;
  }
  const uid = event.target.dataset?.assign;
  if (uid) openAssign(uid, 'replace');
  const network = event.target.dataset?.network;
  if (network) {
    state.networkFilter = network;
    state.offset = 0;
    showTab('library');
    loadLibrary();
  }
});

async function ensureChannels() {
  if (!state.channels.length) {
    state.channels = (await api('/api/channels')).channels.filter((c) => c.accepts_content);
  }
  return state.channels;
}

async function openAssign(uid, mode) {
  await ensureChannels();
  assignTarget = uid;
  assignMode = mode;
  assignSelection = new Set();

  if (uid) {
    const item = await api(`/api/item/${encodeURIComponent(uid)}`).catch(() => null);
    if (item) {
      item.channels.forEach((c) => assignSelection.add(c.number));
      $('assign-title').textContent = item.title;
      $('assign-context').textContent =
        `${item.network ? `Network: ${item.network}. ` : ''}${item.review_reason || ''}`.trim();
    }
  } else {
    $('assign-title').textContent = mode === 'add'
      ? `Add a channel to ${state.selected.size} title(s)`
      : `Assign ${state.selected.size} title(s)`;
    $('assign-context').textContent = mode === 'add'
      ? 'Existing channels are kept.'
      : 'This replaces whatever channels they are on now.';
  }
  $('assign-search').value = '';
  renderAssignOptions('');
  $('assign-dialog').showModal();
}

function renderAssignOptions(filter) {
  const needle = filter.toLowerCase();
  const rows = state.channels
    .filter((c) => !needle || c.name.toLowerCase().includes(needle) || String(c.number).includes(needle))
    .map((c) => `<label class="assign-option">
      <input type="checkbox" value="${c.number}" ${assignSelection.has(c.number) ? 'checked' : ''}>
      <span class="n">${c.number}</span><span>${esc(c.name)}</span>
      <span class="cat">${esc(c.category)} · ${c.total}</span>
    </label>`).join('');
  $('assign-options').innerHTML = rows || '<p class="empty">No channels match.</p>';
  $('assign-options').querySelectorAll('input').forEach((box) => {
    box.addEventListener('change', () => {
      const value = Number(box.value);
      if (box.checked) assignSelection.add(value); else assignSelection.delete(value);
    });
  });
}

$('assign-search').addEventListener('input', (e) => renderAssignOptions(e.target.value));

$('assign-form').addEventListener('submit', async (event) => {
  if (event.submitter?.value !== 'save') return;
  try {
    if (assignTarget) {
      await api('/api/override', {
        method: 'POST',
        body: JSON.stringify({ uid: assignTarget, channels: [...assignSelection] }),
      });
      banner('Assignment saved.', 'ok', 2500);
    } else {
      const result = await api('/api/override/bulk', {
        method: 'POST',
        body: JSON.stringify({
          uids: [...state.selected],
          channels: [...assignSelection],
          mode: assignMode,
        }),
      });
      banner(`Updated ${result.updated} title(s).`, 'ok', 3000);
      state.selected.clear();
    }
    state.channels = [];
    loadLibrary(); loadReview(); refreshStatus();
  } catch (err) {
    banner(err.message, 'err');
  }
});

/* ── review queue ──────────────────────────────────────────── */
async function loadReview() {
  let data;
  try { data = await api('/api/review'); } catch { return; }
  const list = $('review-list');
  $('review-empty').classList.toggle('is-hidden', data.total > 0);
  list.innerHTML = data.items.map((item) => `
    <div class="card review">
      ${item.poster_path
        ? `<img class="review-art" loading="lazy" decoding="async" alt=""
             src="${esc(posterUrl(item.poster_path, 'w154'))}">`
        : '<div class="review-art"></div>'}
      <div class="review-body">
      <h4>${esc(item.title)}</h4>
      <div class="review-meta">${item.year ?? '—'} · ${item.episode_count || 0} eps${item.network ? ` · ${esc(item.network)}` : ''}</div>
      <div class="review-reason">⚑ ${esc(item.review_reason)}</div>
      ${item.overview ? `<div class="review-overview">${esc(item.overview)}</div>` : ''}
      <div>${channelChips(item)}</div>
      <div class="card-actions">
        <button class="btn btn-small" data-assign="${esc(item.uid)}">Assign</button>
        <button class="btn btn-small" data-dismiss="${esc(item.uid)}">Looks right</button>
        ${item.network ? `<button class="btn btn-small" data-network="${esc(item.network)}">All from ${esc(item.network)}</button>` : ''}
        ${item.tmdb_id ? `<a class="btn btn-small" target="_blank" rel="noopener"
           href="https://www.themoviedb.org/tv/${item.tmdb_id}">TMDB ↗</a>` : ''}
      </div>
      </div>
    </div>`).join('');

  list.querySelectorAll('[data-dismiss]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      await api(`/api/dismiss/${encodeURIComponent(btn.dataset.dismiss)}`, { method: 'POST' });
      loadReview(); refreshStatus();
    });
  });
}

/* ── networks ──────────────────────────────────────────────── */
let networkRows = [];

async function loadNetworks() {
  await ensureChannels();
  const data = await api('/api/networks').catch(() => null);
  if (!data) return;
  networkRows = data.networks;
  $('tab-networks-count').textContent = data.total || '';
  $('network-empty').classList.toggle('is-hidden', data.scanned && data.total > 0);

  if (data.scanned) {
    const unmapped = data.networks.filter((n) => n.status === 'unmapped').length;
    const orphan = data.networks.filter((n) => n.status === 'orphan').length;
    $('network-stats').innerHTML = [
      ['Networks', data.total, ''],
      ['Unmapped', unmapped, unmapped ? 'bad' : ''],
      ['Orphan fallbacks', orphan, orphan ? 'warn' : ''],
      ['Titles stranded', data.unmapped_titles, data.unmapped_titles ? 'bad' : ''],
    ].map(([l, v, c]) => `<div class="stat ${c}"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join('');
  }
  renderNetworks();
}

function renderNetworks() {
  const onlyUnmapped = $('only-unmapped').checked;
  const needle = $('network-q').value.trim().toLowerCase();
  const rows = networkRows
    .filter((n) => !onlyUnmapped || n.status === 'unmapped' || n.status === 'orphan')
    .filter((n) => !needle || n.network.toLowerCase().includes(needle));

  $('network-body').innerHTML = rows.map((n) => {
    // A network scattered across a dozen channels is the symptom worth seeing,
    // so show the top few and count the rest rather than printing a wall of chips.
    const shown = n.landing.slice(0, 3);
    const rest = n.landing.length - shown.length;
    const landing = n.landing.length
      ? shown.map((l) => `<span class="chip" title="${l.titles} title(s)">${l.number} ${esc(l.name)}</span>`).join('')
        + (rest > 0 ? `<span class="muted" title="${n.landing.map((l) => `${l.number} ${l.name} (${l.titles})`).join(', ')}">+${rest} more</span>` : '')
      : '<span class="muted">nowhere</span>';
    const scattered = n.landing.length >= 4 && n.status !== 'mapped'
      ? `<span class="scatter" title="These titles landed on ${n.landing.length} different channels because nothing maps this network">scattered across ${n.landing.length}</span>`
      : '';
    const options = state.channels
      .map((c) => `<option value="${c.number}" ${c.number === n.channel_number ? 'selected' : ''}>${c.number} ${esc(c.name)}</option>`)
      .join('');
    return `<tr>
      <td class="title-cell">
        <button class="btn-link" data-network="${esc(n.network)}">${esc(n.network)}</button>
        <div class="samples">${n.samples.map(esc).join(' · ')}</div>
      </td>
      <td class="num">${n.titles}</td>
      <td class="num">${n.episodes}</td>
      <td><span class="netstatus netstatus-${n.status}">${n.status}</span>
        ${n.needs_review ? `<span class="flag" title="${n.needs_review} need review">⚑${n.needs_review}</span>` : ''}</td>
      <td>${landing}${scattered ? `<div>${scattered}</div>` : ''}</td>
      <td class="maprow">
        <select data-map-select="${esc(n.network)}"><option value="">— pick a channel —</option>${options}</select>
        <button class="btn btn-small" data-map-save="${esc(n.network)}">Map</button>
        ${n.status === 'custom' ? `<button class="btn btn-small" data-map-clear="${esc(n.network)}">Reset</button>` : ''}
      </td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="empty">Nothing matches.</td></tr>';

  $('network-body').querySelectorAll('[data-map-save]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const network = btn.dataset.mapSave;
      const select = $('network-body').querySelector(`[data-map-select="${CSS.escape(network)}"]`);
      if (!select.value) { banner('Pick a channel first.', 'err', 2500); return; }
      try {
        const result = await api('/api/networks/map', {
          method: 'POST',
          body: JSON.stringify({ network, channel: Number(select.value) }),
        });
        banner(`Every ${network} title will route to ${result.channel_name}. Re-scan to apply.`, 'ok', 5000);
        loadNetworks();
      } catch (err) { banner(err.message, 'err'); }
    });
  });

  $('network-body').querySelectorAll('[data-map-clear]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      await api(`/api/networks/map/${encodeURIComponent(btn.dataset.mapClear)}`, { method: 'DELETE' });
      banner('Mapping reset. Re-scan to apply.', 'ok', 3500);
      loadNetworks();
    });
  });
}

['only-unmapped', 'network-q'].forEach((id) => {
  $(id).addEventListener('input', renderNetworks);
  $(id).addEventListener('change', renderNetworks);
});

/* ── title detail ─────────────────────────────────────────── */
const SOURCE_LABEL = {
  lineup: 'From your channels.csv',
  auto: 'Placed by Nostalgia Line',
  manual: 'Assigned by you',
  none: 'Not placed',
};

async function openDetail(uid) {
  const item = await api(`/api/item/${encodeURIComponent(uid)}`).catch(() => null);
  if (!item) return;

  const chips = (item.channels || [])
    .map((c) => `<button class="chip" data-assign="${esc(item.uid)}">${c.number} ${esc(c.name)}</button>`)
    .join('') || '<span class="muted">none</span>';

  const why = (item.assignments || []).length
    ? item.assignments.map((a) => `<div class="detail-why">
        <b>${a.channel_number} ${esc(a.channel_name)}</b> — ${esc(a.reason)}
        <br><span class="muted">${esc(a.rule)} · ${esc(a.confidence)} confidence${a.primary ? '' : ' · secondary'}</span>
      </div>`).join('')
    : `<div class="detail-why">${esc(item.review_reason || 'Nothing placed this title.')}</div>`;

  const rows = [
    ['TMDB network', item.network || '—'],
    ['Mapping', SOURCE_LABEL[item.mapping_source] || item.mapping_source],
    ['Confidence', item.confidence],
    ['Library', item.section],
    ['Seasons / episodes', `${item.season_count || '—'} / ${item.episode_count || '—'}`],
    ['Genres', (item.genres || []).join(', ') || '—'],
    ['TMDB id', item.tmdb_id || 'missing'],
  ].map(([k, v]) => `<div class="detail-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');

  const lineupNote = item.mapping_source === 'lineup'
    ? `<p class="hint" style="margin-top:10px">This came from the channels.csv you imported.
       Export never removes an existing row, so changing it here adds a second placement
       rather than moving it.</p>`
    : '';

  $('detail-panel').innerHTML = `
    <div class="detail-head">
      ${item.poster_path
        ? `<img class="detail-art" src="${esc(posterUrl(item.poster_path, 'w185'))}" alt="">`
        : '<div class="detail-art"></div>'}
      <div>
        <h3>${esc(item.title)}</h3>
        <div class="detail-meta">${item.year ?? '—'}${item.network ? ` · ${esc(item.network)}` : ''}</div>
        <div class="detail-meta"><span class="src src-${esc(item.mapping_source)}">${esc(item.mapping_source)}</span></div>
      </div>
    </div>

    <div class="detail-section">
      <h4>Channels</h4>
      <div class="chips">${chips}</div>
      <div class="detail-actions">
        <button class="btn btn-small btn-primary" data-assign="${esc(item.uid)}">Change channel</button>
        ${item.tmdb_id ? `<a class="btn btn-small" target="_blank" rel="noopener"
          href="https://www.themoviedb.org/tv/${item.tmdb_id}">TMDB ↗</a>` : ''}
        ${item.network ? `<button class="btn btn-small" data-network="${esc(item.network)}">All from ${esc(item.network)}</button>` : ''}
      </div>
      ${lineupNote}
    </div>

    <div class="detail-section"><h4>Why it landed there</h4>${why}</div>
    ${item.overview ? `<div class="detail-section"><h4>Overview</h4>
      <p class="detail-overview">${esc(item.overview)}</p></div>` : ''}
    <div class="detail-section"><h4>Details</h4><div class="detail-rows">${rows}</div></div>
    <div class="detail-actions">
      <button class="btn btn-small btn-ghost" data-close-detail>Close</button>
    </div>`;
  $('detail').classList.remove('is-hidden');
}

document.addEventListener('click', (event) => {
  if (event.target.closest('[data-close-detail]')) $('detail').classList.add('is-hidden');
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') $('detail').classList.add('is-hidden');
});

/* ── channel contents ─────────────────────────────────────── */
let channelRows = [];
let channelData = null;
let channelNumber = null;
let networkCatalog = null;

function showChannelPane(which) {
  const mine = which === 'mine';
  $('seg-mine').classList.toggle('is-on', mine);
  $('seg-network').classList.toggle('is-on', !mine);
  $('channel-body').classList.toggle('is-hidden', !mine);
  $('channel-network').classList.toggle('is-hidden', mine);
  $('channel-q').placeholder = mine ? 'Filter titles' : 'Filter the station catalogue';
}

$('seg-mine').addEventListener('click', () => { showChannelPane('mine'); renderChannelRows(); });
$('seg-network').addEventListener('click', async () => {
  showChannelPane('network');
  if (networkCatalog === null) await loadNetworkCatalog();
  else renderNetworkCatalog();
});

/** What really aired on the station this channel parodies, and what you lack. */
async function loadNetworkCatalog() {
  $('channel-network').innerHTML = '<p class="empty">Asking TMDB what aired here…</p>';
  try {
    networkCatalog = await api(`/api/channels/${channelNumber}/network-catalog`);
    renderNetworkCatalog();
  } catch (err) {
    networkCatalog = null;
    $('channel-network').innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

function renderNetworkCatalog() {
  const data = networkCatalog;
  if (!data) return;
  if (!data.titles.length) {
    $('channel-network').innerHTML = `<p class="empty">${esc(data.note || 'Nothing to show.')}</p>`;
    $('channel-network-note').textContent = '';
    return;
  }
  const needle = $('channel-q').value.trim().toLowerCase();
  const rows = data.titles.filter((t) => !needle || t.name.toLowerCase().includes(needle));

  $('channel-network').innerHTML = `<div class="net-grid">${rows.map((t) => {
    const cls = !t.in_library ? 'missing' : (t.elsewhere ? 'have' : 'have');
    const badge = !t.in_library
      ? '<span class="net-badge missing">not in library</span>'
      : t.elsewhere
        ? `<span class="net-badge elsewhere" title="You have it, on ${t.channels.map((c) => c.name).join(', ')}">on ${t.channels[0]?.number ?? '?'}</span>`
        : '<span class="net-badge have">here</span>';
    const art = t.poster_path
      ? `<img loading="lazy" decoding="async" alt="" src="${esc(posterUrl(t.poster_path, 'w185'))}">`
      : '<div class="ph"></div>';
    const open = t.uid ? ` data-detail="${esc(t.uid)}"` : '';
    return `<div class="net-card ${cls}"${open} style="${t.uid ? 'cursor:pointer' : ''}">
      ${art}${badge}
      <div class="n">${esc(t.name)}</div>
      <div class="y">${esc((t.first_air_date || '').slice(0, 4) || '—')}</div>
    </div>`;
  }).join('')}</div>`;

  $('channel-network-note').innerHTML =
    `Top ${data.counts.total} shows TMDB lists for <strong>${esc(data.network)}</strong> — ` +
    `you have <strong>${data.counts.in_library}</strong>, missing <strong>${data.counts.missing}</strong>. ` +
    `Popularity order, so it is a sense of the station rather than its full history.`;
}


async function openChannel(number) {
  const data = await api(`/api/channels/${number}/titles`).catch(() => null);
  if (!data) return;
  channelData = data;
  channelRows = data.titles;
  channelNumber = number;
  networkCatalog = null;
  showChannelPane('mine');
  $('channel-art').src = `${logoUrl(number)}?v=${state.logoVersion}`;
  $('channel-title').textContent = `${data.channel.number} · ${data.channel.name}`;
  $('channel-sub').textContent =
    `${data.counts.in_library} from your library`
    + (data.counts.needs_review ? ` · ${data.counts.needs_review} need review` : '')
    + (data.counts.not_in_library ? ` · ${data.counts.not_in_library} in the lineup file but not in your library` : '');
  $('channel-q').value = '';
  renderChannelRows();
  $('channel-dialog').showModal();
}

function renderChannelRows() {
  const needle = $('channel-q').value.trim().toLowerCase();
  const rows = channelRows.filter((r) => !needle || r.title.toLowerCase().includes(needle));
  const body = rows.map((r) => `
    <div class="channel-row">
      ${r.poster_path
        ? `<img class="art" loading="lazy" src="${esc(posterUrl(r.poster_path, 'w92'))}" alt="">`
        : '<div class="art"></div>'}
      <div class="channel-row-main">
        <div class="t">${esc(r.title)} ${r.needs_review ? '<span class="flag" title="needs review">⚑</span>' : ''}</div>
        <div class="s">${r.year ?? '—'} · ${r.episode_count || 0} eps${r.network ? ` · ${esc(r.network)}` : ''}${
          r.other_channels.length ? ` · also on ${r.other_channels.map((c) => c.number).join(', ')}` : ''}</div>
      </div>
      <span class="src src-${esc(r.mapping_source)}">${esc(r.mapping_source)}</span>
      <div class="channel-row-actions">
        <button class="btn btn-small" data-detail="${esc(r.uid)}">Details</button>
        <button class="btn btn-small" data-assign="${esc(r.uid)}">Move</button>
      </div>
    </div>`).join('');

  const absent = channelData && channelData.not_in_library.length
    ? `<div class="channel-absent"><strong>${channelData.not_in_library.length}</strong> title(s) sit
       on this channel in your channels.csv but are not in your media library, so there is nothing
       to edit here: ${channelData.not_in_library.slice(0, 12).map((t) => esc(t.title)).join(', ')}${
         channelData.not_in_library.length > 12 ? '…' : ''}</div>`
    : '';

  $('channel-body').innerHTML =
    (body || '<p class="empty">Nothing from your library on this channel yet.</p>') + absent;
}

$('channel-q').addEventListener('input', () => {
  if ($('channel-network').classList.contains('is-hidden')) renderChannelRows();
  else renderNetworkCatalog();
});
$('channel-close').addEventListener('click', () => $('channel-dialog').close());

$('channel-table').addEventListener('click', (event) => {
  if (event.target.closest('button, a')) return;
  const row = event.target.closest('[data-channel]');
  if (row) openChannel(Number(row.dataset.channel));
});

/* ── channel artwork ──────────────────────────────────────── */
async function loadLogos() {
  const data = await api('/api/logos').catch(() => null);
  if (!data) return;
  const badge = $('logo-badge');
  const covered = data.installed_count + (data.from_tmdb || 0);
  badge.dataset.state = covered ? 'ok' : 'testing';
  badge.textContent = `${covered}/${data.total_channels} channels`;

  const lines = [];
  if (data.from_tmdb) {
    lines.push(`<p class="hint"><strong>${data.from_tmdb}</strong> channels use the real
      network's logo from TMDB automatically — no setup needed.</p>`);
  }
  if (data.installed_count) {
    lines.push(`<p class="hint"><strong>${data.installed_count}</strong> use artwork you
      supplied, which always wins over the automatic one.</p>`);
  }
  if (data.extra_dirs?.length) {
    lines.push(`<p class="hint">Also reading: ${data.extra_dirs.map(esc).join(', ')}</p>`);
  }
  if (data.missing_count) {
    lines.push(`<p class="hint">${data.missing_count} fall back to a generated badge.</p>`);
  }
  $('logo-coverage').innerHTML = lines.join('');
}

async function fetchPlaylistArtwork(url, resultId, badgeId) {
  const target = $(resultId);
  if (badgeId) setBadge(badgeId, 'testing', 'fetching…');
  target.innerHTML = '<p class="hint">Fetching playlist and artwork…</p>';
  try {
    const r = await api('/api/logos/from-m3u', {
      method: 'POST', body: JSON.stringify({ url: url || '' }),
    });
    const bits = [`<p class="result-ok">✓ Imported ${r.imported_count} channel logo(s).</p>`];
    if (r.unmatched_count) {
      bits.push(`<p class="hint">${r.unmatched_count} playlist entries are not NostalgiaTV
        channels (live TV and the like) and were ignored.</p>`);
    }
    if (r.skipped_count) {
      bits.push(`<p class="hint">${r.skipped_count} could not be fetched:
        ${r.skipped.slice(0, 4).map((x) => `${esc(x.file)} (${esc(x.why)})`).join(', ')}</p>`);
    }
    target.innerHTML = bits.join('');
    if (badgeId) setBadge(badgeId, r.imported_count ? 'ok' : 'err',
      r.imported_count ? `${r.imported_count} logos` : 'nothing imported');
    state.logoVersion += 1;
    loadLogos(); loadChannels();
  } catch (err) {
    target.innerHTML = `<p class="result-err">✗ ${esc(err.message)}</p>`;
    if (badgeId) setBadge(badgeId, 'err', 'failed');
  }
}

$('ntv-fetch').addEventListener('click', () =>
  fetchPlaylistArtwork($('ntv-m3u').value.trim(), 'ntv-result', 'ntv-badge'));

$('ntv-upload').addEventListener('click', () => $('ntv-file').click());

$('ntv-file').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  $('ntv-result').innerHTML = '<p class="hint">Reading playlist…</p>';
  const body = new FormData();
  body.append('files', file, file.name);
  try {
    const response = await fetch('/api/logos', { method: 'POST', body });
    if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
    const r = await response.json();
    $('ntv-result').innerHTML = `<p class="result-ok">✓ Imported ${r.imported_count}
      logo(s) from ${esc(file.name)}.</p>
      <p class="hint">The playlist names the artwork rather than containing it, so those
      URLs had to be reachable from the server.</p>`;
    setBadge('ntv-badge', r.imported_count ? 'ok' : 'err', `${r.imported_count} logos`);
    state.logoVersion += 1;
    loadLogos(); loadChannels();
  } catch (err) {
    $('ntv-result').innerHTML = `<p class="result-err">✗ ${esc(err.message)}</p>`;
  } finally {
    event.target.value = '';
  }
});

$('logo-import-btn').addEventListener('click', () => $('logo-files').click());

$('logo-m3u-btn').addEventListener('click', async () => {
  const url = '';  // blank uses the playlist URL saved in Settings
  const button = $('logo-m3u-btn');
  button.disabled = true;
  $('logo-result').innerHTML = '<p class="hint">Fetching playlist and artwork…</p>';
  try {
    const r = await api('/api/logos/from-m3u', { method: 'POST', body: JSON.stringify({ url }) });
    const bits = [`<p class="result-ok">✓ Imported ${r.imported_count} channel logo(s).</p>`];
    if (r.unmatched_count) {
      bits.push(`<p class="hint">${r.unmatched_count} entries in the playlist are not
        NostalgiaTV channels (live TV, etc.) and were ignored.</p>`);
    }
    if (r.skipped_count) {
      bits.push(`<p class="hint">${r.skipped_count} could not be fetched:
        ${r.skipped.slice(0, 4).map((x) => `${esc(x.file)} (${esc(x.why)})`).join(', ')}</p>`);
    }
    state.logoVersion += 1;
    $('logo-result').innerHTML = bits.join('');
    loadLogos(); loadChannels();
  } catch (err) {
    $('logo-result').innerHTML = `<p class="result-err">✗ ${esc(err.message)}</p>`;
  } finally {
    button.disabled = false;
  }
});

$('logo-files').addEventListener('change', async (event) => {
  const files = [...event.target.files];
  if (!files.length) return;
  $('logo-result').innerHTML = `<p class="hint">Uploading ${files.length} file(s)…</p>`;
  const body = new FormData();
  files.forEach((f) => body.append('files', f, f.name));
  try {
    const response = await fetch('/api/logos', { method: 'POST', body });
    if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
    const r = await response.json();
    const bits = [`<p class="result-ok">✓ Imported ${r.imported_count} logo(s).</p>`];
    if (r.unmatched_count) {
      bits.push(`<p class="hint">${r.unmatched_count} matched no channel:
        ${r.unmatched.slice(0, 8).map(esc).join(', ')}${r.unmatched_count > 8 ? '…' : ''}</p>`);
    }
    if (r.skipped_count) {
      bits.push(`<p class="hint">${r.skipped_count} skipped:
        ${r.skipped.slice(0, 5).map((s) => `${esc(s.file)} (${esc(s.why)})`).join(', ')}</p>`);
    }
    state.logoVersion += 1;  // bust the browser cache for replaced artwork
    $('logo-result').innerHTML = bits.join('');
    loadLogos();
    loadChannels();
  } catch (err) {
    $('logo-result').innerHTML = `<p class="result-err">✗ ${esc(err.message)}</p>`;
  } finally {
    event.target.value = '';
  }
});

$('logo-clear-btn').addEventListener('click', async () => {
  if (!confirm('Remove all imported channel artwork?')) return;
  const r = await api('/api/logos', { method: 'DELETE' });
  state.logoVersion += 1;
  $('logo-result').innerHTML = `<p class="hint">Removed ${r.removed} file(s).</p>`;
  loadLogos(); loadChannels();
});

/* ── channels ──────────────────────────────────────────────── */
async function loadChannels() {
  const data = await api('/api/channels').catch(() => null);
  if (!data) return;
  state.channels = data.channels.filter((c) => c.accepts_content);
  renderChannels(data.channels);
  loadLogos();
}

function renderChannels(channels) {
  const hideNoContent = $('hide-nocontent').checked;
  const onlyProblem = $('only-problem').checked;
  const max = Math.max(1, ...channels.map((c) => c.total));
  const rows = channels
    .filter((c) => (!hideNoContent || c.accepts_content))
    .filter((c) => (!onlyProblem || c.empty || c.thin))
    .map((c) => {
      const pct = Math.round((c.total / max) * 100);
      const hot = c.total > max * 0.6 ? 'hot' : '';
      const tag = c.empty ? '<span class="tag tag-empty">empty</span>'
        : c.thin ? '<span class="tag tag-thin">thin</span>' : '';
      return `<tr class="is-clickable" data-channel="${c.number}">
        <td class="num">${c.number}</td>
        <td class="col-art"><img class="art-logo art-logo-${esc(c.logo_source || 'badge')}"
          loading="lazy" decoding="async" alt=""
          src="${esc(logoUrl(c.number))}?v=${state.logoVersion}"></td>
        <td class="title-cell">${esc(c.name)} ${tag}</td>
        <td class="col-cat">${esc(c.category)}${c.accepts_content ? '' : ' · no content'}</td>
        <td class="num">${c.existing}</td>
        <td class="num">${c.added ? `+${c.added}` : ''}</td>
        <td class="num">${c.total}</td>
        <td><div class="bar"><span class="${hot}" style="width:${pct}%"></span></div></td>
      </tr>`;
    }).join('');
  $('channel-body').innerHTML = rows || '<tr><td colspan="8" class="empty">Nothing matches.</td></tr>';
}

['hide-nocontent', 'only-problem'].forEach((id) => $(id).addEventListener('change', loadChannels));

/* ── custom stations ───────────────────────────────────────── */
async function loadStations() {
  const data = await api('/api/stations').catch(() => null);
  if (!data) return;

  $('station-problems').innerHTML = data.problems.length
    ? `<div class="notice-block">${data.problems.map(esc).join('<br>')}</div>`
    : '';
  $('st-number').placeholder = `auto (${data.next_number})`;

  $('station-list').innerHTML = data.stations.map((s) => `
    <div class="card">
      <h4>${s.number} · ${esc(s.name)}</h4>
      <div class="meta">${esc(s.mode)}${s.enabled ? '' : ' · disabled'}</div>
      ${s.source_networks.length ? `<div>Networks: ${s.source_networks.map(esc).join(', ')}</div>` : ''}
      ${s.source_channels.length ? `<div>Borrows: ${s.source_channels.join(', ')}</div>` : ''}
      ${s.keywords.length ? `<div>Keywords: ${s.keywords.map(esc).join(', ')}</div>` : ''}
      <div class="card-actions"><button class="btn btn-small" data-del-station="${s.number}">Delete</button></div>
    </div>`).join('') || '<p class="empty">No custom stations yet.</p>';

  $('station-list').querySelectorAll('[data-del-station]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      await api(`/api/stations/${btn.dataset.delStation}`, { method: 'DELETE' });
      state.channels = [];
      loadStations();
    });
  });
}

$('station-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = {
    number: $('st-number').value ? Number($('st-number').value) : null,
    name: $('st-name').value.trim(),
    source_networks: splitList($('st-networks').value),
    source_channels: splitList($('st-channels').value).map(Number).filter((n) => !Number.isNaN(n)),
    keywords: splitList($('st-keywords').value),
    mode: $('st-mode').value,
    enabled: true,
  };
  try {
    await api('/api/stations', { method: 'POST', body: JSON.stringify(payload) });
    event.target.reset();
    banner('Station saved. Re-scan to route content to it.', 'ok', 3500);
    state.channels = [];
    loadStations();
  } catch (err) {
    banner(err.message, 'err');
  }
});

/* ── settings ──────────────────────────────────────────────── */
function syncSourceFields() {
  const source = $('source').value;
  $('source-plex').classList.toggle('is-hidden', source !== 'plex');
  $('source-jellyfin').classList.toggle('is-hidden', source !== 'jellyfin');
}

$('source').addEventListener('change', syncSourceFields);

async function loadSettings() {
  const settings = await api('/api/settings').catch(() => null);
  if (!settings) return;
  $('source').value = settings.source || 'plex';
  syncSourceFields();
  $('plex-url').value = settings.plex_url || '';
  $('plex-libraries').value = (settings.plex_libraries || []).join(', ');
  $('plex-token').placeholder = settings.plex_token_set ? '•••••• (saved)' : 'required';
  $('jellyfin-url').value = settings.jellyfin_url || '';
  $('jellyfin-libraries').value = (settings.jellyfin_libraries || []).join(', ');
  $('jellyfin-key').placeholder = settings.jellyfin_api_key_set ? '•••••• (saved)' : 'required';
  $('tmdb-key').placeholder = settings.tmdb_api_key_set ? '•••••• (saved)' : 'required';
  $('ntv-m3u').value = settings.nostalgiatv_m3u_url || '';
  $('ntv-auto').checked = settings.auto_refresh_logos !== false;
  $('routing-mode').value = settings.routing_mode;
  $('multi-channel').value = settings.multi_channel;
  $('orphan-network').value = settings.orphan_network;
}

$('save-settings').addEventListener('click', async () => {
  const payload = {
    source: $('source').value,
    plex_url: $('plex-url').value.trim(),
    plex_libraries: splitList($('plex-libraries').value),
    jellyfin_url: $('jellyfin-url').value.trim(),
    jellyfin_libraries: splitList($('jellyfin-libraries').value),
    nostalgiatv_m3u_url: $('ntv-m3u').value.trim(),
    auto_refresh_logos: $('ntv-auto').checked,
    routing_mode: $('routing-mode').value,
    multi_channel: $('multi-channel').value,
    orphan_network: $('orphan-network').value,
  };
  if ($('plex-token').value.trim()) payload.plex_token = $('plex-token').value.trim();
  if ($('jellyfin-key').value.trim()) payload.jellyfin_api_key = $('jellyfin-key').value.trim();
  if ($('tmdb-key').value.trim()) payload.tmdb_api_key = $('tmdb-key').value.trim();
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify(payload) });
    $('plex-token').value = '';
    $('jellyfin-key').value = '';
    $('tmdb-key').value = '';
    banner('Settings saved.', 'ok', 2500);
    loadSettings(); refreshStatus();
  } catch (err) {
    banner(err.message, 'err');
  }
});

function setBadge(id, state, text) {
  const el = $(id);
  el.dataset.state = state;
  el.textContent = text;
}

$('test-server').addEventListener('click', async () => {
  setBadge('server-badge', 'testing', 'testing…');
  $('server-result').innerHTML = '';
  try {
    const r = await api('/api/test/server', { method: 'POST' });
    if (r.ok) {
      setBadge('server-badge', 'ok', 'connected');
      $('server-result').innerHTML =
        `<p class="result-ok">✓ ${esc(r.name)} ${esc(r.version)} — ${esc(r.detail)}</p>
         <ul>${r.sections.map((x) => `<li>${esc(x.title)} <span class="muted">(${esc(x.type)})</span></li>`).join('')}</ul>`;
    } else {
      setBadge('server-badge', 'err', 'failed');
      $('server-result').innerHTML = `<p class="result-err">✗ ${esc(r.error)}</p>`;
    }
  } catch (err) {
    setBadge('server-badge', 'err', 'failed');
    $('server-result').innerHTML = `<p class="result-err">✗ ${esc(err.message)}</p>`;
  }
});

$('test-tmdb').addEventListener('click', async () => {
  setBadge('tmdb-badge', 'testing', 'testing…');
  $('tmdb-result').innerHTML = '';
  try {
    const r = await api('/api/test/tmdb', { method: 'POST' });
    if (r.ok) {
      setBadge('tmdb-badge', 'ok', 'key valid');
      $('tmdb-result').innerHTML = `<p class="result-ok">✓ Key accepted — ${esc(r.detail)}</p>`;
    } else {
      setBadge('tmdb-badge', 'err', 'failed');
      $('tmdb-result').innerHTML = `<p class="result-err">✗ ${esc(r.error)}</p>`;
    }
  } catch (err) {
    setBadge('tmdb-badge', 'err', 'failed');
    $('tmdb-result').innerHTML = `<p class="result-err">✗ ${esc(err.message)}</p>`;
  }
});

$('clear-posters').addEventListener('click', async () => {
  try {
    const r = await api('/api/posters/clear', { method: 'POST' });
    banner(`Cleared ${r.removed} cached poster(s).`, 'ok', 2500);
    refreshStatus();
  } catch (err) { banner(err.message, 'err'); }
});

// Both Import buttons (header and Settings) drive the one hidden file input.
$('import-btn').addEventListener('click', () => $('channels-file').click());
$('import-btn-2').addEventListener('click', () => $('channels-file').click());

$('channels-file').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  $('upload-result').innerHTML = '<p class="hint">Validating…</p>';
  try {
    const text = await file.text();
    const result = await api('/api/channels-file', {
      method: 'POST', body: text, raw: true,
    });
    $('upload-result').innerHTML = `<div class="result-block">
      <p class="result-ok">✓ Loaded ${result.rows} assignments across ${result.channels} channels
        (was ${result.previous_rows}).</p>
      <p class="hint">${esc(result.sanctioned_pairs)} sanctioned pairings rebuilt from your file.
        Previous file backed up. Re-scan to diff against it.</p></div>`;
    refreshStatus(); loadLibrary();
  } catch (err) {
    $('upload-result').innerHTML = `<p class="result-err">✗ ${esc(err.message)}</p>`;
  } finally {
    event.target.value = '';
  }
});

/* ── export ────────────────────────────────────────────────── */
$('export-btn').addEventListener('click', async () => {
  $('export-downloads').classList.add('is-hidden');
  await refreshExportPreview();
  $('export-dialog').showModal();
});

$('export-review').addEventListener('change', refreshExportPreview);

async function refreshExportPreview() {
  const include = $('export-review').checked;
  $('export-preview').innerHTML = '<p class="hint">Calculating…</p>';
  try {
    const p = await api(`/api/export/preview?include_review=${include}`);
    const top = p.top_channels
      .map((c) => `<li>${c.number} ${esc(c.name)} — ${c.rows} row(s)</li>`).join('');
    $('export-preview').innerHTML = `
      <div class="stats">
        <div class="stat good"><b>${p.additions}</b><span>New rows</span></div>
        <div class="stat"><b>${p.secondary_rows}</b><span>Secondary</span></div>
        <div class="stat ${p.skipped_review ? 'warn' : ''}"><b>${p.skipped_review}</b><span>Held for review</span></div>
        <div class="stat"><b>${p.merged_rows}</b><span>Merged total</span></div>
      </div>
      <p class="hint">Your ${p.original_rows} existing rows are preserved untouched.</p>
      ${top ? `<p class="hint"><strong>Most affected channels</strong></p><ul class="mini">${top}</ul>` : ''}`;
  } catch (err) {
    $('export-preview').innerHTML = `<p class="result-err">${esc(err.message)}</p>`;
  }
}

$('export-form').addEventListener('submit', async (event) => {
  if (event.submitter?.value !== 'write') return;
  event.preventDefault();
  try {
    const report = await api('/api/export', {
      method: 'POST',
      body: JSON.stringify({ include_review: $('export-review').checked }),
    });
    $('export-preview').innerHTML = `<p class="result-ok">✓ Wrote ${report.additions} new rows.
      Merged file has ${report.merged_rows} rows.</p>
      <p class="hint">${esc(report.merged_path)}</p>`;
    $('export-downloads').classList.remove('is-hidden');
  } catch (err) {
    banner(err.message, 'err');
  }
});

/* ── boot ──────────────────────────────────────────────────── */
refreshStatus();
loadLibrary();
setInterval(refreshStatus, 5000);

// ============================================================================
// Hermes Kanban Dashboard — Clean Build
// ============================================================================

const bus = (() => {
  const listeners = {};
  return {
    on(event, fn) { (listeners[event] = listeners[event] || []).push(fn); },
    off(event, fn) { if (listeners[event]) listeners[event] = listeners[event].filter(h => h !== fn); },
    emit(event, data) { for (const fn of (listeners[event] || [])) { try { fn(data); } catch(_){} } },
  };
})();

let selected = null;
let currentBoard = new URLSearchParams(window.location.search).get('board') || 'chatgpt-extension';
let currentProfile = new URLSearchParams(window.location.search).get('profile') || 'default';
let pendingProfile = currentProfile;
const OVERSEER_PROFILE = 'overseer';
let chatSessionId = null;

let filterStatus = new URLSearchParams(window.location.search).get('filter') || 'all';
let filterQuery = new URLSearchParams(window.location.search).get('q') || '';
let sortBy = new URLSearchParams(window.location.search).get('sort') || 'created';
let sortAsc = new URLSearchParams(window.location.search).get('dir') === 'asc';
let allTasks = [];

function syncUrl() {
  const url = new URL(window.location.href);
  if (currentBoard) url.searchParams.set('board', currentBoard); else url.searchParams.delete('board');
  if (currentProfile) url.searchParams.set('profile', currentProfile); else url.searchParams.delete('profile');
  if (filterStatus && filterStatus !== 'all') url.searchParams.set('filter', filterStatus); else url.searchParams.delete('filter');
  if (filterQuery) url.searchParams.set('q', filterQuery); else url.searchParams.delete('q');
  window.history.replaceState({}, '', url.toString());
}

async function api(path, params = {}, method = 'GET', body = null) {
  const url = new URL(path, window.location.origin);
  for (const [k, v] of Object.entries(params)) { if (v != null) url.searchParams.set(k, String(v)); }
  const opts = { method, headers: {} };
  if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${r.statusText}`);
  return r.json();
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

const ALL_STATUSES = ['done', 'running', 'blocked', 'ready', 'scheduled', 'triage', 'todo', 'archived'];

function statusBadge(s) {
  const k = String(s).toLowerCase();
  return `<span class="badge badge-${k}">${esc(k)}</span>`;
}

function stateIcon(st) {
  const icons = { 'done': '✓', 'running': '●', 'blocked': '⊘', 'ready': '▶', 'scheduled': '◷', 'triage': '?', 'todo': '◻', 'archived': '▣' };
  return icons[st] || st;
}

async function refresh() {
  try {
    const [listData, profiles, boardsData] = await Promise.all([
      api('/api/list', { board: currentBoard }),
      api('/api/profiles'),
      api('/api/boards'),
    ]);
    
    const tasks = listData.tasks || [];
    allTasks = tasks;
    
    const current = boardsData.current_board || {};
    document.getElementById('boardName').textContent = current.name || current.slug || currentBoard;
    
    const counts = {};
    for (const t of tasks) { counts[t.status] = (counts[t.status] || 0) + 1; }
    document.getElementById('summary').innerHTML = `${tasks.length} tasks · ${counts.done || 0} done · ${counts.running || 0} running`;
    
    renderBoardSelector(boardsData.boards || []);
    renderProfileSelector(profiles);
    renderTasks(tasks);
    bus.emit('board-loaded', { board: currentBoard, tasks });
  } catch (err) {
    document.getElementById('summary').textContent = `Error: ${err.message}`;
    console.error('refresh error:', err);
  }
}

function renderBoardSelector(boards) {
  const sel = document.getElementById('boardSelect');
  sel.innerHTML = '';
  for (const b of boards) {
    const opt = document.createElement('option');
    opt.value = b.slug;
    opt.textContent = b.archived ? `📁 ${b.name}` : b.name;
    if (b.slug === currentBoard) opt.selected = true;
    sel.appendChild(opt);
  }
}

function renderProfileSelector(profiles) {
  const sel = document.getElementById('profileSelect');
  sel.innerHTML = '';
  for (const p of profiles) {
    const opt = document.createElement('option');
    opt.value = p.name;
    opt.textContent = `${p.name} (${p.model || '?'})`;
    if (p.name === currentProfile) opt.selected = true;
    sel.appendChild(opt);
  }
}

function renderTasks(tasks) {
  const tbody = document.getElementById('tasks');
  if (!tbody) return;
  tbody.innerHTML = '';
  
  let filtered = tasks;
  if (filterStatus !== 'all') filtered = filtered.filter(t => t.status === filterStatus);
  if (filterQuery) {
    const q = filterQuery.toLowerCase();
    filtered = filtered.filter(t => (t.title || '').toLowerCase().includes(q) || (t.id || '').toLowerCase().includes(q));
  }
  
  const coll = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
  filtered.sort((a, b) => {
    let cmp = 0;
    if (sortBy === 'title') cmp = coll.compare(a.title || '', b.title || '');
    else if (sortBy === 'status') cmp = coll.compare(a.status || '', b.status || '');
    else if (sortBy === 'assignee') cmp = coll.compare(a.assignee || '', b.assignee || '');
    return sortAsc ? cmp : -cmp;
  });
  
  for (const t of filtered) {
    const tr = document.createElement('tr');
    tr.dataset.id = t.id;
    tr.innerHTML = `
      <td class="state-cell">${stateIcon(t.state || t.status)}</td>
      <td class="id-cell"><code>${esc(t.id || '')}</code></td>
      <td>${statusBadge(t.status)}</td>
      <td class="assignee-cell">${esc(t.assignee || '—')}</td>
      <td class="title-cell">${esc(t.title || '')}</td>`;
    tr.addEventListener('click', () => selectTask(t, tr));
    tbody.appendChild(tr);
  }
}

function selectTask(task, row) {
  document.querySelectorAll('#tasks tr.selected').forEach(r => r.classList.remove('selected'));
  row.classList.add('selected');
  selected = task;
  bus.emit('task-selected', task);
}

async function createTask(title, body, opts = {}) {
  if (!title.trim()) throw new Error('Title required');
  const result = await api('/api/create-task', {}, 'POST', {
    title: title.trim(),
    body: body || '',
    profile: opts.profile || OVERSEER_PROFILE,
    assignee: opts.profile || OVERSEER_PROFILE,
    board: currentBoard,
    parent_task_id: opts.parent || null,
    chat_session_id: chatSessionId || null,
  });
  await refresh();
  return result;
}

async function sendChat(message) {
  if (!message.trim()) throw new Error('Message required');
  const r = await api('/api/chat', {}, 'POST', {
    board: currentBoard,
    profile: currentProfile,
    message: message.trim(),
    session_id: chatSessionId,
  });
  chatSessionId = r.session_id || chatSessionId;
  return r;
}

// ============================================================================
// Event bindings
// ============================================================================

document.getElementById('boardSelect').addEventListener('change', async (e) => {
  currentBoard = e.target.value;
  syncUrl();
  selected = null;
  await refresh();
});

document.getElementById('profileSelect').addEventListener('change', (e) => {
  pendingProfile = e.target.value;
  const stateEl = document.getElementById('profileState');
  if (stateEl) stateEl.textContent = `Active: ${pendingProfile}`;
});

document.getElementById('profileConfirmBtn')?.addEventListener('click', () => {
  currentProfile = pendingProfile;
  syncUrl();
  const stateEl = document.getElementById('profileState');
  if (stateEl) stateEl.textContent = `Active: ${currentProfile}`;
});

document.getElementById('refreshBtn').addEventListener('click', refresh);

document.getElementById('searchInput').addEventListener('input', (e) => {
  filterQuery = e.target.value;
  renderTasks(allTasks);
});

document.getElementById('sortSelect')?.addEventListener('change', (e) => {
  sortBy = e.target.value;
  renderTasks(allTasks);
});

document.getElementById('sortDirToggle')?.addEventListener('click', () => {
  sortAsc = !sortAsc;
  document.getElementById('sortDirToggle').textContent = sortAsc ? '▲' : '▼';
  renderTasks(allTasks);
});

// Filter chips
document.querySelectorAll('.filter-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    filterStatus = chip.dataset.status || 'all';
    renderTasks(allTasks);
  });
});

// ============================================================================
// Bootstrap
// ============================================================================
async function boot() {
  console.log('[Kanban Dashboard] booting...');
  window.bus = bus;
  await refresh();
  setInterval(refresh, 30000);
  console.log('[Kanban Dashboard] ready');
}

document.addEventListener('DOMContentLoaded', boot);

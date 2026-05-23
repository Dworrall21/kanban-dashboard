// ============================================================================
// Event Bus — lightweight pub/sub for decoupled module communication
// ============================================================================
const bus = (() => {
  const listeners = {};
  return {
    on(event, fn) {
      (listeners[event] = listeners[event] || []).push(fn);
    },
    off(event, fn) {
      if (!listeners[event]) return;
      listeners[event] = listeners[event].filter(h => h !== fn);
    },
    emit(event, data) {
      for (const fn of (listeners[event] || [])) {
        try { fn(data); } catch (_) { /* ignore handler errors */ }
      }
    },
  };
})();

// ============================================================================
// State — shared configuration (not module-internal state)
// ============================================================================
let selected = null;
let currentBoard = new URLSearchParams(window.location.search).get('board') || 'chatgpt-extension';
let currentProfile = new URLSearchParams(window.location.search).get('profile') || 'default';
let pendingProfile = currentProfile;
const OVERSEER_PROFILE = 'overseer';
let chatSessionId = null;  // set after first chat message, used to link tasks

// ---- Filter / Search / Sort state ----
let filterStatus = new URLSearchParams(window.location.search).get('filter') || 'all';
let filterQuery = new URLSearchParams(window.location.search).get('q') || '';
let sortBy = new URLSearchParams(window.location.search).get('sort') || 'created';
let sortAsc = new URLSearchParams(window.location.search).get('dir') === 'asc';
let allTasks = [];  // cache the full task list from the last refresh

function syncUrl() {
  const url = new URL(window.location.href);
  if (currentBoard) url.searchParams.set('board', currentBoard); else url.searchParams.delete('board');
  if (currentProfile) url.searchParams.set('profile', currentProfile); else url.searchParams.delete('profile');
  if (filterStatus && filterStatus !== 'all') url.searchParams.set('filter', filterStatus); else url.searchParams.delete('filter');
  if (filterQuery) url.searchParams.set('q', filterQuery); else url.searchParams.delete('q');
  if (sortBy && sortBy !== 'created') url.searchParams.set('sort', sortBy); else url.searchParams.delete('sort');
  if (sortAsc) url.searchParams.set('dir', 'asc'); else url.searchParams.delete('dir');
  window.history.replaceState({}, '', url.toString());
}

async function api(path, params = {}, method = 'GET', body = null) {
  const url = new URL(path, window.location.origin);
  if (currentBoard) url.searchParams.set('board', currentBoard);
  if (currentProfile) url.searchParams.set('profile', currentProfile);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
  }
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  return fetch(url.toString(), opts);
}

// ============================================================================
// Chat Module — mounts from template, owns its DOM via this.root
// ============================================================================
const chat = (() => {
  let history = [];
  let root = null;
  let currentSessionId = chatSessionId;
  let _sessions = [];  // cached session list

  function q(sel) { return root ? root.querySelector(sel) : null; }
  function qAll(sel) { return root ? root.querySelectorAll(sel) : []; }

  function storageKey() {
    return `kanban-chat-sessions:${currentBoard || 'mobile-web-dashboard-chat'}`;
  }

  function oldStorageKey() {
    return `kanban-chat-history:${currentBoard || 'mobile-web-dashboard-chat'}`;
  }

  function sessionsStorageKey() {
    return `kanban-chat-sessions-meta:${currentBoard || 'mobile-web-dashboard-chat'}`;
  }

  function hashString(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) {
      h = ((h << 5) - h) + s.charCodeAt(i);
      h |= 0;
    }
    return h;
  }

  function loadFromStorage() {
    try {
      // Migrate from old key format
      const oldKey = oldStorageKey();
      const oldRaw = localStorage.getItem(oldKey);
      const newKey = storageKey();
      if (oldRaw && !localStorage.getItem(newKey)) {
        localStorage.setItem(newKey, oldRaw);
        localStorage.removeItem(oldKey);
      }
      const raw = localStorage.getItem(newKey);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      // Support both legacy array and new session format
      if (Array.isArray(parsed)) return parsed.filter(Boolean);
      return [];
    } catch (_) { return []; }
  }

  function saveToStorage() {
    try { localStorage.setItem(storageKey(), JSON.stringify(history.slice(-60))); } catch (_) {}
  }

  function loadSessionsMeta() {
    try {
      const raw = localStorage.getItem(sessionsStorageKey());
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) { return []; }
  }

  function saveSessionsMeta(sessions) {
    try { localStorage.setItem(sessionsStorageKey(), JSON.stringify(sessions)); } catch (_) {}
  }

  function render() {
    const log = q('.chat-log');
    if (!log) return;
    log.innerHTML = '';
    for (const msg of history) {
      const wrap = document.createElement('div');
      wrap.className = `chat-msg ${msg.role}`;
      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      meta.textContent = msg.role === 'user' ? 'You' : msg.role === 'assistant' ? 'Hermes' : 'System';
      const body = document.createElement('div');
      body.textContent = msg.content;
      wrap.appendChild(meta);
      wrap.appendChild(body);
      log.appendChild(wrap);
    }
    log.scrollTop = log.scrollHeight;
  }

  function statusDetail() {
    const turns = history.filter(m => m.role === 'user' || m.role === 'assistant').length;
    const lastUser = [...history].reverse().find(m => m.role === 'user');
    const lastAssistant = [...history].reverse().find(m => m.role === 'assistant');
    const clip = (msg) => {
      if (!msg) return 'none';
      return String(msg.content || '').replace(/\s+/g, ' ').trim().slice(0, 140) || 'none';
    };
    return [
      `board=${currentBoard}`,
      `profile=${currentProfile}${pendingProfile && pendingProfile !== currentProfile ? ` (staged=${pendingProfile})` : ''}`,
      `session=${currentSessionId || 'none'}`,
      `turns=${turns}`,
      `last_user=${clip(lastUser)}`,
      `last_assistant=${clip(lastAssistant)}`,
    ].join(' | ');
  }

  function setStatus(text) {
    const el = q('.chat-status');
    if (el) el.textContent = text;
  }

  function setDetail(text) {
    const el = q('.chat-detail');
    if (el) el.textContent = text;
  }

  function liveLog() { return q('.chat-live'); }
  function liveWrap() { return q('.chat-live-wrap'); }
  function liveHeader() { return q('.chat-live-header'); }
  function liveToggle() { return q('.chat-live-toggle'); }

  function appendLiveLine(text, cls) {
    const live = liveLog();
    if (!live) return;
    const span = document.createElement('span');
    span.className = 'line' + (cls ? ' ' + cls : '');
    span.textContent = text;
    live.appendChild(span);
    live.scrollTop = live.scrollHeight;
  }

  function clearLive() {
    const live = liveLog();
    if (live) live.innerHTML = '';
  }

  function expandLive() {
    const live = liveLog();
    const toggle = liveToggle();
    if (live) live.classList.remove('collapsed');
    if (toggle) toggle.textContent = 'Hide';
  }

  function collapseLive() {
    const live = liveLog();
    const toggle = liveToggle();
    if (live) live.classList.add('collapsed');
    if (toggle) toggle.textContent = 'Show';
  }

  function updateSessionBadge() {
    const badge = q('#chatSessionBadge');
    const nameEl = q('.chat-session-name');
    if (badge) {
      const sessionData = _sessions.find(s => s.id === currentSessionId);
      const label = sessionData ? (sessionData.name || sessionData.id) : (currentSessionId || 'new');
      const shortId = currentSessionId ? currentSessionId.slice(0, 8) : 'new';
      badge.textContent = `${history.length} msgs · ${shortId}`;
    }
    if (nameEl) {
      const sessionData = _sessions.find(s => s.id === currentSessionId);
      nameEl.textContent = sessionData ? `Session: ${sessionData.name || sessionData.id}` : 'Session: new';
    }
  }

  async function loadSessionsFromServer() {
    try {
      const r = await api('/api/chat-sessions');
      const d = await r.json();
      _sessions = Array.isArray(d.sessions) ? d.sessions : [];
      saveSessionsMeta(_sessions);
      return _sessions;
    } catch (_) {
      _sessions = loadSessionsMeta();
      return _sessions;
    }
  }

  async function switchSession(sessionId) {
    try {
      const r = await api('/api/chat-sessions/switch', {}, 'POST', {
        board: currentBoard,
        session_id: sessionId,
      });
      const d = await r.json();
      if (d.switched) {
        history = Array.isArray(d.history) ? d.history : [];
        currentSessionId = sessionId;
        chatSessionId = sessionId;
        saveToStorage();
        render();
        setStatus('Ready');
        setDetail(statusDetail());
        updateSessionBadge();
        await loadSessionsFromServer();
        renderSessionPanel();
        closeSessionPanel();
        bus.emit('chat:session-switched', { session_id: sessionId });
      }
    } catch (err) {
      console.error('Failed to switch session:', err);
    }
  }

  async function renameSession(sessionId, newName) {
    try {
      const r = await api('/api/chat-sessions', {}, 'POST', {
        board: currentBoard,
        action: 'rename',
        session_id: sessionId,
        name: newName,
      });
      const d = await r.json();
      if (d.renamed) {
        await loadSessionsFromServer();
        renderSessionPanel();
        updateSessionBadge();
      }
    } catch (err) {
      console.error('Failed to rename session:', err);
    }
  }

  async function deleteSession(sessionId) {
    if (!confirm(`Delete this session and all its messages? This cannot be undone.`)) return;
    try {
      const r = await api('/api/chat-sessions', { session: sessionId }, 'DELETE');
      const d = await r.json();
      if (d.deleted) {
        if (currentSessionId === sessionId) {
          // Switch to current session from server
          const newCurrent = d.current_session;
          if (newCurrent) {
            await switchSession(newCurrent);
          } else {
            history = [];
            currentSessionId = null;
            chatSessionId = null;
            saveToStorage();
            render();
            setStatus('Ready');
            setDetail(statusDetail());
            updateSessionBadge();
          }
        }
        await loadSessionsFromServer();
        renderSessionPanel();
      }
    } catch (err) {
      console.error('Failed to delete session:', err);
    }
  }

  function downloadExport(data, filename, mimeType) {
    const blob = new Blob([data], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  async function exportSession(format) {
    const params = { format: format };
    if (currentSessionId) params.session = currentSessionId;
    try {
      const r = await api('/api/chat-export', params);
      if (!r.ok) { alert('Export failed'); return; }
      const blob = await r.blob();
      const disposition = r.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="?([^"]+)"?/);
      const filename = match ? match[1] : `chat-export.${format === 'markdown' ? 'md' : 'json'}`;
      downloadExport(blob, filename, r.headers.get('Content-Type') || 'application/octet-stream');
    } catch (err) {
      console.error('Export failed:', err);
      alert('Export failed');
    }
  }

  function renderSessionPanel() {
    const list = document.getElementById('chatSessionList');
    if (!list) return;
    if (_sessions.length === 0) {
      list.innerHTML = '<div class="session-empty">No saved sessions yet. Send a message to start one.</div>';
      return;
    }
    list.innerHTML = '';
    for (const s of _sessions) {
      const item = document.createElement('div');
      item.className = 'session-item' + (s.is_current ? ' current' : '');
      const lastMsg = s.last_message || {};
      const lastPreview = lastMsg.content
        ? String(lastMsg.content).replace(/\s+/g, ' ').trim().slice(0, 80) + (String(lastMsg.content).length > 80 ? '…' : '')
        : '(empty)';
      const ts = s.updated_at || s.created_at || 0;
      const dateStr = ts ? new Date(ts * 1000).toLocaleString() : 'unknown';
      const roleLabel = lastMsg.role === 'user' ? 'You' : lastMsg.role === 'assistant' ? 'Hermes' : '';
      item.innerHTML = `
        <div class="session-item-name">
          <span>${s.name || s.id}</span>
          <span class="small">${s.message_count} msgs</span>
        </div>
        <div class="session-item-meta">${dateStr}</div>
        <div class="session-item-preview">${roleLabel ? roleLabel + ': ' : ''}${lastPreview}</div>
        <div class="session-item-actions">
          <button class="button secondary session-switch-btn" data-sid="${s.id}" type="button">Switch</button>
          <button class="button secondary session-rename-btn" data-sid="${s.id}" type="button">Rename</button>
          <button class="button secondary session-delete-btn" data-sid="${s.id}" type="button" style="color:#f85149;">Delete</button>
        </div>`;
      // Switch click on the item itself
      item.querySelector('.session-switch-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        switchSession(s.id);
      });
      // Rename
      item.querySelector('.session-rename-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        const nameSpan = item.querySelector('.session-item-name span');
        const currentName = s.name || s.id;
        const input = document.createElement('input');
        input.className = 'session-rename-input';
        input.type = 'text';
        input.value = currentName;
        input.autofocus = true;
        nameSpan.replaceWith(input);
        input.focus();
        input.select();
        const finishRename = () => {
          const newName = input.value.trim() || currentName;
          renameSession(s.id, newName);
        };
        input.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter') { input.blur(); }
          if (ev.key === 'Escape') { input.value = currentName; input.blur(); }
        });
        input.addEventListener('blur', finishRename);
      });
      // Delete
      item.querySelector('.session-delete-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        deleteSession(s.id);
      });
      list.appendChild(item);
    }
  }

  function openSessionPanel() {
    const overlay = document.getElementById('chatSessionOverlay');
    const panel = document.getElementById('chatSessionPanel');
    if (overlay) overlay.classList.add('open');
    if (panel) panel.classList.add('open');
    loadSessionsFromServer().then(() => renderSessionPanel());
  }

  function closeSessionPanel() {
    const overlay = document.getElementById('chatSessionOverlay');
    const panel = document.getElementById('chatSessionPanel');
    if (overlay) overlay.classList.remove('open');
    if (panel) panel.classList.remove('open');
  }

  function toggleExportMenu(e) {
    e.stopPropagation();
    const menu = document.getElementById('chatExportMenu');
    if (menu) menu.classList.toggle('open');
  }

  function closeExportMenu() {
    const menu = document.getElementById('chatExportMenu');
    if (menu) menu.classList.remove('open');
  }

  return {
    mount(mountPoint) {
      const tpl = document.getElementById('tpl-chat-composer');
      if (!tpl) return;
      const clone = tpl.content.cloneNode(true);
      mountPoint.appendChild(clone);
      root = mountPoint.firstElementChild;
      // Bind events within this component's root
      q('.chat-send-btn').addEventListener('click', () => {
        const input = q('.chat-input');
        const msg = input.value.trim();
        if (msg) { input.value = ''; chat.send(msg); }
      });
      q('.chat-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          const input = q('.chat-input');
          const msg = input.value.trim();
          if (msg) { input.value = ''; chat.send(msg); }
        }
      });
      q('.chat-clear-btn').addEventListener('click', () => chat.clear());
      // Session panel
      q('.chat-sessions-btn').addEventListener('click', () => openSessionPanel());
      q('.chat-session-name').addEventListener('click', () => openSessionPanel());
      // Export
      q('.chat-export-btn').addEventListener('click', (e) => toggleExportMenu(e));
      qAll('.chat-export-option').forEach(el => {
        el.addEventListener('click', (e) => {
          e.stopPropagation();
          closeExportMenu();
          exportSession(el.dataset.format);
        });
      });
      // Live log toggle
      const header = q('.chat-live-header');
      if (header) {
        header.addEventListener('click', () => {
          const live = liveLog();
          const toggle = liveToggle();
          if (!live) return;
          if (live.classList.contains('collapsed')) {
            live.classList.remove('collapsed');
            if (toggle) toggle.textContent = 'Hide';
          } else {
            live.classList.add('collapsed');
            if (toggle) toggle.textContent = 'Show';
          }
        });
      }
      // Session panel close
      const panelClose = document.getElementById('chatSessionPanelClose');
      if (panelClose) panelClose.addEventListener('click', closeSessionPanel);
      const panelOverlay = document.getElementById('chatSessionOverlay');
      if (panelOverlay) panelOverlay.addEventListener('click', closeSessionPanel);
      history = loadFromStorage();
      render();
    },

    unmount() {
      if (root && root.parentNode) root.parentNode.removeChild(root);
      root = null;
    },

    init() {
      history = loadFromStorage();
      currentSessionId = chatSessionId;
      render();
      setStatus('Ready');
      setDetail(statusDetail());
      updateSessionBadge();
      // Fetch server-side history, merge new messages, re-render
      chat._syncFromServer().catch(() => {});  // best-effort, silent failures
      // Load sessions in background
      loadSessionsFromServer();
    },

    async _syncFromServer() {
      try {
        const r = await api('/api/chat-history', {}, 'GET');
        const d = await r.json();
        const serverHistory = Array.isArray(d.history) ? d.history : [];
        const existingKeys = new Set(
          history.map(m => `${m.ts}|${hashString(String(m.content || ''))}`),
        );
        const toAdd = serverHistory.filter(m => {
          const k = `${m.ts}|${hashString(String(m.content || ''))}`;
          return !existingKeys.has(k);
        });
        if (toAdd.length > 0) {
          history.push(...toAdd);
          saveToStorage();
          render();
        }
      } catch (_) {
        // server unavailable — local storage is the source of truth
      }
    },

    async send(message) {
      const raw = String(message ?? '');
      if (!raw.trim()) return null;
      history.push({ role: 'user', content: raw, ts: Date.now() });
      saveToStorage();
      render();
      setStatus('Thinking…');
      setDetail(`Thinking…\n${statusDetail()}`);
      bus.emit('chat:append', { role: 'user', content: raw });
      clearLive();

      const payload = {
        board: currentBoard,
        profile: currentProfile,
        history: history.slice(0, -1),
        message: raw,
      };
      if (currentSessionId) payload.session_id = currentSessionId;
      try {
        const r = await api('/api/chat', {}, 'POST', payload);
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Chat failed');
        const reply = d.reply || '';
        history.push({ role: 'assistant', content: reply, ts: Date.now() });
        saveToStorage();
        render();
        setStatus(`${currentProfile} · ${currentBoard}`);
        setDetail(statusDetail());
        // Populate agent log with raw CLI output
        if (d.raw) {
          const live = liveLog();
          if (live) {
            live.innerHTML = '';
            for (const line of String(d.raw).split('\n')) {
              const cls = line.toLowerCase().includes('error') ? 'err'
                        : line.toLowerCase().includes('success') || line.includes('✓') ? 'ok'
                        : line.startsWith('🤖') || line.includes('Model:') || line.includes('initialized') ? 'info'
                        : '';
              appendLiveLine(line, cls);
            }
          }
        }
        if (d.chat_session_id) {
          chatSessionId = d.chat_session_id;
          currentSessionId = d.chat_session_id;
        }
        updateSessionBadge();
        // Refresh session list in background
        loadSessionsFromServer();
        bus.emit('chat:append', { role: 'assistant', content: reply });
        return d;
      } catch (err) {
        history.push({ role: 'system', content: String(err && err.message ? err.message : err) });
        saveToStorage();
        render();
        setStatus('Error');
        setDetail(`Error\n${statusDetail()}`);
        bus.emit('chat:error', { error: err });
        throw err;
      }
    },

    clear() {
      // Create a new session rather than destroying data
      history = [];
      saveToStorage();
      render();
      currentSessionId = null;
      chatSessionId = null;
      setStatus('Ready');
      setDetail(statusDetail());
      clearLive();
      updateSessionBadge();
      bus.emit('chat:clear', {});
      // Reload sessions list
      loadSessionsFromServer();
    },

    getHistory() { return history.slice(); },
    getSessionId() { return currentSessionId; },
    getStorageKey() { return storageKey(); },
    statusDetail,
    setStatus,
    setDetail,
    render,
  };
})();
window.chat = chat;
// ============================================================================
// Task Module — mounts from template, owns its DOM via this.root
// ============================================================================
const task = (() => {
  let root = null;
  let taskProfile = OVERSEER_PROFILE;
  let taskPendingProfile = OVERSEER_PROFILE;

  function q(sel) { return root ? root.querySelector(sel) : null; }

  function setStatus(text) {
    const el = q('.task-status');
    if (el) el.textContent = text;
  }

  function setDetail(text) {
    const el = q('.task-detail');
    if (el) el.textContent = text;
  }

  function taskStatusDetail() {
    const selCtx = selectedTaskContext();
    return [
      `board=${currentBoard}`,
      `profile=${taskProfile}${taskPendingProfile !== taskProfile ? ` (staged=${taskPendingProfile})` : ''}`,
      `selected=${selCtx ? selCtx.id : 'none'}`,
    ].join(' | ');
  }

  function stageTaskProfile(nextProfile) {
    if (!nextProfile) return;
    taskPendingProfile = nextProfile;
    const btn = q('.task-profile-confirm-btn');
    const state = q('.task-profile-state');
    if (btn) btn.textContent = taskPendingProfile === taskProfile ? 'Profile active' : `Confirm ${taskPendingProfile}`;
    if (state) state.textContent = taskPendingProfile === taskProfile ? `Active: ${taskProfile}` : `Staged: ${taskPendingProfile}`;
    setDetail(taskStatusDetail());
  }

  function applyTaskProfile() {
    if (!taskPendingProfile || taskPendingProfile === taskProfile) return;
    taskProfile = taskPendingProfile;
    const btn = q('.task-profile-confirm-btn');
    const state = q('.task-profile-state');
    if (state) state.textContent = `Active: ${taskProfile}`;
    if (btn) btn.textContent = 'Profile active';
    setDetail(taskStatusDetail());
  }

  function selectedTaskContext() {
    const label = document.getElementById('detailsLabel');
    const details = document.getElementById('details');
    const labelText = label ? String(label.textContent || '').trim() : '';
    if (!labelText || labelText === 'Select a task') return null;
    const m = labelText.match(/^(t_[a-z0-9]+)\s+—\s+(.+)$/i);
    return {
      id: m ? m[1] : (selected || labelText),
      title: m ? m[2].trim() : labelText,
      raw: details ? String(details.textContent || '') : '',
    };
  }

  return {
    mount(mountPoint) {
      const tpl = document.getElementById('tpl-task-composer');
      if (!tpl) return;
      const clone = tpl.content.cloneNode(true);
      mountPoint.appendChild(clone);
      root = mountPoint.firstElementChild;
      q('.task-create-btn').addEventListener('click', () => task.createFromFields());
      q('.task-from-selection-btn').addEventListener('click', () => task.createFromSelection());
      q('.task-from-chat-btn').addEventListener('click', () => task.createWithChatSession());
      const taskProfSel = q('.task-profile-select');
      if (taskProfSel) {
        taskProfSel.addEventListener('change', (e) => stageTaskProfile(e.target.value));
        if (taskProfSel.options.length > 0) taskProfSel.value = taskProfile;
      }
      const taskConfBtn = q('.task-profile-confirm-btn');
      if (taskConfBtn) taskConfBtn.addEventListener('click', () => applyTaskProfile());
      setDetail(taskStatusDetail());
    },

    unmount() {
      if (root && root.parentNode) root.parentNode.removeChild(root);
      root = null;
    },

    async create({ title, body, chatSessionId, parentTask }) {
      const t = String(title || '').trim();
      if (!t) { setStatus('Title required'); setDetail(taskStatusDetail()); return null; }
      const createBtn = q('.task-create-btn');
      const prevBtnText = createBtn ? createBtn.textContent : '';
      setStatus('Creating…');
      if (createBtn) {
        createBtn.disabled = true;
        createBtn.textContent = 'Creating…';
      }
      const payload = {
        board: currentBoard,
        profile: taskProfile || OVERSEER_PROFILE,
        assignee: taskProfile || OVERSEER_PROFILE,
        title: t,
        body: String(body || '').trim(),
      };
      if (chatSessionId) payload.chat_session_id = chatSessionId;
      if (parentTask) payload.parent_task_id = parentTask;
      try {
        const r = await api('/api/create-task', {}, 'POST', payload);
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Task creation failed');
        setStatus(`Created ${d.task?.id || 'task'}`);
        if (createBtn) createBtn.textContent = 'Created';
        setDetail(taskStatusDetail());
        bus.emit('task:created', { task: d.task });
        return d.task;
      } catch (err) {
        setStatus('Error');
        if (createBtn) createBtn.textContent = 'Error';
        setDetail(taskStatusDetail());
        bus.emit('task:error', { error: err });
        throw err;
      } finally {
        setTimeout(() => {
          if (createBtn) {
            createBtn.disabled = false;
            createBtn.textContent = prevBtnText || 'Create task';
          }
          if (q('.task-status') && q('.task-status').textContent !== 'Ready') setStatus('Ready');
        }, 1200);
      }
    },

    async createFromFields() {
      const titleEl = q('.task-title-input');
      const bodyEl = q('.task-body-input');
      const title = titleEl ? titleEl.value.trim() : '';
      const body = bodyEl ? bodyEl.value.trim() : '';
      const result = await this.create({ title, body });
      if (result && titleEl) titleEl.value = '';
      if (result && bodyEl) bodyEl.value = '';
      return result;
    },

    async createFromSelection() {
      const sel = selectedTaskContext();
      if (!sel) { setStatus('Select a task first'); setDetail(taskStatusDetail()); return null; }
      const titleEl = q('.task-title-input');
      const bodyEl = q('.task-body-input');
      const title = titleEl && titleEl.value.trim() ? titleEl.value.trim() : sel.title;
      const body = bodyEl && bodyEl.value.trim()
        ? bodyEl.value.trim()
        : `Follow-up to ${sel.id}\n\n${sel.raw}`.trim();
      return this.create({ title, body, parentTask: sel.id });
    },

    async createWithChatSession() {
      const titleEl = q('.task-title-input');
      const bodyEl = q('.task-body-input');
      const title = titleEl ? titleEl.value.trim() : '';
      const body = bodyEl ? bodyEl.value.trim() : '';
      const sid = chat.getSessionId();
      if (!title) { setStatus('Add a title first'); setDetail(taskStatusDetail()); return null; }
      return this.create({ title, body, chatSessionId: sid });
    },
    statusDetail: taskStatusDetail,
    setStatus,
    setDetail,
  };
})();
window.task = task;
// ============================================================================
// Board / Profile / Task list — shared infrastructure
// ============================================================================
async function loadBoards() {
  const r = await api('/api/boards');
  const d = await r.json();
  const boards = Array.isArray(d.boards) ? d.boards : [];
  const sel = document.getElementById('boardSelect');
  sel.innerHTML = '';
  for (const b of boards) {
    const opt = document.createElement('option');
    opt.value = b.slug;
    opt.textContent = `${b.name || b.slug}${b.archived ? ' (archived)' : ''}`;
    sel.appendChild(opt);
  }
  if (!currentBoard || !boards.some(b => b.slug === currentBoard)) {
    currentBoard = (d.current_board && d.current_board.slug) || (boards[0] && boards[0].slug) || 'chatgpt-extension';
  }
  sel.value = currentBoard;
}

async function loadProfiles() {
  const r = await api('/api/profiles');
  const d = await r.json();
  const profiles = Array.isArray(d.profiles) ? d.profiles : [];
  const sel = document.getElementById('profileSelect');
  sel.innerHTML = '';
  const taskSels = document.querySelectorAll('.task-profile-select');
  taskSels.forEach(taskSel => { taskSel.innerHTML = ''; });
  for (const p of profiles) {
    const opt = document.createElement('option');
    opt.value = p.name;
    opt.textContent = `${p.name} — ${p.model || 'unknown model'}`;
    sel.appendChild(opt);
    taskSels.forEach(taskSel => {
      const taskOpt = document.createElement('option');
      taskOpt.value = p.name;
      taskOpt.textContent = `${p.name} — ${p.model || 'unknown model'}`;
      taskSel.appendChild(taskOpt);
    });
  }
  if (!currentProfile || !profiles.some(p => p.name === currentProfile)) {
    currentProfile = profiles.some(p => p.name === OVERSEER_PROFILE)
      ? OVERSEER_PROFILE
      : ((d.current_profile && d.current_profile.name) || (profiles[0] && profiles[0].name) || OVERSEER_PROFILE);
  }
  pendingProfile = currentProfile;
  sel.value = pendingProfile;
  taskSels.forEach(taskSel => {
    if (Array.from(taskSel.options).some(o => o.value === OVERSEER_PROFILE)) taskSel.value = OVERSEER_PROFILE;
  });
  const state = document.getElementById('profileState');
  if (state) state.textContent = `Active: ${currentProfile}`;
  const confirmBtn = document.getElementById('profileConfirmBtn');
  if (confirmBtn) confirmBtn.textContent = currentProfile === pendingProfile ? 'Profile active' : `Confirm ${pendingProfile}`;
}

async function refresh() {
  const r = await api('/api/list');
  const d = await r.json();
  document.getElementById('summary').textContent = d.summary || '';
  document.getElementById('boardName').textContent = d.board || currentBoard;
  allTasks = d.tasks || [];
  applyFilters();
  if (selected) loadTask(selected, true);
}

// ============================================================================
// DOM patching helpers for SSE events — avoid full re-render
// ============================================================================

function patchTask(taskId, updatedTask) {
  // Find existing row by data-task-id
  const row = document.querySelector(`#tasks tr[data-task-id="${taskId}"]`);
  if (!row) {
    // Task not in current view — fall back to full refresh
    refresh();
    return;
  }
  
  // Update status cell class and badge
  const statusCell = row.querySelector('td:first-child');
  const statusBadge = row.querySelector('.status-badge');
  const newStatus = updatedTask.status || 'todo';
  const newState = updatedTask.state || '';
  
  if (statusCell) {
    statusCell.className = `status-${newStatus}`;
  }
  if (statusBadge) {
    statusBadge.textContent = `${newState} ${newStatus}`;
  }
  
  // Update dataset for filter/sort consistency
  row.dataset.status = newStatus;
  row.dataset.title = updatedTask.title || '';
  
  // Update assignee cell (2nd td) and title cell (4th td)
  const cells = row.querySelectorAll('td');
  if (cells.length >= 4) {
    cells[1].textContent = updatedTask.id || taskId;
    cells[2].textContent = updatedTask.assignee || '';
    cells[3].textContent = updatedTask.title || '';
  }
  
  // If task is selected, reload its detail panel
  if (selected === taskId) {
    loadTask(taskId, true);
  }
}

function patchBoardSummary(summary, boardName) {
  const summaryEl = document.getElementById('summary');
  const boardNameEl = document.getElementById('boardName');
  if (summaryEl) summaryEl.textContent = summary || '';
  if (boardNameEl) boardNameEl.textContent = boardName || currentBoard;
}

async function refreshTaskList() {
  // Full refresh of task list only — used when patching isn't possible
  const r = await api('/api/list');
  const d = await r.json();
  allTasks = d.tasks || [];
  applyFilters();
  if (selected) loadTask(selected, true);
}

function applyFilters() {
  // Compute status counts from all tasks
  const counts = { all: allTasks.length };
  for (const t of allTasks) {
    const s = t.status || 'todo';
    counts[s] = (counts[s] || 0) + 1;
  }

  // Update count badges
  const statuses = ['all', 'ready', 'running', 'done', 'blocked', 'todo'];
  for (const s of statuses) {
    const el = document.getElementById('count-' + s);
    if (el) el.textContent = counts[s] || 0;
  }

  // Sync filter UI state from globals
  document.querySelectorAll('.filter-chip').forEach(chip => {
    chip.classList.toggle('active', chip.dataset.status === filterStatus);
  });
  const searchInput = document.getElementById('searchInput');
  if (searchInput) searchInput.value = filterQuery;
  const sortSelect = document.getElementById('sortSelect');
  if (sortSelect) sortSelect.value = sortBy;
  const sortToggle = document.getElementById('sortDirToggle');
  if (sortToggle) sortToggle.textContent = sortAsc ? '▼' : '▲';
  if (sortToggle) sortToggle.classList.toggle('active', sortAsc);

  // Filter and sort the tasks
  let filtered = allTasks;
  if (filterStatus && filterStatus !== 'all') {
    filtered = filtered.filter(t => (t.status || 'todo') === filterStatus);
  }
  if (filterQuery) {
    const q = filterQuery.toLowerCase();
    filtered = filtered.filter(t =>
      (t.title || '').toLowerCase().includes(q) ||
      (t.id || '').toLowerCase().includes(q)
    );
  }

  // Sort
  const dir = sortAsc ? 1 : -1;
  filtered.sort((a, b) => {
    let cmp = 0;
    if (sortBy === 'title') {
      cmp = (a.title || '').localeCompare(b.title || '');
    } else if (sortBy === 'status') {
      cmp = (a.status || '').localeCompare(b.status || '');
    } else {
      // Default: sort by created date (newest first by default)
      const da = a.created_at || a.id || '';
      const db = b.created_at || b.id || '';
      cmp = da < db ? -1 : da > db ? 1 : 0;
    }
    return cmp * dir;
  });

  // Render
  const tbody = document.getElementById('tasks');
  tbody.innerHTML = '';
  for (const t of filtered) {
    const tr = document.createElement('tr');
    tr.dataset.taskId = t.id;
    tr.dataset.status = t.status;
    tr.dataset.title = t.title;
    tr.innerHTML = `<td class="status-${t.status}"><span class="status-badge">${t.state} ${t.status}</span></td><td>${t.id}</td><td>${t.assignee}</td><td>${t.title}</td>`;
    tr.draggable = true;
    tr.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('text/plain', t.id);
      e.dataTransfer.effectAllowed = 'move';
      tr.classList.add('dragging');
    });
    tr.addEventListener('dragend', () => {
      tr.classList.remove('dragging');
      for (const row of tbody.querySelectorAll('tr')) row.classList.remove('drag-over');
    });
    tr.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      for (const row of tbody.querySelectorAll('tr')) row.classList.remove('drag-over');
      tr.classList.add('drag-over');
    });
    tr.addEventListener('dragleave', () => {
      tr.classList.remove('drag-over');
    });
    tr.addEventListener('drop', async (e) => {
      e.preventDefault();
      const draggedId = e.dataTransfer.getData('text/plain');
      const targetId = tr.dataset.taskId;
      if (draggedId === targetId) return;
      const rows = Array.from(tbody.querySelectorAll('tr'));
      const ids = rows.map(r => r.dataset.taskId);
      const fromIdx = ids.indexOf(draggedId);
      const toIdx = ids.indexOf(targetId);
      if (fromIdx < 0 || toIdx < 0) return;
      ids.splice(fromIdx, 1);
      ids.splice(toIdx, 0, draggedId);
      // Optimistic DOM reorder
      const draggedRow = rows[fromIdx];
      if (fromIdx < toIdx) {
        rows[toIdx].after(draggedRow);
      } else {
        rows[toIdx].before(draggedRow);
      }
      for (const row of tbody.querySelectorAll('tr')) row.classList.remove('drag-over');
      // Persist
      await api('/api/reorder', {}, 'POST', { board: currentBoard, order: ids });
    });
    tr.onclick = (e) => {
      if (e.target.closest('.status-dropdown')) return;
      loadTask(t.id);
    };
    tr.oncontextmenu = (e) => { showContextMenu(e, t.id, t.status, t.title); };
    // Long-press for mobile touch devices
    let longPressTimer = null;
    const LONG_PRESS_MS = 500;
    tr.addEventListener('touchstart', (e) => {
      if (e.target.closest('.status-dropdown')) return;
      longPressTimer = setTimeout(() => {
        longPressTimer = null;
        const touch = e.touches[0];
        showContextMenu({ clientX: touch.clientX, clientY: touch.clientY, preventDefault: () => {} }, t.id, t.status, t.title);
      }, LONG_PRESS_MS);
    }, { passive: true });
    tr.addEventListener('touchend', () => {
      if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    }, { passive: true });
    tr.addEventListener('touchmove', () => {
      if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    }, { passive: true });
    // Status dropdown on badge
    const badge = tr.querySelector('.status-badge');
    if (badge) {
      badge.style.cursor = 'pointer';
      badge.onclick = (e) => { e.stopPropagation(); showStatusDropdown(e, t.id, t.status, tr); };
    }
    tr.addEventListener('click', () => loadTask(t.id));
    tbody.appendChild(tr);
  }

  // Update summary with filtered count
  const summaryEl = document.getElementById('summary');
  if (summaryEl) {
    const total = allTasks.length;
    const shown = filtered.length;
    const base = summaryEl.textContent.replace(/\s+\(\d+\/\d+ shown\)$/, '');
    summaryEl.textContent = total > shown ? `${base} (${shown}/${total} shown)` : base;
  }
  renderBoardView(filtered);
}

function isMobile() { return window.innerWidth <= 768; }

function openDetailSheet(id, title, raw) {
  document.getElementById('detailSheetTitle').textContent = `${id} — ${title}`;
  document.getElementById('detailSheetBody').textContent = raw;
  document.getElementById('detailOverlay').classList.add('open');
  document.getElementById('detailSheet').classList.add('open');
  document.body.style.overflow = 'hidden';
  const actions = document.getElementById('detailActions');
  if (actions) actions.style.display = '';
  stopEditing();
}

function closeDetailSheet() {
  document.getElementById('detailOverlay').classList.remove('open');
  document.getElementById('detailSheet').classList.remove('open');
  document.body.style.overflow = '';
}

const detailCloseBtn = document.getElementById('detailSheetClose');
if (detailCloseBtn) detailCloseBtn.addEventListener('click', closeDetailSheet);
const detailOverlay = document.getElementById('detailOverlay');
if (detailOverlay) detailOverlay.addEventListener('click', closeDetailSheet);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDetailSheet(); });
async function loadTask(id, silent=false) {
  selected = id;
  const r = await api('/api/show/' + encodeURIComponent(id));
  const d = await r.json();
  // Show detail actions
  stopEditing();
  const actions = document.getElementById('detailActions');
  if (actions) actions.style.display = '';
  if (isMobile()) {
    openDetailSheet(d.id, d.title, d.raw);
  } else {
    document.getElementById('detailsLabel').textContent = `${d.id} — ${d.title}`;
    document.getElementById('details').textContent = d.raw;
  }
}

// ---------------------------------------------------------------------------
// Task lifecycle helpers
// ---------------------------------------------------------------------------
const ALL_STATUSES = ['todo','ready','running','done','blocked','scheduled','review','triage','archived'];
let editingTaskId = null;
let editingOriginal = { title: '', body: '' };

async function patchTask(id, fields) {
  const r = await api('/api/tasks/' + encodeURIComponent(id), {}, 'PATCH', { ...fields, board: currentBoard });
  return r;
}
async function deleteTask(id) {
  const r = await api('/api/tasks/' + encodeURIComponent(id), { board: currentBoard }, 'DELETE');
  return r;
}
async function transitionTask(id, status, reason) {
  const r = await api('/api/tasks/' + encodeURIComponent(id) + '/transition', { board: currentBoard }, 'POST', { status, reason });
  return r;
}

function showContextMenu(e, id, status, title) {
  e.preventDefault();
  closeAllMenus();
  const menu = document.getElementById('contextMenu');
  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  menu.classList.add('open');
  menu.dataset.taskId = id;
  menu.dataset.status = status;
  menu.dataset.title = title;
}
function hideContextMenu() {
  document.getElementById('contextMenu').classList.remove('open');
}

function showStatusDropdown(e, id, currentStatus, tr) {
  e.preventDefault();
  e.stopPropagation();
  closeAllMenus();
  // Remove any existing inline dropdown
  const existing = tr.querySelector('.status-dropdown');
  if (existing) existing.remove();
  const wrap = document.createElement('span');
  wrap.className = 'status-dropdown open';
  wrap.style.position = 'absolute';
  wrap.style.zIndex = '60';
  const menu = document.createElement('div');
  menu.className = 'status-dropdown-menu';
  menu.style.display = 'block';
  for (const s of ALL_STATUSES) {
    const item = document.createElement('div');
    item.className = 'status-dropdown-item' + (s === currentStatus ? ' active' : '');
    item.textContent = s;
    item.onclick = async (ev) => {
      ev.stopPropagation();
      const r = await transitionTask(id, s);
      if (r.ok) { refresh(); } else { const d = await r.json(); alert('Transition failed: ' + (d.error || 'unknown')); }
      wrap.remove();
    };
    menu.appendChild(item);
  }
  wrap.appendChild(menu);
  const badge = tr.querySelector('.status-badge');
  if (badge) {
    badge.style.position = 'relative';
    badge.appendChild(wrap);
  }
}

function closeAllMenus() {
  hideContextMenu();
  // Remove dynamically created inline status dropdowns (inside table rows)
  document.querySelectorAll('tr .status-dropdown.open').forEach(el => el.remove());
  // Close permanent dropdowns by removing the open class
  document.querySelectorAll('#detailStatusDropdown.open').forEach(el => el.classList.remove('open'));
  document.querySelectorAll('.status-dropdown-menu').forEach(el => { if (el.style.display === 'block') el.style.display = 'none'; });
}

// ============================================================================
// Kanban Board View — columns, drag-and-drop, view toggle
// ============================================================================
let currentView = localStorage.getItem('kanbanView') || 'table';

function statusLabel(status) {
  const labels = { todo: 'To Do', ready: 'Ready', running: 'Running', done: 'Done', blocked: 'Blocked' };
  return labels[status] || status;
}

function getStatusForColumn(colStatus) {
  // Map column status to task status (identical for our columns)
  return colStatus;
}

function renderBoardView(tasks) {
  // Clear all columns
  const columns = ['todo', 'ready', 'running', 'done', 'blocked'];
  for (const col of columns) {
    const body = document.getElementById('col-' + col);
    if (!body) continue;
    body.innerHTML = '';
    const filtered = tasks.filter(t => t.status === col);
    for (const t of filtered) {
      const card = createKanbanCard(t);
      body.appendChild(card);
    }
    // Update column count
    const countEl = document.getElementById('count-' + col + '-board');
    if (countEl) countEl.textContent = filtered.length;
  }
}

function createKanbanCard(t) {
  const card = document.createElement('div');
  card.className = 'kanban-card';
  card.draggable = true;
  card.dataset.taskId = t.id;
  card.dataset.status = t.status;
  card.dataset.title = t.title;

  // Title
  const title = document.createElement('div');
  title.className = 'card-title';
  title.textContent = t.title || '(untitled)';
  card.appendChild(title);

  // Preview (truncated body)
  const preview = document.createElement('div');
  preview.className = 'card-preview';
  // Use the raw body if available, otherwise just show nothing
  preview.textContent = t.body ? t.body.replace(/\s+/g, ' ').trim().slice(0, 100) : '';
  if (preview.textContent) card.appendChild(preview);

  // Meta row
  const meta = document.createElement('div');
  meta.className = 'card-meta';

  const idSpan = document.createElement('span');
  idSpan.className = 'card-id';
  idSpan.textContent = t.id;
  meta.appendChild(idSpan);

  if (t.assignee) {
    const assignee = document.createElement('span');
    assignee.className = 'card-assignee';
    assignee.textContent = t.assignee;
    meta.appendChild(assignee);
  }

  const badge = document.createElement('span');
  badge.className = 'card-status-badge ' + t.status;
  badge.textContent = t.status;
  meta.appendChild(badge);

  card.appendChild(meta);

  // Click to show details (skip if just dragged)
  card.addEventListener('click', (e) => {
    if (!card.classList.contains('dragging') && !card.dataset.wasDragged) loadTask(t.id);
    card.dataset.wasDragged = '';
  });

  // Context menu (right-click / long-press)
  card.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    showContextMenu(e, t.id, t.status, t.title);
  });

  // --- HTML5 Drag and Drop (desktop) ---
  card.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/plain', t.id);
    e.dataTransfer.effectAllowed = 'move';
    card.classList.add('dragging');
    // Custom drag image
    const ghost = card.cloneNode(true);
    ghost.className = 'kanban-drag-ghost';
    ghost.style.left = '-9999px';
    document.body.appendChild(ghost);
    e.dataTransfer.setDragImage(ghost, 20, 20);
    setTimeout(() => document.body.removeChild(ghost), 0);
    // Store drag source info
    dragSource = { id: t.id, status: t.status, el: card };
  });

  card.addEventListener('dragend', () => {
    card.classList.remove('dragging');
    card.dataset.wasDragged = '1';
    document.querySelectorAll('.kanban-column-body.drag-over').forEach(el => el.classList.remove('drag-over'));
    dragSource = null;
  });

  // --- Touch drag (mobile) ---
  let touchState = null;
  const LONG_PRESS_MS = 300;

  card.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    const touch = e.touches[0];
    touchState = {
      startX: touch.clientX,
      startY: touch.clientY,
      moved: false,
      timer: setTimeout(() => {
        // Long press initiates drag
        if (!touchState) return;
        touchState.active = true;
        card.classList.add('dragging');
        dragSource = { id: t.id, status: t.status, el: card };
        // Create a visual clone that follows the finger
        const clone = card.cloneNode(true);
        clone.className = 'kanban-drag-ghost';
        clone.style.left = (touch.clientX - 20) + 'px';
        clone.style.top = (touch.clientY - 20) + 'px';
        clone.id = 'touchDragGhost';
        document.body.appendChild(clone);
      }, LONG_PRESS_MS),
    };
  }, { passive: true });

  card.addEventListener('touchmove', (e) => {
    if (!touchState) return;
    const touch = e.touches[0];
    const dx = Math.abs(touch.clientX - touchState.startX);
    const dy = Math.abs(touch.clientY - touchState.startY);
    if (dx > 10 || dy > 10) {
      touchState.moved = true;
      if (touchState.timer) {
        clearTimeout(touchState.timer);
        touchState.timer = null;
      }
    }
    if (touchState.active) {
      e.preventDefault();
      // Move ghost
      const ghost = document.getElementById('touchDragGhost');
      if (ghost) {
        ghost.style.left = (touch.clientX - 20) + 'px';
        ghost.style.top = (touch.clientY - 20) + 'px';
      }
      // Highlight drop target
      document.querySelectorAll('.kanban-column-body').forEach(body => {
        const rect = body.getBoundingClientRect();
        if (touch.clientX >= rect.left && touch.clientX <= rect.right &&
            touch.clientY >= rect.top && touch.clientY <= rect.bottom) {
          body.classList.add('drag-over');
        } else {
          body.classList.remove('drag-over');
        }
      });
    }
  }, { passive: false });

  card.addEventListener('touchend', async (e) => {
    if (!touchState) return;
    if (touchState.timer) {
      clearTimeout(touchState.timer);
    }
    if (touchState.active && dragSource) {
      // Find which column we dropped on
      const touch = e.changedTouches[0];
      let targetCol = null;
      document.querySelectorAll('.kanban-column-body').forEach(body => {
        const rect = body.getBoundingClientRect();
        if (touch.clientX >= rect.left && touch.clientX <= rect.right &&
            touch.clientY >= rect.top && touch.clientY <= rect.bottom) {
          targetCol = body;
        }
      });
      // Remove ghost
      const ghost = document.getElementById('touchDragGhost');
      if (ghost) ghost.remove();
      card.classList.remove('dragging');
      document.querySelectorAll('.kanban-column-body.drag-over').forEach(el => el.classList.remove('drag-over'));
      if (targetCol) {
        const col = targetCol.closest('.kanban-column');
        if (col) {
          const targetStatus = col.dataset.status;
          if (targetStatus !== dragSource.status) {
            card.dataset.wasDragged = '1';
            const r = await transitionTask(t.id, targetStatus);
            if (r.ok) { refresh(); }
            else {
              const d = await r.json();
              console.warn('Touch drop failed:', d.error);
            }
          } else {
            card.dataset.wasDragged = '1';
          }
        }
      }
    }
    touchState = null;
    dragSource = null;
  }, { passive: true });

  return card;
}

let dragSource = null;

// Set up drop zones on columns (idempotent — only add listeners once)
let kanbanDropZonesSetup = false;
function setupKanbanDropZones() {
  if (kanbanDropZonesSetup) return;
  kanbanDropZonesSetup = true;
  document.querySelectorAll('.kanban-column-body').forEach(body => {
    body.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      body.classList.add('drag-over');
    });

    body.addEventListener('dragleave', () => {
      body.classList.remove('drag-over');
    });

    body.addEventListener('drop', async (e) => {
      e.preventDefault();
      body.classList.remove('drag-over');
      const taskId = e.dataTransfer.getData('text/plain');
      if (!taskId || !dragSource) return;
      const col = body.closest('.kanban-column');
      if (!col) return;
      const targetStatus = col.dataset.status;
      if (targetStatus === dragSource.status) return; // same column
      const r = await transitionTask(taskId, targetStatus);
      if (r.ok) { refresh(); }
      else {
        const d = await r.json();
        console.warn('Drop transition failed:', d.error);
      }
      dragSource = null;
    });
  });
}

function toggleView(view) {
  currentView = view;
  localStorage.setItem('kanbanView', view);
  const tableView = document.getElementById('tableView');
  const boardView = document.getElementById('kanbanBoard');
  const btns = document.querySelectorAll('#viewToggle button');
  btns.forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  if (view === 'board') {
    tableView.classList.remove('active');
    tableView.classList.add('hidden');
    boardView.classList.add('active');
  } else {
    tableView.classList.add('active');
    tableView.classList.remove('hidden');
    boardView.classList.remove('active');
  }
}

// Setup view toggle — use same pattern as existing boot code
function initKanbanView() {
  const viewBtns = document.querySelectorAll('#viewToggle button');
  viewBtns.forEach(btn => {
    btn.addEventListener('click', () => toggleView(btn.dataset.view));
  });
  // Initialize from stored preference
  if (currentView === 'board') {
    toggleView('board');
  }
  setupKanbanDropZones();
}

// Also re-setup drop zones when board becomes visible after refresh
bus.on('task:created', () => { setTimeout(setupKanbanDropZones, 0); });

// Defer init until DOM ready (matches pattern used by bootKanbanUI)
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initKanbanView, { once: true });
} else {
  initKanbanView();
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('.context-menu')) hideContextMenu();
  if (!e.target.closest('.status-dropdown')) closeAllMenus();
  if (!e.target.closest('.chat-export-menu') && !e.target.closest('.chat-export-btn')) {
    const menu = document.getElementById('chatExportMenu');
    if (menu) menu.classList.remove('open');
  }
});
document.addEventListener('touchstart', (e) => {
  if (!e.target.closest('.context-menu')) hideContextMenu();
  if (!e.target.closest('.status-dropdown')) closeAllMenus();
  if (!e.target.closest('.chat-export-menu') && !e.target.closest('.chat-export-btn')) {
    const menu = document.getElementById('chatExportMenu');
    if (menu) menu.classList.remove('open');
  }
}, { passive: true });

const contextMenu = document.getElementById('contextMenu');
if (contextMenu) contextMenu.addEventListener('click', async (e) => {
  const item = e.target.closest('.context-menu-item');
  if (!item) return;
  const menu = document.getElementById('contextMenu');
  const id = menu.dataset.taskId;
  const action = item.dataset.action;
  hideContextMenu();
  if (!id) return;
  if (action === 'edit') {
    const r = await api('/api/show/' + encodeURIComponent(id));
    const d = await r.json();
    startEditing(id, d.title, d.raw);
  } else if (action === 'done') {
    const r = await transitionTask(id, 'done');
    if (r.ok) refresh(); else { const d = await r.json(); alert('Failed: ' + (d.error || 'unknown')); }
  } else if (action === 'blocked') {
    const reason = prompt('Block reason (optional):');
    if (reason === null) return;
    const r = await transitionTask(id, 'blocked', reason);
    if (r.ok) refresh(); else { const d = await r.json(); alert('Failed: ' + (d.error || 'unknown')); }
  } else if (action === 'ready') {
    const r = await transitionTask(id, 'ready');
    if (r.ok) refresh(); else { const d = await r.json(); alert('Failed: ' + (d.error || 'unknown')); }
  } else if (action === 'delete') {
    if (!confirm(`Delete task ${id}? This cannot be undone.`)) return;
    const r = await deleteTask(id);
    if (r.ok) { selected = null; refresh(); } else { const d = await r.json(); alert('Failed: ' + (d.error || 'unknown')); }
  }
});

// Detail sheet inline editing
function startEditing(id, title, rawBody) {
  editingTaskId = id;
  editingOriginal = { title: title || '', body: rawBody || '' };
  const titleInput = document.getElementById('detailSheetTitleInput');
  const bodyInput = document.getElementById('detailSheetBodyInput');
  const bodyPre = document.getElementById('detailSheetBody');
  const actions = document.getElementById('detailActions');
  titleInput.value = editingOriginal.title;
  bodyInput.value = editingOriginal.body;
  titleInput.style.display = '';
  bodyInput.style.display = '';
  bodyPre.style.display = 'none';
  actions.style.display = '';
  document.getElementById('detailEditBtn').style.display = 'none';
  document.getElementById('detailDeleteBtn').style.display = 'none';
  document.getElementById('detailStatusDropdown').style.display = 'none';
  document.getElementById('detailSaveBtn').style.display = '';
  document.getElementById('detailCancelBtn').style.display = '';
}

function stopEditing() {
  editingTaskId = null;
  const titleInput = document.getElementById('detailSheetTitleInput');
  const bodyInput = document.getElementById('detailSheetBodyInput');
  const bodyPre = document.getElementById('detailSheetBody');
  const actions = document.getElementById('detailActions');
  titleInput.style.display = 'none';
  bodyInput.style.display = 'none';
  bodyPre.style.display = '';
  actions.style.display = '';
  document.getElementById('detailEditBtn').style.display = '';
  document.getElementById('detailDeleteBtn').style.display = '';
  document.getElementById('detailStatusDropdown').style.display = '';
  document.getElementById('detailSaveBtn').style.display = 'none';
  document.getElementById('detailCancelBtn').style.display = 'none';
}

const detailEditBtn = document.getElementById('detailEditBtn');
if (detailEditBtn) detailEditBtn.addEventListener('click', async () => {
  if (!selected) return;
  const r = await api('/api/show/' + encodeURIComponent(selected));
  const d = await r.json();
  startEditing(selected, d.title, d.raw);
});

const detailSaveBtn = document.getElementById('detailSaveBtn');
if (detailSaveBtn) detailSaveBtn.addEventListener('click', async () => {
  if (!editingTaskId) return;
  const title = document.getElementById('detailSheetTitleInput').value.trim();
  const body = document.getElementById('detailSheetBodyInput').value.trim();
  const r = await patchTask(editingTaskId, { title, body });
  if (r.ok) {
    stopEditing();
    refresh();
    if (selected) loadTask(selected);
  } else {
    const d = await r.json(); alert('Save failed: ' + (d.error || 'unknown'));
  }
});

const detailCancelBtn = document.getElementById('detailCancelBtn');
if (detailCancelBtn) detailCancelBtn.addEventListener('click', () => { stopEditing(); });

const detailDeleteBtn = document.getElementById('detailDeleteBtn');
if (detailDeleteBtn) detailDeleteBtn.addEventListener('click', async () => {
  if (!selected) return;
  if (!confirm(`Delete task ${selected}? This cannot be undone.`)) return;
  const r = await deleteTask(selected);
  if (r.ok) { selected = null; closeDetailSheet(); refresh(); }
  else { const d = await r.json(); alert('Delete failed: ' + (d.error || 'unknown')); }
});

// Detail sheet status dropdown
const detailStatusBtn = document.getElementById('detailStatusBtn');
const detailStatusMenu = document.getElementById('detailStatusMenu');
if (detailStatusBtn && detailStatusMenu) {
  ALL_STATUSES.forEach(s => {
    const item = document.createElement('div');
    item.className = 'status-dropdown-item';
    item.textContent = s;
    item.onclick = async () => {
      if (!selected) return;
      const r = await transitionTask(selected, s);
      if (r.ok) { refresh(); if (selected) loadTask(selected); }
      else { const d = await r.json(); alert('Transition failed: ' + (d.error || 'unknown')); }
    };
    detailStatusMenu.appendChild(item);
  });
  detailStatusBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const wrap = document.getElementById('detailStatusDropdown');
    wrap.classList.toggle('open');
  });
}

function setBoard(nextBoard) {
  if (!nextBoard || nextBoard === currentBoard) return;
  currentBoard = nextBoard;
  selected = null;
  chatSessionId = null;
  document.getElementById('boardName').textContent = currentBoard;
  chat.init();  // reload chat history for new board
  syncUrl();
  refresh();
}

function stageProfile(nextProfile) {
  if (!nextProfile) return;
  pendingProfile = nextProfile;
  const confirmBtn = document.getElementById('profileConfirmBtn');
  if (confirmBtn) confirmBtn.textContent = pendingProfile === currentProfile ? 'Profile active' : `Confirm ${pendingProfile}`;
  const state = document.getElementById('profileState');
  if (state) state.textContent = pendingProfile === currentProfile ? `Active: ${currentProfile}` : `Staged: ${pendingProfile} (active: ${currentProfile})`;
  if (chat && typeof chat.setDetail === 'function') chat.setDetail(chat.statusDetail());
  if (window.task && typeof window.task.setDetail === 'function') window.task.setDetail(window.task.statusDetail());
  if (window.promptComposer && typeof window.promptComposer.setDetail === 'function') window.promptComposer.setDetail(window.promptComposer.statusDetail());
}

function applyProfile() {
  if (!pendingProfile || pendingProfile === currentProfile) return;
  currentProfile = pendingProfile;
  syncUrl();
  chat.setStatus(`${currentProfile} · ${currentBoard}`);
  chat.setDetail(chat.statusDetail());
  if (window.task && typeof window.task.setDetail === 'function') window.task.setDetail(window.task.statusDetail());
  if (window.promptComposer && typeof window.promptComposer.setDetail === 'function') window.promptComposer.setDetail(window.promptComposer.statusDetail());
  const state = document.getElementById('profileState');
  if (state) state.textContent = `Active: ${currentProfile}`;
  const confirmBtn = document.getElementById('profileConfirmBtn');
  if (confirmBtn) confirmBtn.textContent = 'Profile active';
  refresh();
}

// ============================================================================
// PromptComposer — mounts from template, owns its DOM via this.root
// ============================================================================
const promptComposer = (() => {
  let root = null;
  function q(sel) { return root ? root.querySelector(sel) : null; }
  function setStatus(text) { const el = q('.prompt-status'); if (el) el.textContent = text; }
  function setDetail(text) { const el = q('.prompt-detail'); if (el) el.textContent = text; }
  function promptStatusDetail() {
    const input = q('.prompt-input');
    const isSecret = input ? input.type === 'password' : true;
    return [
      `board=${currentBoard}`,
      `profile=${currentProfile}${pendingProfile && pendingProfile !== currentProfile ? ' (staged=' + pendingProfile + ')' : ''}`,
      `secret=${isSecret ? 'hidden' : 'visible'}`,
    ].join(' | ');
  }
  async function sendResponse(message, displayContent) {
    const input = q('.prompt-input');
    const response = String(message !== undefined ? message : (input ? input.value : '') || '');
    if (!response.trim()) return;
    if (input) input.value = '';
    try {
      await window.chat.send(displayContent || response);
      setStatus('Ready');
      setDetail(promptStatusDetail());
    } catch (_) { setStatus('Error'); setDetail(promptStatusDetail()); }
  }
  return {
    mount(mountPoint) {
      const tpl = document.getElementById('tpl-prompt-composer');
      if (!tpl) return;
      const clone = tpl.content.cloneNode(true);
      mountPoint.appendChild(clone);
      root = mountPoint.firstElementChild;
      q('.prompt-approve-btn').addEventListener('click', () => sendResponse('y', 'Prompt response: y'));
      q('.prompt-deny-btn').addEventListener('click', () => sendResponse('n', 'Prompt response: n'));
      q('.prompt-enter-btn').addEventListener('click', () => sendResponse('[prompt response: enter]', 'Prompt response: Enter'));
      q('.prompt-send-btn').addEventListener('click', () => {
        const input = q('.prompt-input');
        if (!input || !input.value.trim()) return;
        const display = input.type === 'password' ? 'Prompt response: [hidden secret]' : 'Prompt response: ' + input.value;
        sendResponse(input.value, display);
      });
      q('.prompt-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); q('.prompt-send-btn').click(); }
      });
      q('.prompt-secret-toggle').addEventListener('click', () => {
        const input = q('.prompt-input');
        const btn = q('.prompt-secret-toggle');
        const next = input.type === 'password' ? 'text' : 'password';
        input.type = next;
        btn.textContent = next === 'password' ? 'Show' : 'Hide';
        setDetail(promptStatusDetail());
      });
      setStatus('Ready');
      setDetail(promptStatusDetail());
    },
    unmount() { if (root && root.parentNode) root.parentNode.removeChild(root); root = null; },
    setStatus, setDetail, sendResponse,
    statusDetail: promptStatusDetail,
  };
})();
window.promptComposer = promptComposer;

// ============================================================================
// Event bindings — static elements only (board/profile selectors, detail sheet)
// ============================================================================
document.getElementById('boardSelect').addEventListener('change', (e) => setBoard(e.target.value));
document.getElementById('profileSelect').addEventListener('change', (e) => stageProfile(e.target.value));
document.getElementById('profileConfirmBtn').addEventListener('click', () => applyProfile());
document.getElementById('refreshBtn').addEventListener('click', () => refresh());
document.getElementById('editProfilesBtn').addEventListener('click', () => profileEditor.open());

// ---- Filter / Search / Sort event bindings ----
// Debounced search
let searchDebounceTimer = null;
document.getElementById('searchInput').addEventListener('input', (e) => {
  clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    filterQuery = e.target.value;
    syncUrl();
    applyFilters();
  }, 250);
});

// Filter chips
document.querySelectorAll('.filter-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const status = chip.dataset.status;
    if (status === 'all') {
      filterStatus = 'all';
    } else {
      filterStatus = filterStatus === status ? 'all' : status;
    }
    syncUrl();
    applyFilters();
  });
});

// Sort select
document.getElementById('sortSelect').addEventListener('change', (e) => {
  sortBy = e.target.value;
  syncUrl();
  applyFilters();
});

// Sort direction toggle
document.getElementById('sortDirToggle').addEventListener('click', () => {
  sortAsc = !sortAsc;
  syncUrl();
  applyFilters();
});

// Cross-module event wiring (the ONLY place modules talk to each other)
bus.on('task:created', async ({ task: t }) => {
  await refresh();
  if (window.task && typeof window.task.setDetail === 'function') window.task.setDetail(window.task.statusDetail());
});

// ============================================================================
// Init — mount template components, then load data
// ============================================================================
function bootKanbanUI() {
  const chatApp = window.chat;
  const taskApp = window.task;
  const promptApp = window.promptComposer;
  if (!chatApp || !taskApp || !promptApp) {
    console.error('bootKanbanUI: app modules not ready');
    return;
  }
  chatApp.mount(document.getElementById('chatComposerMount'));
  taskApp.mount(document.getElementById('taskComposerMount'));
  promptApp.mount(document.getElementById('promptComposerMount'));
  if (window.profileEditor && typeof window.profileEditor.init === 'function') {
    window.profileEditor.init();
  }
  loadBoards()
    .then(() => loadProfiles())
    .then(() => {
      syncUrl();
      chatApp.init();
      return refresh();
    })
    .then(() => {
      chatApp.setStatus(`${currentProfile} · ${currentBoard}`);
      chatApp.setDetail(chatApp.statusDetail());
      taskApp.setDetail(taskApp.statusDetail());
      promptApp.setStatus('Ready');
      promptApp.setDetail(promptApp.statusDetail());
    })
    .catch((err) => {
      console.error('bootKanbanUI failed', err);
    });
}

if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', bootKanbanUI, { once: true });
} else {
  bootKanbanUI();
}

// ============================================================================
// SSE — real-time event stream, replaces polling
// ============================================================================
let eventSource = null;

function connectSSE() {
  stopPolling();  // clear any stale fallback timer
  if (eventSource) {
    eventSource.close();
  }
  const params = new URLSearchParams({ board: currentBoard, profile: currentProfile });
  eventSource = new EventSource('/api/events?' + params.toString());

  eventSource.addEventListener('connected', (e) => {
    const data = JSON.parse(e.data);
    console.log('SSE connected:', data.board, data.profile);
    stopPolling();  // SSE is live — stop the fallback
    document.querySelector('.small:last-child')?.replaceWith(
      Object.assign(document.createElement('span'), { className: 'small', textContent: 'live via SSE' })
    );
  });

  eventSource.addEventListener('task-changed', async (e) => {
    const data = JSON.parse(e.data);
    console.log('SSE task-changed:', data.action, data.task_id);
    
    // Fetch updated task list and find the changed task
    try {
      const r = await api('/api/list');
      if (r.ok) {
        const d = await r.json();
        const updatedTask = (d.tasks || []).find(t => t.id === data.task_id);
        if (updatedTask) {
          patchTask(data.task_id, updatedTask);
        } else {
          // Task not found in list — full refresh
          refreshTaskList();
        }
      } else {
        refreshTaskList();
      }
    } catch (err) {
      console.warn('task-changed patch failed, falling back to refresh:', err);
      refreshTaskList();
    }
  });

  eventSource.addEventListener('chat-message', (e) => {
    const data = JSON.parse(e.data);
    console.log('SSE chat-message:', data.board, data.session_id);
    const chatApp = window.chat;
    if (chatApp && typeof chatApp.init === 'function') {
      chatApp.init();
    }
  });

  eventSource.addEventListener('board-update', async (e) => {
    console.log('SSE board-update');
    // Fetch updated board data and patch summary/name inline
    try {
      const r = await api('/api/list');
      if (r.ok) {
        const d = await r.json();
        patchBoardSummary(d.summary || '', d.board || currentBoard);
        // Also refresh the task list since board changed
        refreshTaskList();
      } else {
        refresh();
      }
    } catch (err) {
      console.warn('board-update patch failed, falling back to refresh:', err);
      refresh();
    }
  });

  eventSource.addEventListener('profile-changed', (e) => {
    console.log('SSE profile-changed');
    loadProfiles();
  });

  eventSource.onerror = () => {
    console.warn('SSE connection error, falling back to adaptive polling');
    document.querySelector('.small:last-child')?.replaceWith(
      Object.assign(document.createElement('span'), { className: 'small', textContent: 'SSE disconnected, polling...' })
    );
    // EventSource auto-reconnects — close old one to be safe
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    // Start adaptive polling fallback instead of fixed reconnect
    pollErrorStreak = 0;
    pollTick();
  };
}

// Kick off SSE after bootKanbanUI finishes
const origBoot = bootKanbanUI;
bootKanbanUI = function() {
  origBoot.apply(this, arguments);
  // Wait a beat for initial load, then connect SSE
  setTimeout(connectSSE, 500);
};
// Also update label on reconnect after board/profile change
const origSetBoard = setBoard;
setBoard = function(nextBoard) {
  origSetBoard.apply(this, arguments);
  connectSSE();
};
const origApplyProfile = applyProfile;
applyProfile = function() {
  origApplyProfile.apply(this, arguments);
  connectSSE();
};
// ============================================================================
// Adaptive polling fallback — kicks in when SSE disconnects
// ============================================================================
let pollTimer = null;
let pollErrorStreak = 0;
const POLL_INTERVALS = [3000, 10000, 30000, 60000];

function pollTick() {
  // Pause during active chat or task creation — skip the request
  const chatStatus = document.querySelector('.chat-status');
  const taskStatus = document.querySelector('.task-status');
  if ((chatStatus && chatStatus.textContent === 'Thinking…') ||
      (taskStatus && (taskStatus.textContent === 'Creating…'))) {
    schedulePoll();
    return;
  }
  api('/api/ping')
    .then(r => {
      if (!r.ok) { pollErrorStreak++; schedulePoll(); return; }
      pollErrorStreak = 0;
      // Server is back — try reconnecting SSE for live updates
      connectSSE();
    })
    .catch(() => {
      pollErrorStreak++;
      schedulePoll();
    });
}

function schedulePoll() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  // Don't poll if SSE is active — it's the primary mechanism
  if (eventSource !== null) return;
  const idx = Math.min(pollErrorStreak, POLL_INTERVALS.length - 1);
  pollTimer = setTimeout(pollTick, POLL_INTERVALS[idx]);
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  pollErrorStreak = 0;
}

// ============================================================================
const profileEditor = (() => {
  let profilesCache = [];
  let selectedProfile = null;
  let isCreating = false;

  const overlay = () => document.getElementById('profileEditorOverlay');
  const modal = () => document.getElementById('profileEditorModal');
  const sidebar = () => document.getElementById('profileEditorSidebar');
  const msgEl = () => document.getElementById('peMsg');

  let modelsCache = {};  // { provider: [model1, model2, ...] }

  function setMsg(text, ok) {
    const el = msgEl();
    if (!el) return;
    el.textContent = text || '';
    el.className = 'profile-editor-msg ' + (ok ? 'ok' : (ok === false ? 'err' : ''));
  }

  function getFields() {
    const peProviderSel = document.getElementById('peProvider');
    const peProviderCustom = document.getElementById('peProviderCustom');
    const peModelSel = document.getElementById('peModel');
    const peModelCustom = document.getElementById('peModelCustom');
    return {
      name: document.getElementById('peName').value.trim(),
      provider: peProviderSel && peProviderSel.value === 'custom'
        ? (peProviderCustom ? peProviderCustom.value.trim() : '')
        : (peProviderSel ? peProviderSel.value : ''),
      model_default: peModelSel && peModelSel.value === 'custom'
        ? (peModelCustom ? peModelCustom.value.trim() : '')
        : (peModelSel ? peModelSel.value : ''),
      base_url: document.getElementById('peBaseUrl').value.trim(),
      context_length: document.getElementById('peContext').value.trim(),
      api_mode: document.getElementById('peApiMode').value.trim(),
      reasoning_effort: document.getElementById('peReasoningEffort').value,
      show_reasoning: document.getElementById('peReasoning').value,
      max_turns: document.getElementById('peMaxTurns').value.trim(),
      personality: document.getElementById('pePersonality').value,
    };
  }

  function setFields(cfg) {
    document.getElementById('peName').value = cfg.name || '';
    // Provider: dropdown or custom
    const peProviderSel = document.getElementById('peProvider');
    const peProviderCustom = document.getElementById('peProviderCustom');
    const prov = cfg.provider || '';
    if (peProviderCustom) peProviderCustom.style.display = 'none';
    if (peProviderSel) {
      const hasOption = prov && Array.from(peProviderSel.options).some(o => o.value === prov);
      if (hasOption) {
        peProviderSel.value = prov;
      } else if (prov) {
        peProviderSel.value = 'custom';
        if (peProviderCustom) { peProviderCustom.value = prov; peProviderCustom.style.display = ''; }
      } else {
        peProviderSel.value = '';
      }
    }
    // Model: dropdown or custom
    const peModelSel = document.getElementById('peModel');
    const peModelCustom = document.getElementById('peModelCustom');
    const mdl = cfg.model_default || '';
    if (peModelCustom) peModelCustom.style.display = 'none';
    if (peModelSel) {
      const hasOption = mdl && Array.from(peModelSel.options).some(o => o.value === mdl);
      if (hasOption) {
        peModelSel.value = mdl;
      } else if (mdl) {
        peModelSel.value = 'custom';
        if (peModelCustom) { peModelCustom.value = mdl; peModelCustom.style.display = ''; }
      } else {
        peModelSel.value = '';
      }
    }
    document.getElementById('peBaseUrl').value = cfg.base_url || '';
    document.getElementById('peContext').value = cfg.context_length || '';
    document.getElementById('peApiMode').value = cfg.api_mode || '';
    document.getElementById('peReasoningEffort').value = cfg.reasoning_effort || '';
    document.getElementById('peReasoning').value = cfg.show_reasoning || '';
    document.getElementById('peMaxTurns').value = cfg.max_turns || '';
    document.getElementById('pePersonality').value = cfg.personality || '';
    // Update model dropdown based on selected provider
    if (peProviderSel && peProviderSel.value && peProviderSel.value !== 'custom') {
      populateModelDropdown(peProviderSel.value, mdl);
    }
  }

  function populateModelDropdown(provider, selectedModel) {
    const peModelSel = document.getElementById('peModel');
    if (!peModelSel) return;
    const models = modelsCache[provider] || [];
    peModelSel.innerHTML = '<option value="">-- select model --</option>';
    for (const m of models) {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      peModelSel.appendChild(opt);
    }
    const customOpt = document.createElement('option');
    customOpt.value = 'custom';
    customOpt.textContent = 'Custom...';
    peModelSel.appendChild(customOpt);
    // Restore selection if it exists in the new list
    if (selectedModel && models.includes(selectedModel)) {
      peModelSel.value = selectedModel;
    } else if (selectedModel) {
      peModelSel.value = 'custom';
      const peModelCustom = document.getElementById('peModelCustom');
      if (peModelCustom) { peModelCustom.value = selectedModel; peModelCustom.style.display = ''; }
    }
  }

  function toggleCustomField(selectId, customId) {
    const sel = document.getElementById(selectId);
    const custom = document.getElementById(customId);
    if (!sel || !custom) return;
    custom.style.display = sel.value === 'custom' ? '' : 'none';
  }

  function renderSidebar() {
    const sb = sidebar();
    if (!sb) return;
    sb.innerHTML = '';
    for (const p of profilesCache) {
      const div = document.createElement('div');
      div.className = 'profile-editor-sidebar-item' + (selectedProfile === p.name ? ' active' : '');
      div.innerHTML = `<span>${p.name}</span><span class="small">${p.model || ''}</span>`;
      div.onclick = () => { isCreating = false; selectedProfile = p.name; renderSidebar(); loadProfile(p.name); };
      sb.appendChild(div);
    }
  }

  async function loadProfile(name) {
    setMsg('Loading…', null);
    try {
      const r = await api('/api/profile/' + encodeURIComponent(name));
      const d = await r.json();
      if (d.error) { setMsg(d.error, false); return; }
      document.getElementById('peName').readOnly = true;
      // Ensure model catalog is loaded before setting fields (race condition fix)
      if (Object.keys(modelsCache).length === 0) {
        try {
          const mr = await api('/api/models');
          const md = await mr.json();
          modelsCache = md.providers || {};
          populateProviderDropdown();
        } catch (_) {}
      }
      setFields(d);
      setMsg('Loaded ' + name, true);
    } catch (err) {
      setMsg(String(err), false);
    }
  }

  async function saveProfile() {
    console.log('saveProfile entered, isCreating=', isCreating);
    const fields = getFields();
    if (!fields.name) { setMsg('Name is required', false); return; }
    setMsg('Saving…', null);
    try {
      const r = await api('/api/profile/' + encodeURIComponent(fields.name), {}, 'POST', fields);
      const d = await r.json();
      if (d.error) { setMsg(d.error, false); return; }
      setMsg('Saved ' + fields.name, true);
      await loadProfiles();
      // Refresh cache for sidebar
      const pr = await api('/api/profiles');
      const pd = await pr.json();
      profilesCache = Array.isArray(pd.profiles) ? pd.profiles : [];
      renderSidebar();
    } catch (err) {
      setMsg(String(err), false);
    }
  }

  async function createProfile() {
    const fields = getFields();
    if (!fields.name) { setMsg('Enter a name for the new profile', false); return; }
    setMsg('Creating…', null);
    try {
      const r = await api('/api/profile', {}, 'POST', fields);
      const d = await r.json();
      if (d.error) { setMsg(d.error, false); return; }
      setMsg('Created ' + fields.name, true);
      isCreating = false;
      selectedProfile = fields.name;
      document.getElementById('peName').readOnly = true;
      await loadProfiles();
      const pr = await api('/api/profiles');
      const pd = await pr.json();
      profilesCache = Array.isArray(pd.profiles) ? pd.profiles : [];
      renderSidebar();
    } catch (err) {
      setMsg(String(err), false);
    }
  }

  async function deleteProfile() {
    const name = selectedProfile;
    if (!name) { setMsg('Select a profile to delete', false); return; }
    if (!confirm('Delete profile "' + name + '"? This cannot be undone.')) return;
    setMsg('Deleting…', null);
    try {
      const r = await api('/api/profile/' + encodeURIComponent(name), {}, 'DELETE');
      const d = await r.json();
      if (d.error) { setMsg(d.error, false); return; }
      setMsg('Deleted ' + name, true);
      selectedProfile = null;
      document.getElementById('peName').value = '';
      document.getElementById('peName').readOnly = false;
      await loadProfiles();
      const pr = await api('/api/profiles');
      const pd = await pr.json();
      profilesCache = Array.isArray(pd.profiles) ? pd.profiles : [];
      renderSidebar();
    } catch (err) {
      setMsg(String(err), false);
    }
  }

  function open() {
    selectedProfile = currentProfile;
    isCreating = false;
    const ov = overlay();
    const md = modal();
    if (ov) ov.classList.add('open');
    if (md) md.classList.add('open');
    setMsg('Loading profiles…', null);
    // Load model catalog
    api('/api/models')
      .then(r => r.json())
      .then(d => {
        modelsCache = d.providers || {};
        populateProviderDropdown();
      })
      .catch(_ => {});
    api('/api/profiles')
      .then(r => r.json())
      .then(d => {
        profilesCache = Array.isArray(d.profiles) ? d.profiles : [];
        renderSidebar();
        if (selectedProfile) loadProfile(selectedProfile);
        else setMsg('Select a profile from the sidebar', null);
      })
      .catch(err => setMsg('Failed to load profiles: ' + err, false));
  }

  function populateProviderDropdown() {
    const peProviderSel = document.getElementById('peProvider');
    if (!peProviderSel) return;
    const currentVal = peProviderSel.value;
    // Keep first two options (default + custom), replace the rest
    while (peProviderSel.options.length > 2) {
      peProviderSel.remove(peProviderSel.options.length - 1);
    }
    const providers = Object.keys(modelsCache).sort();
    for (const p of providers) {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = p + ' (' + (modelsCache[p] || []).length + ' models)';
      peProviderSel.insertBefore(opt, peProviderSel.lastElementChild);
    }
    // Restore selection
    if (currentVal && Array.from(peProviderSel.options).some(o => o.value === currentVal)) {
      peProviderSel.value = currentVal;
    }
  }

  function close() {
    const ov = overlay();
    const md = modal();
    if (ov) ov.classList.remove('open');
    if (md) md.classList.remove('open');
  }

  // Provider defaults: base_url, context_length, api_mode
  const PROVIDER_DEFAULTS = {
    'nous':          { base_url: 'https://inference-api.nousresearch.com/v1', context_length: '1048576', api_mode: 'chat_completions' },
    'openrouter':    { base_url: 'https://openrouter.ai/api/v1/', context_length: '1048576', api_mode: 'chat_completions' },
    'openai':        { base_url: 'https://api.openai.com/v1', context_length: '1048576', api_mode: 'chat_completions' },
    'anthropic':     { base_url: 'https://api.anthropic.com/v1', context_length: '200000', api_mode: 'messages' },
    'xai-oauth':     { base_url: 'https://api.x.ai/v1', context_length: '131072', api_mode: 'chat_completions' },
    'xai':           { base_url: 'https://api.x.ai/v1', context_length: '131072', api_mode: 'chat_completions' },
    'gemini':        { base_url: 'https://generativelanguage.googleapis.com/v1beta/openai', context_length: '1048576', api_mode: 'chat_completions' },
    'copilot':       { base_url: 'https://api.githubcopilot.com', context_length: '128000', api_mode: 'chat_completions' },
    'openai-codex':  { base_url: 'https://api.openai.com/v1', context_length: '1048576', api_mode: 'chat_completions' },
    'opencode-go':   { base_url: '', context_length: '1048576', api_mode: 'chat_completions' },
    'opencode-zen':  { base_url: 'https://opencode.ai/zen/go/v1', context_length: '1048576', api_mode: 'chat_completions' },
    'moonshot':      { base_url: 'https://api.moonshot.cn/v1', context_length: '131072', api_mode: 'chat_completions' },
    'minimax':       { base_url: 'https://api.minimax.chat/v1', context_length: '262144', api_mode: 'chat_completions' },
    'stepfun':       { base_url: 'https://api.stepfun.com/v1', context_length: '131072', api_mode: 'chat_completions' },
    'deepseek':      { base_url: 'https://api.deepseek.com/v1', context_length: '131072', api_mode: 'chat_completions' },
    'zai':           { base_url: 'https://open.bigmodel.cn/api/paas/v4', context_length: '131072', api_mode: 'chat_completions' },
    'nvidia':        { base_url: 'https://integrate.api.nvidia.com/v1', context_length: '131072', api_mode: 'chat_completions' },
    'google-gemini-cli': { base_url: 'https://generativelanguage.googleapis.com/v1beta/openai', context_length: '1048576', api_mode: 'chat_completions' },
    'ai-gateway':    { base_url: 'https://ai-gateway.vercel.com/v1', context_length: '1048576', api_mode: 'chat_completions' },
  };

  function applyProviderDefaults(provider) {
    const defs = PROVIDER_DEFAULTS[provider];
    if (!defs) return;
    const baseUrl = document.getElementById('peBaseUrl');
    const ctxLen = document.getElementById('peContext');
    const apiMode = document.getElementById('peApiMode');
    // Always update base_url when provider changes (it's provider-specific)
    if (baseUrl) baseUrl.value = defs.base_url;
    // Only fill context_length and api_mode if empty
    if (ctxLen && !ctxLen.value.trim()) ctxLen.value = defs.context_length;
    if (apiMode && !apiMode.value.trim()) apiMode.value = defs.api_mode;
  }

  function init() {
    document.getElementById('profileEditorClose').addEventListener('click', close);
    document.getElementById('profileEditorOverlay').addEventListener('click', close);
    document.getElementById('peSaveBtn').addEventListener('click', () => {
      if (isCreating) createProfile(); else saveProfile();
    });
    document.getElementById('peCreateBtn').addEventListener('click', () => {
      isCreating = true;
      selectedProfile = null;
      document.getElementById('peName').readOnly = false;
      setFields({});
      renderSidebar();
      setMsg('Enter details for the new profile, then click Save', null);
    });
    document.getElementById('peDeleteBtn').addEventListener('click', deleteProfile);
    // Provider change -> update model dropdown + auto-fill base_url/context/api_mode
    document.getElementById('peProvider').addEventListener('change', () => {
      toggleCustomField('peProvider', 'peProviderCustom');
      const sel = document.getElementById('peProvider');
      if (sel && sel.value && sel.value !== 'custom') {
        const mdl = document.getElementById('peModel');
        const currentModel = (mdl && mdl.value === 'custom')
          ? (document.getElementById('peModelCustom')?.value || '')
          : (mdl?.value || '');
        populateModelDropdown(sel.value, currentModel);
        applyProviderDefaults(sel.value);
      }
    });
    // Model dropdown change -> toggle custom field
    document.getElementById('peModel').addEventListener('change', () => {
      toggleCustomField('peModel', 'peModelCustom');
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  }

  return { open, close, init };
})();
window.profileEditor = profileEditor;

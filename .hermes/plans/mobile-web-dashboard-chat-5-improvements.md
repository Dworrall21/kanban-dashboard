# Plan: 5 Improvements for Mobile Web Dashboard + Chat

## Profile assignments & task IDs

---

### 1. Task lifecycle management (edit · delete · status change)
**Delegate to:** `profile=visual-reviewer`

Currently tasks are created from the UI but can never be modified or removed. Users must drop to terminal for any lifecycle management.

**What to build:**
- Backend: Add `PATCH /api/tasks/<id>` and `DELETE /api/tasks/<id>` endpoints that call `hermes kanban edit` and `hermes kanban delete`
- Backend: Add `POST /api/tasks/<id>/transition` to change status (todo → running → done → blocked)
- Frontend: Right-click or long-press context menu on task rows with: Edit title, Edit body, Mark done, Mark blocked, Delete
- Frontend: Inline edit on the detail panel/sheet
- Frontend: Status badge becomes clickable dropdown to change status

**Files changed:** `kanban_browser.py` (+ new API handlers, + frontend context menu + inline edits, + tests)

---

### 2. SSE push for real-time updates (kill polling)
**Delegate to:** `profile=overseer`

The current 3-second polling is wasteful and introduces latency. Replace with Server-Sent Events.

**What to build:**
- Backend: `GET /api/events` SSE endpoint that emits `board-update`, `task-changed`, `chat-message`, and `profile-changed` events
- Frontend: `EventSource('/api/events')` replaces the `setInterval(refresh, 3000)` — instant updates on task create, status change, or chat reply
- Backend: Emit events from existing POST handlers (`_handle_create_task`, `_handle_chat`, etc.)

**Files changed:** `kanban_browser.py` (+ SSE endpoint + EventSource client code + remove polling)

---

### 3. Kanban board columns (drag & drop)
**Delegate to:** `profile=visual-reviewer`

The flat table is functional but doesn't give a real kanban overview. Turn it into column-based view with drag-and-drop status changes.

**What to build:**
- Columns: To Do, Running, Done, Blocked
- Drag-and-drop task cards between columns → calls status transition API
- Responsive: columns stack vertically on mobile (< 768px)
- Toggle between table view and board view (persisted in URL param)
- Smooth animations on card movement

**Files changed:** `kanban_browser.py` (+ board view CSS + drag-and-drop JS + view toggle + tests)

---

### 4. Task filtering, search & sort
**Delegate to:** `profile=overseer`

With even moderate task volumes the flat list becomes hard to navigate.

**What to build:**
- Search bar above task list: filters by title, ID (client-side, debounced 300ms)
- Filter chips: status (✓ done / ● running / ◻ todo / ⊘ blocked), assignee, date range
- Sort: by created date, title, status — with ascending/descending toggle
- URL-persisted filter state so bookmarkable views work
- Task count badge per status in both table and board views

**Files changed:** `kanban_browser.py` (+ filter UI + search bar + sort controls + URL param sync)

---

### 5. Chat history export & session management
**Delegate to:** `profile=overseer`

Chat history is stored per-board but there's no way to browse past sessions or export conversations.

**What to build:**
- Session list panel: collapsible sidebar showing past sessions per board with date, message count, last message preview
- Export: Copy as JSON, Copy as Markdown, Download as .md file buttons
- Session rename: allow naming sessions for easier recall
- Session delete: remove old sessions from server-side history
- Persist session metadata (`session_meta.json` alongside `chat-history.json`)

**Files changed:** `kanban_browser.py` (+ session sidebar + export functions + session metadata persistence + tests)

---

## Execution order

1. **Task lifecycle** — foundational, other items depend on transition API
2. **SSE push** — makes all future updates feel instant
3. **Kanban board columns** — big UX improvement, uses SSE for live updates
4. **Filtering/search/sort** — enhances both table and board views
5. **Chat history export & sessions** — independent, can be done in parallel with 3+4
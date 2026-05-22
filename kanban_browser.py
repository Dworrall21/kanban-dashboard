#!/usr/bin/env python3
"""
Hermes Kanban Browser — Mobile Web Dashboard with Chat.

Decoupled architecture:
  - Chat module: handles chat history persistence (client localStorage + server-side)
  - Task module: handles task creation, accepts optional chat session reference
  - Event bus: lightweight pub/sub for cross-module communication
  - Server: separate endpoints for chat, task creation, and chat history sync
"""
import ast
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import time
import hashlib

DEFAULT_BOARD = os.environ.get('HERMES_KANBAN_BOARD', 'mobile-web-dashboard-chat')
DEFAULT_PROFILE = os.environ.get('HERMES_PROFILE', 'default')
OVERSEER_PROFILE = os.environ.get('HERMES_OVERSEER_PROFILE', 'overseer')
PORT = int(os.environ.get('KANBAN_BROWSER_PORT', '8799'))
HERMES_HOME = Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes'))
BOARDS_ROOT = HERMES_HOME / 'kanban' / 'boards'
TASK_RE = re.compile(r'^(?P<state>[✓●◻▶])\s+(?P<id>t_[0-9a-f]+)\s+(?P<status>\w+)\s+(?P<assignee>\S+)\s+(?P<title>.+)$')
MODEL_LINE_RE = re.compile(r'^\s*Model:\s+(?P<model>\{.*\})\s*$')

# ---------------------------------------------------------------------------
# Simple TTL caches
# ---------------------------------------------------------------------------
_PROFILE_CACHE = {'profiles': None, 'current': None, 'ts': 0.0}
_PROFILE_CACHE_TTL = 30.0
_LIST_CACHE: dict[tuple[str, str], dict] = {}
_LIST_CACHE_TTL = 2.5


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Event bus — thread-safe pub/sub for Server-Sent Events
# ---------------------------------------------------------------------------

class EventBus:
    """Per-client-queue pub/sub so SSE connections receive events in real time."""
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[queue.Queue]] = {}

    def subscribe(self, event_type: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(q)
        return q

    def unsubscribe(self, event_type: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(event_type)
            if subs:
                self._subscribers[event_type] = [s for s in subs if s is not q]

    def emit(self, event_type: str, data: object) -> None:
        with self._lock:
            queues = list(self._subscribers.get(event_type, []))
        for q in queues:
            try:
                q.put_nowait({'event': event_type, 'data': data})
            except queue.Full:
                pass  # slow client — drop event silently


event_bus = EventBus()
# Static file serving
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / 'static'
_INDEX_HTML: str | None = None
_INDEX_MTIME: float = 0


def _load_index_html() -> str:
    """Read index.html from disk, cache until mtime changes."""
    global _INDEX_HTML, _INDEX_MTIME
    path = STATIC_DIR / 'index.html'
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return '<!doctype html><html><body>index.html not found</body></html>'
    if _INDEX_HTML is None or mtime != _INDEX_MTIME:
        _INDEX_HTML = path.read_text(encoding='utf-8').replace('{{BOARD_NAME}}', DEFAULT_BOARD)
        _INDEX_MTIME = mtime
    return _INDEX_HTML


def _parse_simple_yaml_profile(text: str) -> dict:
    """Best-effort parser for simple 2-level YAML config files.

    Supports the profile structure used by Hermes:
      section:
        key: value
    """
    result: dict = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if not line.startswith(' ') and stripped.endswith(':'):
            current = stripped[:-1].strip()
            if current:
                result.setdefault(current, {})
            continue
        if current and ':' in stripped:
            key, value = stripped.split(':', 1)
            key = key.strip()
            value = value.strip()
            # remove simple quotes
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            # parse primitive scalars
            lower = value.lower()
            if lower == 'true':
                parsed = True
            elif lower == 'false':
                parsed = False
            else:
                try:
                    parsed = int(value)
                except ValueError:
                    parsed = value
            section = result.setdefault(current, {})
            if isinstance(section, dict):
                section[key] = parsed
    return result


_MIME_TYPES = {
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.html': 'text/html',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.ico': 'image/x-icon',
    '.json': 'application/json',
    '.woff2': 'font/woff2',
    '.woff': 'font/woff',
    '.ttf': 'font/ttf',
}


def _static_mime(path: str) -> str:
    from pathlib import Path as _P
    return _MIME_TYPES.get(_P(path).suffix.lower(), 'application/octet-stream')



# ---------------------------------------------------------------------------
# Server-side helpers
# ---------------------------------------------------------------------------

CHAT_HISTORY_MAX = 200


def chat_history_path(board: str) -> Path:
    return BOARDS_ROOT / board / 'chat-history.json'


def _load_chat_store(board: str) -> dict:
    """Load the full session-indexed chat store for a board."""
    path = chat_history_path(board)
    if not path.exists():
        return {'sessions': {}, 'current_session': None}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and 'sessions' in data:
            return data
        # Legacy flat list — migrate to session format
        if isinstance(data, list):
            store = {'sessions': {}, 'current_session': None}
            if data:
                sid = hashlib.sha256(f"{board}:legacy".encode()).hexdigest()[:12]
                ts = data[0].get('ts', 1715000000)
                store['sessions'][sid] = {
                    'id': sid,
                    'name': 'Session ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(ts)),
                    'created_at': ts,
                    'updated_at': time.time(),
                    'history': data,
                }
                store['current_session'] = sid
            return store
    except Exception:
        pass
    return {'sessions': {}, 'current_session': None}


def _save_chat_store(board: str, store: dict) -> None:
    """Save the full session-indexed chat store for a board."""
    path = chat_history_path(board)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Trim each session's history
        for sid, sess in store.get('sessions', {}).items():
            if isinstance(sess.get('history'), list) and len(sess['history']) > CHAT_HISTORY_MAX:
                sess['history'] = sess['history'][-CHAT_HISTORY_MAX:]
        path.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def load_chat_history(board: str, session_id: str | None = None) -> list:
    """Load chat history for a specific session, or current session."""
    store = _load_chat_store(board)
    sid = session_id or store.get('current_session')
    if sid and sid in store.get('sessions', {}):
        return store['sessions'][sid].get('history', [])
    return []


def save_chat_history(board: str, history: list, session_id: str | None = None) -> str | None:
    """Save chat history, returning the session id. Creates a session if needed."""
    store = _load_chat_store(board)
    sid = session_id or store.get('current_session')

    if sid and sid in store.get('sessions', {}):
        store['sessions'][sid]['history'] = history
        store['sessions'][sid]['updated_at'] = time.time()
    else:
        # Create a new session
        ts = time.time()
        sid = hashlib.sha256(f"{board}:{ts}:{len(history)}".encode()).hexdigest()[:12]
        store.setdefault('sessions', {})[sid] = {
            'id': sid,
            'name': 'Session ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(ts)),
            'created_at': ts,
            'updated_at': ts,
            'history': history,
        }
        store['current_session'] = sid

    _save_chat_store(board, store)
    return sid


def list_chat_sessions(board: str) -> list[dict]:
    """Return metadata for all sessions (without full history)."""
    store = _load_chat_store(board)
    sessions = store.get('sessions', {})
    current = store.get('current_session')
    result = []
    for sid, sess in sessions.items():
        last_msg = None
        history = sess.get('history', [])
        if history:
            last_msg = history[-1]
        result.append({
            'id': sid,
            'name': sess.get('name', sid),
            'created_at': sess.get('created_at', 0),
            'updated_at': sess.get('updated_at', 0),
            'message_count': len(history),
            'last_message': last_msg,
            'is_current': sid == current,
        })
    result.sort(key=lambda s: s.get('updated_at', 0), reverse=True)
    return result


def delete_chat_session(board: str, session_id: str) -> bool:
    """Delete a session. Returns True if deleted."""
    store = _load_chat_store(board)
    if session_id not in store.get('sessions', {}):
        return False
    del store['sessions'][session_id]
    if store.get('current_session') == session_id:
        # Switch to next available session
        remaining = list(store['sessions'].keys())
        store['current_session'] = remaining[0] if remaining else None
    _save_chat_store(board, store)
    return True


def rename_chat_session(board: str, session_id: str, new_name: str) -> bool:
    """Rename a session. Returns True if renamed."""
    store = _load_chat_store(board)
    if session_id not in store.get('sessions', {}):
        return False
    store['sessions'][session_id]['name'] = new_name.strip() or session_id
    _save_chat_store(board, store)
    return True


def set_current_session(board: str, session_id: str) -> bool:
    """Set the current active session. Returns True if found."""
    store = _load_chat_store(board)
    if session_id not in store.get('sessions', {}):
        return False
    store['current_session'] = session_id
    _save_chat_store(board, store)
    return True


def shell(cmd: str, board: str, profile: str) -> str:
    env = os.environ.copy()
    env['HERMES_KANBAN_BOARD'] = board
    if profile:
        env['HERMES_PROFILE'] = profile
    proc = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


def hermes_chat(board: str, profile: str, history: list[dict], message: str) -> tuple[str, str]:
    """Send a chat message to Hermes. Returns (reply, raw_output)."""
    board_snapshot = shell('hermes kanban list', board, profile)
    transcript = []
    for turn in history[-12:]:
        role = str(turn.get('role', '')).strip().lower()
        content = str(turn.get('content', '')).strip()
        if not content:
            continue
        if role not in {'user', 'assistant', 'system'}:
            continue
        transcript.append(f"{role.title()}: {content}")
    transcript.append(f"User: {message}")
    prompt = textwrap.dedent(f"""
    You are helping direct the selected kanban project.
    Board: {board}
    Profile: {profile}

    Current kanban snapshot:
    {board_snapshot.strip()}

    Conversation so far:
    {chr(10).join(transcript)}

    Respond with concise, actionable guidance for the selected project.
    If useful, mention specific task ids from the board. Do not mention that this
    is a simulation.

    Assistant:
    """).strip()
    env = {**os.environ, 'HERMES_KANBAN_BOARD': board, 'HERMES_PROFILE': profile}
    # Inject openrouter api key from global auth so profile-specific auth gaps don't break chat
    auth_path = HERMES_HOME / 'auth.json'
    if auth_path.exists() and not env.get('OPENROUTER_API_KEY'):
        try:
            auth_data = json.loads(auth_path.read_text(encoding='utf-8'))
            for cred in auth_data.get('credential_pool', {}).get('openrouter', []):
                if cred.get('access_token'):
                    env['OPENROUTER_API_KEY'] = cred['access_token']
                    break
        except Exception:
            pass
    proc = subprocess.run(
        ['hermes', '-p', profile, 'chat', '-q', prompt, '-v'],
        env=env,
        capture_output=True,
        text=True,
    )
    stdout = proc.stdout or ''
    stderr = proc.stderr or ''
    combined = stderr + ('\n' if stderr and stdout else '') + stdout
    if proc.returncode != 0:
        raise RuntimeError(combined.strip() or f'hermes chat failed with code {proc.returncode}')
    reply = extract_hermes_reply(stdout)
    return reply, combined


def hermes_create_task(board: str, profile: str, title: str, body: str,
                       chat_session_id: str = None, parent_task_id: str = None) -> dict:
    """Create a kanban task. Returns the task dict from hermes kanban create --json."""
    env = os.environ.copy()
    env['HERMES_KANBAN_BOARD'] = board
    if profile:
        env['HERMES_PROFILE'] = profile
    args = ['hermes', 'kanban', 'create', title, '--json', '--created-by', 'kanban-browser']
    if profile:
        args.extend(['--assignee', profile])
    if body:
        args.extend(['--body', body])
    proc = subprocess.run(args, env=env, capture_output=True, text=True)
    raw = (proc.stdout or '').strip() or (proc.stderr or '').strip()
    if proc.returncode != 0:
        raise RuntimeError(raw or f'hermes kanban create failed with code {proc.returncode}')
    try:
        data = json.loads(raw)
    except Exception:
        raise RuntimeError(raw or 'hermes kanban create returned non-JSON output')
    if not isinstance(data, dict):
        raise RuntimeError('unexpected create response')
    # Attach metadata about chat session / parent for downstream consumers
    if chat_session_id:
        data['chat_session_id'] = chat_session_id
    if parent_task_id:
        data['parent_task_id'] = parent_task_id
    return data


def extract_hermes_reply(output: str) -> str:
    lines = output.splitlines()
    # Find ALL response box boundaries, then use the LAST one
    # (agents often produce multiple intermediate boxes during tool calls)
    box_starts = []
    box_ends = []
    current_start = None
    for i, line in enumerate(lines):
        if line.startswith('╭') and 'Hermes' in line:
            current_start = i + 1
        elif current_start is not None and line.startswith('╰'):
            box_starts.append(current_start)
            box_ends.append(i)
            current_start = None
    if box_starts and box_ends:
        # Use the last box — it contains the final response after all tool calls
        start = box_starts[-1]
        end = box_ends[-1]
        if end > start:
            body = lines[start:end]
            while body and not body[0].strip():
                body.pop(0)
            while body and not body[-1].strip():
                body.pop()
            stripped = []
            for line in body:
                stripped.append(line[4:] if line.startswith('    ') else line.lstrip())
            return '\n'.join(stripped).strip()
    # Fallback: last non-empty line
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return ''


def parse_tasks(text: str):
    tasks = []
    for line in text.splitlines():
        m = TASK_RE.match(line.strip())
        if not m:
            continue
        d = m.groupdict()
        tasks.append({
            'state': d['state'],
            'id': d['id'],
            'status': d['status'],
            'assignee': d['assignee'],
            'title': d['title'],
        })
    return tasks


def list_boards():
    boards = []
    if BOARDS_ROOT.exists():
        for entry in sorted(BOARDS_ROOT.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir():
                continue
            meta = {
                'slug': entry.name,
                'name': entry.name.replace('-', ' ').title(),
                'archived': False,
            }
            board_json = entry / 'board.json'
            if board_json.exists():
                try:
                    data = json.loads(board_json.read_text())
                    if isinstance(data, dict):
                        meta.update({
                            'slug': data.get('slug', meta['slug']),
                            'name': data.get('name', meta['name']),
                            'archived': bool(data.get('archived', False)),
                        })
                except Exception:
                    pass
            boards.append(meta)
    if not any(b['slug'] == 'default' for b in boards):
        boards.insert(0, {'slug': 'default', 'name': 'Default', 'archived': False})
    current = next((b for b in boards if b['slug'] == DEFAULT_BOARD), boards[0] if boards else {'slug': DEFAULT_BOARD, 'name': DEFAULT_BOARD, 'archived': False})
    return boards, current


def delete_board_slug(board: str) -> tuple[bool, dict | str]:
    board = str(board or '').strip()
    if not board:
        return False, 'board is required'
    if board == 'default':
        return False, 'default board cannot be deleted'
    board_dir = BOARDS_ROOT / board
    if not board_dir.exists() or not board_dir.is_dir():
        return False, 'board not found'
    try:
        shutil.rmtree(board_dir)
        for key in list(_LIST_CACHE.keys()):
            if key[0] == board:
                _LIST_CACHE.pop(key, None)
        boards, current = list_boards()
        if current.get('slug') == board:
            current = next((b for b in boards if b['slug'] != board), {'slug': DEFAULT_BOARD, 'name': 'Default', 'archived': False})
        event_bus.emit('board-update', {'board': board, 'action': 'deleted'})
        return True, current
    except Exception as exc:
        return False, str(exc)


def list_profiles():
    t0 = _now()
    cached = _PROFILE_CACHE
    if cached['profiles'] is not None and (_now() - cached['ts']) < _PROFILE_CACHE_TTL:
        elapsed = _now() - t0
        print(f'[kanban_browser] list_profiles cache HIT ({elapsed:.3f}s)', file=sys.stderr, flush=True)
        return cached['profiles'], cached['current']
    raw = subprocess.run(['hermes', 'profile', 'list'], capture_output=True, text=True, env=os.environ.copy())
    if raw.returncode != 0:
        return [], {'name': DEFAULT_PROFILE, 'model': 'unknown'}
    names = []
    for line in raw.stdout.splitlines():
        line = line.rstrip()
        clean = line.strip()
        if not clean or clean.startswith('Profile') or clean.startswith('─'):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0].lstrip('◆*')
        if name and name != 'Profile':
            names.append(name)
    profiles = []
    for name in names:
        model = 'unknown'
        proc = subprocess.run(['hermes', '-p', name, 'config', 'show'], capture_output=True, text=True, env=os.environ.copy())
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                m = MODEL_LINE_RE.match(line)
                if not m:
                    continue
                try:
                    data = ast.literal_eval(m.group('model'))
                    if isinstance(data, dict):
                        model = str(data.get('default') or data.get('model') or data.get('provider') or 'unknown')
                except Exception:
                    pass
                break
        profiles.append({'name': name, 'model': model})
    current = next((p for p in profiles if p['name'] == DEFAULT_PROFILE), profiles[0] if profiles else {'name': DEFAULT_PROFILE, 'model': 'unknown'})
    _PROFILE_CACHE['profiles'] = profiles
    _PROFILE_CACHE['current'] = current
    _PROFILE_CACHE['ts'] = _now()
    elapsed = _now() - t0
    print(f'[kanban_browser] list_profiles cache MISS ({elapsed:.3f}s, {len(names)} profiles)', file=sys.stderr, flush=True)
    return profiles, current


# ---------------------------------------------------------------------------
# HTTP handler — routes to separate handler methods per domain
# ---------------------------------------------------------------------------

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj, indent=2).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _query(self):
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def _board_from_request(self, qs):
        return qs.get('board', [DEFAULT_BOARD])[0] or DEFAULT_BOARD

    def _profile_from_request(self, qs):
        return qs.get('profile', [DEFAULT_PROFILE])[0] or DEFAULT_PROFILE

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', '0') or '0')
        raw = self.rfile.read(length).decode('utf-8') if length else '{}'
        try:
            return json.loads(raw or '{}')
        except Exception as exc:
            self.send_json({'error': f'invalid JSON: {exc}'}, 400)
            return None

    # ---- GET handlers ----

    def do_GET(self):
        p, qs = self._query()
        board = self._board_from_request(qs)
        profile = self._profile_from_request(qs)

        if p.startswith('/static/'):
            self._serve_static(p)
            return
        if p == '/':
            self._serve_html()
            return
        if p == '/api/boards':
            self._handle_list_boards()
            return
        if p == '/api/profiles':
            self._handle_list_profiles()
            return
        if p == '/api/models':
            self._handle_list_models()
            return
        if p == '/api/list':
            self._handle_list_tasks(board, profile)
            return
        if p.startswith('/api/show/'):
            self._handle_show_task(p, board, profile)
            return
        if p == '/api/chat-history':
            self._handle_get_chat_history(board, qs)
            return
        if p == '/api/chat-sessions':
            self._handle_list_chat_sessions(board)
            return
        if p == '/api/chat-export':
            self._handle_chat_export(board, qs)
            return
        if p.startswith('/api/profile/'):
            self._handle_get_profile(p)
            return
        if p == '/api/events':
            self._handle_events(board, profile)
            return
        if p == '/api/ping':
            self.send_json({'ok': True, 'board': board})
            return
        self.send_json({'error': 'not found'}, 404)

    def _serve_html(self):
        body = _load_index_html().encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str):
        """Serve a file from STATIC_DIR with proper MIME type and caching headers."""
        # path comes in as '/static/<relative>' and STATIC_DIR already points at the
        # 'static/' dir, so strip the leading '/static/' prefix so the join works correctly.
        prefix = '/static/'
        safe_path = path[len(prefix):] if path.startswith(prefix) else path.lstrip('/')
        # Security: prevent directory traversal
        if '..' in safe_path or safe_path.startswith('..'):
            self.send_json({'error': 'invalid path'}, 403)
            return
        file_path = STATIC_DIR / safe_path.replace('/', os.sep)
        try:
            if not file_path.exists() or not file_path.is_file():
                self.send_json({'error': 'not found'}, 404)
                return
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', _static_mime(str(file_path)))
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            self.send_json({'error': str(exc)}, 500)

    def _handle_list_boards(self):
        boards, current = list_boards()
        self.send_json({'boards': boards, 'current_board': current})

    def _handle_delete_board(self, path, qs):
        target = path.rsplit('/', 1)[-1] if path != '/api/boards' else self._board_from_request(qs)
        ok, result = delete_board_slug(target)
        if not ok:
            code = 400 if 'cannot be deleted' in str(result) or 'required' in str(result) else 404 if 'not found' in str(result) else 500
            self.send_json({'error': str(result)}, code)
            return
        self.send_json({'deleted': True, 'board': target, 'current_board': result})

    def _handle_list_profiles(self):
        profiles, current = list_profiles()
        self.send_json({'profiles': profiles, 'current_profile': current})

    def _handle_list_models(self):
        """Return model catalog grouped by provider for the profile editor dropdown."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / '.hermes' / 'hermes-agent'))
        try:
            from hermes_cli.models import _PROVIDER_MODELS
            catalog = {}
            for provider, models in _PROVIDER_MODELS.items():
                if isinstance(models, list):
                    catalog[provider] = models
            # Also expose OpenRouter curated list
            try:
                from hermes_cli.models import OPENROUTER_MODELS
                if 'openrouter' not in catalog:
                    catalog['openrouter'] = [m[0] if isinstance(m, (list, tuple)) else m for m in OPENROUTER_MODELS]
            except Exception:
                pass
            self.send_json({'providers': catalog})
        except Exception as exc:
            self.send_json({'error': str(exc), 'providers': {}}, 500)

    def _handle_list_tasks(self, board, profile):
        t0 = _now()
        cache_key = (board, profile)
        cached = _LIST_CACHE.get(cache_key)
        if cached and (_now() - cached['ts']) < _LIST_CACHE_TTL:
            raw = cached['raw']
            elapsed = _now() - t0
            print(f'[kanban_browser] /api/list cache HIT ({elapsed:.3f}s)', file=sys.stderr, flush=True)
        else:
            raw = shell('hermes kanban list', board, profile)
            _LIST_CACHE[cache_key] = {'raw': raw, 'ts': _now()}
            elapsed = _now() - t0
            print(f'[kanban_browser] /api/list cache MISS ({elapsed:.3f}s)', file=sys.stderr, flush=True)
        tasks = parse_tasks(raw)
        order_path = BOARDS_ROOT / board / 'display_order.json'
        if order_path.exists():
            try:
                order = json.loads(order_path.read_text())
                order_map = {tid: idx for idx, tid in enumerate(order)}
                tasks.sort(key=lambda t: order_map.get(t['id'], 999999))
            except Exception:
                pass
        self.send_json({'board': board, 'profile': profile, 'summary': raw.splitlines()[0] if raw else '', 'tasks': tasks})

    def _handle_show_task(self, path, board, profile):
        tid = path.rsplit('/', 1)[-1]
        raw = shell(f'hermes kanban show {tid}', board, profile)
        title = ''
        for line in raw.splitlines():
            if line.strip().startswith('Task '):
                title = line.split(':', 1)[-1].strip()
                break
        self.send_json({'id': tid, 'title': title, 'raw': raw, 'board': board, 'profile': profile})

    def _handle_get_chat_history(self, board, qs):
        session_id = qs.get('session', [None])[0] if 'session' in qs else None
        history = load_chat_history(board, session_id=session_id)
        self.send_json({'board': board, 'history': history, 'session_id': session_id or ''})

    def _handle_list_chat_sessions(self, board):
        sessions = list_chat_sessions(board)
        self.send_json({'board': board, 'sessions': sessions})

    def _handle_chat_export(self, board, qs):
        session_id = qs.get('session', [None])[0] if 'session' in qs else None
        export_format = qs.get('format', ['json'])[0] if 'format' in qs else 'json'
        history = load_chat_history(board, session_id=session_id)
        store = _load_chat_store(board)
        sid = session_id or store.get('current_session')
        session_info = store.get('sessions', {}).get(sid, {})
        name = session_info.get('name', sid or 'chat')
        if export_format == 'markdown':
            lines = [f"# Chat Export: {name}", f"*Date: {time.strftime('%Y-%m-%d %H:%M:%S')}*", f"*Board: {board}*", f"*Session: {sid}*", f"*Messages: {len(history)}*", "", "---", ""]
            for msg in history:
                role = msg.get('role', 'unknown')
                content = str(msg.get('content', ''))
                ts = msg.get('ts', '')
                ts_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(ts)) if isinstance(ts, (int, float)) else str(ts)
                lines.append(f"### {role.title()} ({ts_str})")
                lines.append("")
                lines.append(content)
                lines.append("")
            body = '\n'.join(lines).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/markdown; charset=utf-8')
            self.send_header('Content-Disposition', f'attachment; filename="{name.replace(" ", "_")}.md"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            payload = {
                'board': board,
                'session_id': sid,
                'session_name': name,
                'exported_at': time.time(),
                'history': history,
            }
            body = json.dumps(payload, indent=2, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Disposition', f'attachment; filename="{name.replace(" ", "_")}.json"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    # ---- SSE endpoint ----

    def _handle_events(self, board, profile):
        """Server-Sent Events endpoint — streams real-time board updates."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')  # nginx
        self.end_headers()

        # Subscribe to all event types
        event_types = ['task-changed', 'chat-message', 'board-update', 'profile-changed']
        subs = {et: event_bus.subscribe(et) for et in event_types}

        # Send initial connection event so the client knows it's live
        self._sse_send('connected', {'board': board, 'profile': profile})

        try:
            while not getattr(self.server, '_shutdown_request', False):
                # Poll all subscribed queues with a shared deadline
                deadline = time.time() + 10
                got_data = False
                for et in event_types:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    try:
                        msg = subs[et].get(timeout=min(remaining, 10))
                        self._sse_send(et, msg.get('data', msg))
                        got_data = True
                    except queue.Empty:
                        continue
                if not got_data:
                    # Keepalive comment — prevents proxies from timing out
                    try:
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client disconnected — clean exit
        except Exception:
            pass  # don't crash the server on one bad SSE client
        finally:
            for et in event_types:
                event_bus.unsubscribe(et, subs[et])

    def _sse_send(self, event_type, data):
        """Format and flush a single SSE message."""
        payload = json.dumps(data, default=str)
        self.wfile.write(f'event: {event_type}\ndata: {payload}\n\n'.encode('utf-8'))
        self.wfile.flush()

    # ---- POST handlers ----

    def do_POST(self):
        p, qs = self._query()

        if p == '/api/chat':
            self._handle_chat(qs)
            return
        if p == '/api/create-task':
            self._handle_create_task(qs)
            return
        if p == '/api/chat-history':
            self._handle_post_chat_history(qs)
            return
        if p == '/api/chat-sessions':
            self._handle_post_chat_session(qs)
            return
        if p == '/api/chat-sessions/switch':
            self._handle_switch_chat_session(qs)
            return
        if p == '/api/profile':
            self._handle_create_profile(qs)
            return
        if p.startswith('/api/profile/'):
            self._handle_update_profile(p, qs)
            return
        m = __import__('re').match(r'^/api/tasks/([^/]+)/transition$', p)
        if m:
            self._handle_transition_task(m.group(1), qs)
            return
        if p == '/api/reorder':
            self._handle_reorder(qs)
            return
        self.send_json({'error': 'not found'}, 404)

    def _handle_chat(self, qs):
        body = self._read_json_body()
        if body is None:
            return
        board = str(body.get('board') or self._board_from_request(qs) or DEFAULT_BOARD)
        profile = str(body.get('profile') or self._profile_from_request(qs) or DEFAULT_PROFILE)
        message = str(body.get('message') or '').strip()
        history = body.get('history') or []
        if not message:
            self.send_json({'error': 'message is required'}, 400)
            return
        try:
            reply, raw = hermes_chat(board, profile, history, message)
        except Exception as exc:
            self.send_json({'error': str(exc)}, 500)
            return
        # Persist chat history server-side
        server_history = load_chat_history(board, session_id=body.get('session_id'))
        server_history.append({'role': 'user', 'content': message, 'ts': time.time()})
        server_history.append({'role': 'assistant', 'content': reply, 'ts': time.time()})
        session_id = save_chat_history(board, server_history, session_id=body.get('session_id'))
        self.send_json({
            'board': board,
            'profile': profile,
            'reply': reply,
            'raw': raw,
            'chat_session_id': session_id,
        })
        # Notify SSE clients of new chat message
        event_bus.emit('chat-message', {
            'board': board,
            'profile': profile,
            'session_id': session_id,
        })

    def _handle_create_task(self, qs):
        body = self._read_json_body()
        if body is None:
            return
        board = str(body.get('board') or self._board_from_request(qs) or DEFAULT_BOARD)
        title = str(body.get('title') or '').strip()
        body_text = str(body.get('body') or '').strip()
        chat_session_id = body.get('chat_session_id') or None
        parent_task_id = body.get('parent_task_id') or None
        assignee = str(body.get('assignee') or body.get('profile') or OVERSEER_PROFILE)
        if not title:
            self.send_json({'error': 'title is required'}, 400)
            return
        try:
            task = hermes_create_task(
                board, assignee, title, body_text,
                chat_session_id=chat_session_id,
                parent_task_id=parent_task_id,
            )
        except Exception as exc:
            self.send_json({'error': str(exc)}, 500)
            return
        self.send_json({'board': board, 'profile': assignee, 'assignee': assignee, 'task': task})
        # Notify SSE clients of new/changed task
        event_bus.emit('task-changed', {
            'board': board,
            'task_id': task.get('id', task.get('task_id', '')),
            'action': 'created',
            'assignee': assignee,
        })

    def _handle_post_chat_history(self, qs):
        body = self._read_json_body()
        if body is None:
            return
        b = str(body.get('board') or self._board_from_request(qs) or DEFAULT_BOARD)
        history = body.get('history')
        if not isinstance(history, list):
            self.send_json({'error': 'history must be a list'}, 400)
            return
        session_id = body.get('session_id')
        if session_id:
            save_chat_history(b, history, session_id=session_id)
        else:
            session_id = save_chat_history(b, history)
        self.send_json({'board': b, 'saved': True, 'count': len(history), 'session_id': session_id})

    def _handle_post_chat_session(self, qs):
        """Handle rename, delete, or create session operations."""
        body = self._read_json_body()
        if body is None:
            return
        b = str(body.get('board') or self._board_from_request(qs) or DEFAULT_BOARD)
        action = str(body.get('action') or '').strip()
        session_id = str(body.get('session_id') or '').strip()
        if not session_id:
            self.send_json({'error': 'session_id is required'}, 400)
            return
        if action == 'rename':
            new_name = str(body.get('name') or '').strip()
            if not new_name:
                self.send_json({'error': 'name is required'}, 400)
                return
            ok = rename_chat_session(b, session_id, new_name)
            if ok:
                self.send_json({'board': b, 'session_id': session_id, 'renamed': True, 'name': new_name})
            else:
                self.send_json({'error': 'session not found'}, 404)
        elif action == 'delete':
            ok = delete_chat_session(b, session_id)
            if ok:
                # Return current session info for client to switch to
                store = _load_chat_store(b)
                current = store.get('current_session')
                self.send_json({'board': b, 'session_id': session_id, 'deleted': True, 'current_session': current})
            else:
                self.send_json({'error': 'session not found'}, 404)
        else:
            self.send_json({'error': f'unknown action: {action}'}, 400)

    def _handle_switch_chat_session(self, qs):
        body = self._read_json_body()
        if body is None:
            return
        b = str(body.get('board') or self._board_from_request(qs) or DEFAULT_BOARD)
        session_id = str(body.get('session_id') or '').strip()
        if not session_id:
            self.send_json({'error': 'session_id is required'}, 400)
            return
        ok = set_current_session(b, session_id)
        if ok:
            history = load_chat_history(b, session_id=session_id)
            self.send_json({'board': b, 'session_id': session_id, 'switched': True, 'history': history})
        else:
            self.send_json({'error': 'session not found'}, 404)

    def _handle_delete_chat_session(self, qs):
        """DELETE /api/chat-sessions?board=X&session=Y"""
        b = str(self._board_from_request(qs) or DEFAULT_BOARD)
        session_id = qs.get('session', [None])[0] if 'session' in qs else None
        if not session_id:
            self.send_json({'error': 'session parameter required'}, 400)
            return
        ok = delete_chat_session(b, session_id)
        if ok:
            store = _load_chat_store(b)
            current = store.get('current_session')
            self.send_json({'board': b, 'session_id': session_id, 'deleted': True, 'current_session': current})
        else:
            self.send_json({'error': 'session not found'}, 404)

    def do_PATCH(self):
        p, qs = self._query()
        if p.startswith('/api/tasks/'):
            self._handle_patch_task(p, qs)
            return
        self.send_json({'error': 'not found'}, 404)

    def do_DELETE(self):
        p, qs = self._query()
        if p == '/api/chat-sessions':
            self._handle_delete_chat_session(qs)
            return
        if p == '/api/boards' or p.startswith('/api/boards/'):
            self._handle_delete_board(p, qs)
            return
        if p.startswith('/api/profile/'):
            self._handle_delete_profile(p)
            return
        if p.startswith('/api/tasks/'):
            self._handle_delete_task(p, qs)
            return
        self.send_json({'error': 'not found'}, 404)

    def _handle_patch_task(self, path, qs):
        tid = path.rsplit('/', 1)[-1]
        body = self._read_json_body()
        if body is None:
            return
        board = str(body.get('board') or self._board_from_request(qs) or DEFAULT_BOARD)
        title = str(body.get('title', '')).strip()
        body_text = str(body.get('body', '')).strip()
        new_status = str(body.get('status', '')).strip().lower()
        new_assignee = str(body.get('assignee', '')).strip()
        if not title and not body_text and not new_status and not new_assignee:
            self.send_json({'error': 'at least one field (title, body, status, assignee) is required'}, 400)
            return
        # Validate status if provided
        valid_statuses = {'todo', 'ready', 'running', 'done', 'blocked', 'scheduled', 'review', 'triage', 'archived'}
        if new_status and new_status not in valid_statuses:
            self.send_json({'error': f'invalid status: {new_status}'}, 400)
            return
        try:
            import sqlite3
            sys.path.insert(0, str(Path(__file__).parent.parent / '.hermes' / 'hermes-agent'))
            from hermes_cli import kanban_db as kb
            with kb.connect(board=board) as conn:
                cur = conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,))
                if not cur.fetchone():
                    self.send_json({'error': 'task not found'}, 404)
                    return
                updates = []
                params = []
                if title:
                    updates.append("title = ?")
                    params.append(title)
                if body_text:
                    updates.append("body = ?")
                    params.append(body_text)
                if new_status:
                    updates.append("status = ?")
                    params.append(new_status)
                if new_assignee:
                    updates.append("assignee = ?")
                    params.append(new_assignee)
                if updates:
                    params.append(tid)
                    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
                    conn.execute(
                        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                        (tid, 'edited', json.dumps({'fields': [k.split()[0] for k in updates]}), int(time.time()))
                    )
        except Exception as exc:
            self.send_json({'error': str(exc)}, 500)
            return
        self.send_json({'id': tid, 'updated': True})
        event_bus.emit('task-changed', {'board': board, 'task_id': tid, 'action': 'edited'})

    def _handle_delete_task(self, path, qs):
        tid = path.rsplit('/', 1)[-1]
        board = self._board_from_request(qs)
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent / '.hermes' / 'hermes-agent'))
            from hermes_cli import kanban_db as kb
            with kb.connect(board=board) as conn:
                if not kb.delete_task(conn, tid):
                    self.send_json({'error': 'task not found'}, 404)
                    return
        except Exception as exc:
            self.send_json({'error': str(exc)}, 500)
            return
        self.send_json({'id': tid, 'deleted': True})
        event_bus.emit('task-changed', {'board': board, 'task_id': tid, 'action': 'deleted'})

    def _handle_transition_task(self, path, qs):
        tid = path.rsplit('/', 1)[-1]
        body = self._read_json_body()
        if body is None:
            return
        board = str(body.get('board') or self._board_from_request(qs) or DEFAULT_BOARD)
        new_status = str(body.get('status') or '').strip().lower()
        reason = str(body.get('reason') or '').strip()
        if not new_status:
            self.send_json({'error': 'status is required'}, 400)
            return
        valid = {'todo', 'ready', 'running', 'done', 'blocked', 'scheduled', 'review', 'triage', 'archived'}
        if new_status not in valid:
            self.send_json({'error': f'invalid status: {new_status}'}, 400)
            return
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent / '.hermes' / 'hermes-agent'))
            from hermes_cli import kanban_db as kb
            with kb.connect(board=board) as conn:
                cur = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,))
                row = cur.fetchone()
                if not row:
                    self.send_json({'error': 'task not found'}, 404)
                    return
                old_status = row[0]
                if old_status == new_status:
                    self.send_json({'id': tid, 'status': new_status, 'transitioned': False})
                    return
                # Use semantic helpers where available; fall back to raw UPDATE.
                ok = False
                if new_status == 'done':
                    ok = kb.complete_task(conn, tid, result=reason or None)
                elif new_status == 'blocked':
                    ok = kb.block_task(conn, tid, reason=reason or None)
                elif new_status == 'ready' and old_status == 'blocked':
                    ok = kb.unblock_task(conn, tid)
                elif new_status == 'archived':
                    ok = kb.archive_task(conn, tid)
                elif new_status == 'scheduled':
                    ok = kb.schedule_task(conn, tid, reason=reason or None)
                else:
                    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, tid))
                    conn.execute(
                        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                        (tid, 'transition', json.dumps({'from': old_status, 'to': new_status}), int(time.time()))
                    )
                    ok = True
                if not ok:
                    self.send_json({'error': f'cannot transition {tid} from {old_status} to {new_status}'}, 409)
                    return
        except Exception as exc:
            self.send_json({'error': str(exc)}, 500)
            return
        self.send_json({'id': tid, 'status': new_status, 'old_status': old_status, 'transitioned': True})
        event_bus.emit('task-changed', {'board': board, 'task_id': tid, 'action': 'transitioned', 'status': new_status})

    def _handle_reorder(self, qs):
        body = self._read_json_body()
        if body is None:
            return
        board = str(body.get('board') or self._board_from_request(qs) or DEFAULT_BOARD)
        order = body.get('order', [])
        if not isinstance(order, list):
            self.send_json({'error': 'order must be a list'}, 400)
            return
        order_path = BOARDS_ROOT / board / 'display_order.json'
        try:
            order_path.write_text(json.dumps(order, indent=2))
            self.send_json({'ok': True, 'board': board})
            event_bus.emit('board-update', {'board': board})
        except Exception as exc:
            self.send_json({'error': str(exc)}, 500)

    def _handle_get_profile(self, path):
        name = path.rsplit('/', 1)[-1]
        if name == 'default':
            config_path = HERMES_HOME / 'config.yaml'
        else:
            config_path = HERMES_HOME / 'profiles' / name / 'config.yaml'
        if not config_path.exists():
            self.send_json({'error': f'profile not found: {name}'}, 404)
            return
        try:
            raw = config_path.read_text(encoding='utf-8')
            try:
                import yaml
                data = yaml.safe_load(raw) or {}
            except ModuleNotFoundError:
                print('[kanban_browser] PyYAML missing; using fallback profile parser', file=sys.stderr, flush=True)
                data = _parse_simple_yaml_profile(raw)
        except Exception as exc:
            self.send_json({'error': f'failed to read profile: {exc}'}, 500)
            return
        model = data.get('model', {}) or {}
        agent = data.get('agent', {}) or {}
        display = data.get('display', {}) or {}
        self.send_json({
            'name': name,
            'model_default': model.get('default', ''),
            'provider': model.get('provider', ''),
            'base_url': model.get('base_url', ''),
            'context_length': str(model.get('context_length', '')) if model.get('context_length') is not None else '',
            'api_mode': model.get('api_mode', ''),
            'reasoning_effort': model.get('reasoning_effort', ''),
            'max_turns': str(agent.get('max_turns', '')) if agent.get('max_turns') is not None else '',
            'personality': display.get('personality', ''),
            'show_reasoning': display.get('show_reasoning', ''),
        })

    def _handle_update_profile(self, path, qs):
        name = path.rsplit('/', 1)[-1]
        body = self._read_json_body()
        if body is None:
            return
        if name == 'default':
            config_path = HERMES_HOME / 'config.yaml'
        else:
            config_path = HERMES_HOME / 'profiles' / name / 'config.yaml'
        if not config_path.exists():
            self.send_json({'error': f'profile not found: {name}'}, 404)
            return
        fields = {
            'model.default': body.get('model_default'),
            'model.provider': body.get('provider'),
            'model.base_url': body.get('base_url'),
            'model.context_length': body.get('context_length'),
            'model.api_mode': body.get('api_mode'),
            'model.reasoning_effort': body.get('reasoning_effort'),
            'agent.max_turns': body.get('max_turns'),
            'display.personality': body.get('personality'),
            'display.show_reasoning': body.get('show_reasoning'),
        }
        errors = []
        for key, value in fields.items():
            if value is None or value == '':
                continue
            try:
                proc = subprocess.run(
                    ['hermes', '-p', name, 'config', 'set', key, str(value)],
                    capture_output=True, text=True, env=os.environ.copy(),
                )
                if proc.returncode != 0:
                    errors.append(f'{key}: {proc.stderr.strip() or proc.stdout.strip() or "unknown error"}')
            except Exception as exc:
                errors.append(f'{key}: {exc}')
        if errors:
            self.send_json({'error': 'partial update failed', 'details': errors}, 500)
            return
        self.send_json({'name': name, 'updated': True})

    def _handle_create_profile(self, qs):
        body = self._read_json_body()
        if body is None:
            return
        name = str(body.get('name') or '').strip()
        if not name:
            self.send_json({'error': 'name is required'}, 400)
            return
        source = str(body.get('clone_from') or DEFAULT_PROFILE)
        try:
            proc = subprocess.run(
                ['hermes', 'profile', 'create', '--clone-from', source, '--no-alias', name],
                capture_output=True, text=True, env=os.environ.copy(),
            )
            if proc.returncode != 0:
                self.send_json({'error': proc.stderr.strip() or proc.stdout.strip() or 'profile create failed'}, 500)
                return
        except Exception as exc:
            self.send_json({'error': str(exc)}, 500)
            return
        # Apply any additional fields beyond the clone
        fields = {
            'model.default': body.get('model_default'),
            'model.provider': body.get('provider'),
            'model.base_url': body.get('base_url'),
            'model.context_length': body.get('context_length'),
            'model.api_mode': body.get('api_mode'),
            'model.reasoning_effort': body.get('reasoning_effort'),
            'agent.max_turns': body.get('max_turns'),
            'display.personality': body.get('personality'),
            'display.show_reasoning': body.get('show_reasoning'),
        }
        for key, value in fields.items():
            if value is None or value == '':
                continue
            try:
                subprocess.run(
                    ['hermes', '-p', name, 'config', 'set', key, str(value)],
                    capture_output=True, text=True, env=os.environ.copy(),
                )
            except Exception:
                pass
        self.send_json({'name': name, 'created': True})

    def _handle_delete_profile(self, path):
        name = path.rsplit('/', 1)[-1]
        try:
            proc = subprocess.run(
                ['hermes', 'profile', 'delete', '-y', name],
                capture_output=True, text=True, env=os.environ.copy(),
            )
            if proc.returncode != 0:
                self.send_json({'error': proc.stderr.strip() or proc.stdout.strip() or 'profile delete failed'}, 500)
                return
        except Exception as exc:
            self.send_json({'error': str(exc)}, 500)
            return
        self.send_json({'name': name, 'deleted': True})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    server = ThreadingHTTPServer(('0.0.0.0', PORT), H)
    print(f'Kanban browser on http://0.0.0.0:{PORT}')
    server.serve_forever()


if __name__ == '__main__':
    main()

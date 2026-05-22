"""
API surface tests for kanban_browser.py.

Covers gaps not tested by test_decoupled.py:
- SSE lifecycle (connect, event stream, disconnect)
- Drag-drop reorder API (/api/reorder)
- Profile CRUD endpoints
- Task transitions (ready -> running -> done/blocked)
- Chat session rename/delete
- Adaptive polling fallback endpoints (/api/ping, /api/list)
"""
import http.client
import json
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Bootstrap: temp HERMES_HOME + mock hermes_cli modules before importing kanban_browser
# ---------------------------------------------------------------------------

TEST_HERMES_HOME = tempfile.mkdtemp(prefix='test_hermes_')
os.environ['HERMES_HOME'] = TEST_HERMES_HOME
os.environ['HERMES_KANBAN_BOARD'] = 'test-board'
os.environ['HERMES_PROFILE'] = 'default'

# Shared file-based mock DB so multiple handler calls see the same data
_MOCK_DB_PATH = os.path.join(TEST_HERMES_HOME, 'mock_kanban.db')


class MockKanbanDB:
    """Minimal drop-in for hermes_cli.kanban_db used by patch/transition handlers."""

    @staticmethod
    def _ensure_schema(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)"
        )
        conn.commit()

    @classmethod
    def connect(cls, board=None):
        conn = sqlite3.connect(_MOCK_DB_PATH)
        cls._ensure_schema(conn)
        return conn

    @staticmethod
    def complete_task(conn, tid, result=None):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        return True

    @staticmethod
    def block_task(conn, tid, reason=None):
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        return True

    @staticmethod
    def unblock_task(conn, tid):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        return True

    @staticmethod
    def archive_task(conn, tid):
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,))
        return True

    @staticmethod
    def schedule_task(conn, tid, reason=None):
        conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (tid,))
        return True

    @staticmethod
    def delete_task(conn, tid):
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        return cur.rowcount > 0


mock_kb = types.ModuleType('hermes_cli.kanban_db')
for _attr in dir(MockKanbanDB):
    if not _attr.startswith('_'):
        setattr(mock_kb, _attr, getattr(MockKanbanDB, _attr))

hermes_cli_pkg = types.ModuleType('hermes_cli')
hermes_cli_pkg.kanban_db = mock_kb
sys.modules['hermes_cli'] = hermes_cli_pkg
sys.modules['hermes_cli.kanban_db'] = mock_kb

# Also mock hermes_cli.models so /api/models doesn't blow up
mock_models = types.ModuleType('hermes_cli.models')
mock_models._PROVIDER_MODELS = {}
mock_models.OPENROUTER_MODELS = []
hermes_cli_pkg.models = mock_models
sys.modules['hermes_cli.models'] = mock_models

sys.path.insert(0, str(Path(__file__).parent))
import kanban_browser as _kb
from kanban_browser import (
    H,
    event_bus,
    DEFAULT_BOARD,
    DEFAULT_PROFILE,
    chat_history_path,
    save_chat_history,
    load_chat_history,
    list_chat_sessions,
    rename_chat_session,
    delete_chat_session,
    set_current_session,
    _load_chat_store,
)
from http.server import ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Isolation mixin: patch HERMES_HOME / BOARDS_ROOT for the duration of our tests
# ---------------------------------------------------------------------------

class IsolatedKanbanMixin:
    @classmethod
    def setUpClass(cls):
        cls._orig_hermes_home = _kb.HERMES_HOME
        cls._orig_boards_root = _kb.BOARDS_ROOT
        _kb.HERMES_HOME = Path(TEST_HERMES_HOME)
        _kb.BOARDS_ROOT = _kb.HERMES_HOME / 'kanban' / 'boards'
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        _kb.HERMES_HOME = cls._orig_hermes_home
        _kb.BOARDS_ROOT = cls._orig_boards_root

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_mock_db():
    """Remove and recreate the shared mock SQLite DB."""
    if os.path.exists(_MOCK_DB_PATH):
        os.unlink(_MOCK_DB_PATH)
    conn = sqlite3.connect(_MOCK_DB_PATH)
    MockKanbanDB._ensure_schema(conn)
    conn.commit()
    conn.close()


def _seed_task(task_id, title, status, assignee='default', body=''):
    """Insert a task into the shared mock DB."""
    conn = sqlite3.connect(_MOCK_DB_PATH)
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, assignee) VALUES (?, ?, ?, ?, ?)",
        (task_id, title, body, status, assignee),
    )
    conn.commit()
    conn.close()


def _subprocess_side_effect(cmd, *args, **kwargs):
    """Configurable side-effect for subprocess.run mocks."""
    mock = MagicMock()
    if isinstance(cmd, list):
        cmd_str = ' '.join(cmd)
    else:
        cmd_str = str(cmd)

    if 'kanban list' in cmd_str:
        mock.stdout = f"Board: {DEFAULT_BOARD}\n● t_abc123 running default Test task\n◻ t_def456 todo default Another task"
        mock.stderr = ""
        mock.returncode = 0
    elif 'kanban show' in cmd_str:
        mock.stdout = "Task t_abc123: Test task\nStatus: running\nAssignee: default"
        mock.stderr = ""
        mock.returncode = 0
    elif 'kanban create' in cmd_str:
        mock.stdout = json.dumps({'id': 't_new123', 'title': 'Created task', 'status': 'todo', 'assignee': 'default'})
        mock.stderr = ""
        mock.returncode = 0
    elif 'hermes chat' in cmd_str or ('chat' in cmd_str and '-q' in cmd_str):
        mock.stdout = "╭─ Hermes ──────────────────╮\n    Hello from Hermes\n╰────────────────────────────╯"
        mock.stderr = ""
        mock.returncode = 0
    elif 'profile list' in cmd_str:
        mock.stdout = "default\noverseer\n"
        mock.stderr = ""
        mock.returncode = 0
    elif 'profile create' in cmd_str:
        mock.stdout = "Created profile"
        mock.stderr = ""
        mock.returncode = 0
    elif 'profile delete' in cmd_str:
        mock.stdout = "Deleted"
        mock.stderr = ""
        mock.returncode = 0
    elif 'config set' in cmd_str:
        mock.stdout = ""
        mock.stderr = ""
        mock.returncode = 0
    elif 'config show' in cmd_str:
        mock.stdout = "  Model: {'default': 'gpt-4', 'provider': 'openai'}"
        mock.stderr = ""
        mock.returncode = 0
    else:
        mock.stdout = ""
        mock.stderr = ""
        mock.returncode = 0
    return mock


class TestServerMixin:
    """Mixin that spins up KanbanHTTPHandler in a background thread on a random port."""

    @classmethod
    def setUpClass(cls):
        # Ensure the default board directory exists so reorder / list work
        board_dir = _kb.BOARDS_ROOT / DEFAULT_BOARD
        board_dir.mkdir(parents=True, exist_ok=True)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server_thread.join(timeout=3)

    def request(self, path, method='GET', data=None, headers=None, timeout=10):
        url = f'http://127.0.0.1:{self.port}{path}'
        req_data = None
        if data is not None:
            req_data = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=req_data, method=method)
        if req_data is not None:
            req.add_header('Content-Type', 'application/json')
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode('utf-8')
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = body
                return resp.status, parsed, body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8') if exc.fp else ''
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = body
            return exc.code, parsed, body


# ---------------------------------------------------------------------------
# EventBus unit tests
# ---------------------------------------------------------------------------

class TestEventBus(IsolatedKanbanMixin, unittest.TestCase):
    """Direct tests for the pub/sub primitive."""

    def tearDown(self):
        event_bus._subscribers.clear()

    def test_subscribe_and_emit(self):
        q = event_bus.subscribe('task-changed')
        event_bus.emit('task-changed', {'id': 't_1'})
        msg = q.get(timeout=1)
        self.assertEqual(msg['event'], 'task-changed')
        self.assertEqual(msg['data']['id'], 't_1')

    def test_unsubscribe_removes_queue(self):
        q = event_bus.subscribe('chat-message')
        event_bus.unsubscribe('chat-message', q)
        event_bus.emit('chat-message', {'text': 'hello'})
        with self.assertRaises(queue.Empty):
            q.get(timeout=0.1)

    def test_full_queue_silently_drops(self):
        q = event_bus.subscribe('board-update')
        # Fill the queue
        for i in range(65):
            event_bus.emit('board-update', {'n': i})
        # Should not raise; one event may be dropped
        self.assertTrue(q.get(timeout=1)['data']['n'] >= 0)


# ---------------------------------------------------------------------------
# SSE lifecycle tests
# ---------------------------------------------------------------------------

class TestSSELifecycle(IsolatedKanbanMixin, TestServerMixin, unittest.TestCase):
    """Server-Sent Events endpoint behaviour."""

    def tearDown(self):
        event_bus._subscribers.clear()

    def test_sse_headers(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        conn.request('GET', '/api/events?board=test-board')
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader('Content-Type'), 'text/event-stream')
        self.assertEqual(resp.getheader('Cache-Control'), 'no-cache')
        conn.close()

    def test_sse_connected_event(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        conn.request('GET', '/api/events?board=test-board')
        resp = conn.getresponse()
        lines = []
        for _ in range(10):
            try:
                line = resp.readline()
                if not line:
                    break
                lines.append(line.decode('utf-8').rstrip('\n'))
                if 'event: connected' in lines[-1]:
                    break
            except Exception:
                break
        self.assertTrue(any('event: connected' in ln for ln in lines))
        conn.close()

    def test_sse_receives_emitted_event(self):
        received = []

        def read_sse():
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
            conn.request('GET', '/api/events?board=test-board')
            resp = conn.getresponse()
            # Read connected event first
            for _ in range(5):
                line = resp.readline()
                if not line:
                    break
                received.append(line.decode('utf-8').rstrip('\n'))
            # Wait for our custom event
            for _ in range(15):
                try:
                    line = resp.readline()
                    if not line:
                        break
                    received.append(line.decode('utf-8').rstrip('\n'))
                except Exception:
                    break
            conn.close()

        t = threading.Thread(target=read_sse, daemon=True)
        t.start()
        time.sleep(0.3)
        event_bus.emit('task-changed', {'task_id': 't_sse1', 'action': 'created'})
        t.join(timeout=6)
        text = '\n'.join(received)
        self.assertIn('t_sse1', text)

    def test_sse_keepalive_sent_when_idle(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        conn.request('GET', '/api/events?board=test-board')
        resp = conn.getresponse()
        # Discard connected event
        for _ in range(5):
            resp.readline()
        # Wait for keepalive (sent after ~10s idle)
        found_keepalive = False
        for _ in range(30):
            try:
                line = resp.readline()
                if not line:
                    break
                if b': keepalive' in line:
                    found_keepalive = True
                    break
            except Exception:
                break
        self.assertTrue(found_keepalive)
        conn.close()


# ---------------------------------------------------------------------------
# Drag-drop reorder
# ---------------------------------------------------------------------------

class TestDragDropReorder(IsolatedKanbanMixin, TestServerMixin, unittest.TestCase):
    """POST /api/reorder persistence."""

    def tearDown(self):
        # Clean up display_order.json
        p = _kb.BOARDS_ROOT / DEFAULT_BOARD / 'display_order.json'
        if p.exists():
            p.unlink()

    def test_reorder_saves_display_order(self):
        status, parsed, raw = self.request('/api/reorder', method='POST', data={
            'board': DEFAULT_BOARD,
            'order': ['t_z', 't_a', 't_b'],
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed.get('ok'))
        order_path = _kb.BOARDS_ROOT / DEFAULT_BOARD / 'display_order.json'
        self.assertTrue(order_path.exists())
        saved = json.loads(order_path.read_text())
        self.assertEqual(saved, ['t_z', 't_a', 't_b'])

    def test_reorder_invalid_body(self):
        status, parsed, raw = self.request('/api/reorder', method='POST', data={
            'board': DEFAULT_BOARD,
            'order': 'not-a-list',
        })
        self.assertEqual(status, 400)
        self.assertIn('must be a list', parsed.get('error', ''))

    def test_reorder_emits_board_update(self):
        # Use event bus to verify emission
        q = event_bus.subscribe('board-update')
        status, parsed, raw = self.request('/api/reorder', method='POST', data={
            'board': DEFAULT_BOARD,
            'order': ['t_1'],
        })
        self.assertEqual(status, 200)
        msg = q.get(timeout=1)
        self.assertEqual(msg['data']['board'], DEFAULT_BOARD)


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------

class TestProfileCRUD(IsolatedKanbanMixin, TestServerMixin, unittest.TestCase):
    """GET /api/profile/<name>, POST /api/profile, POST /api/profile/<name>, DELETE /api/profile/<name>."""

    def setUp(self):
        self.profile_dir = Path(TEST_HERMES_HOME) / 'profiles' / 'test-profile'
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.profile_dir / 'config.yaml'
        self.config_path.write_text(
            "model:\n  default: gpt-4\n  provider: openai\nagent:\n  max_turns: 10\ndisplay:\n  personality: friendly\n",
            encoding='utf-8',
        )
        # Ensure default profile config exists
        default_config = Path(TEST_HERMES_HOME) / 'config.yaml'
        if not default_config.exists():
            default_config.write_text("model:\n  default: claude-sonnet\n", encoding='utf-8')

    def tearDown(self):
        if self.config_path.exists():
            self.config_path.unlink()
        if self.profile_dir.exists():
            self.profile_dir.rmdir()

    @patch('kanban_browser.subprocess.run', side_effect=_subprocess_side_effect)
    def test_get_profile(self, mock_run):
        status, parsed, raw = self.request('/api/profile/test-profile')
        self.assertEqual(status, 200)
        self.assertEqual(parsed['name'], 'test-profile')
        self.assertEqual(parsed['model_default'], 'gpt-4')
        self.assertEqual(parsed['provider'], 'openai')

    def test_get_profile_not_found(self):
        status, parsed, raw = self.request('/api/profile/nonexistent-profile-12345')
        self.assertEqual(status, 404)

    def test_get_default_profile(self):
        default_config = Path(TEST_HERMES_HOME) / 'config.yaml'
        default_config.write_text(
            "model:\n  default: claude-3\n  provider: anthropic\nagent:\n  max_turns: 20\ndisplay:\n  show_reasoning: true\n",
            encoding='utf-8',
        )
        status, parsed, raw = self.request('/api/profile/default')
        self.assertEqual(status, 200)
        self.assertEqual(parsed['name'], 'default')

    def test_get_profile_without_pyyaml(self):
        import builtins
        real_import = builtins.__import__

        def side_effect(name, *args, **kwargs):
            if name == 'yaml':
                raise ModuleNotFoundError("No module named 'yaml'")
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=side_effect):
            status, parsed, raw = self.request('/api/profile/test-profile')
        self.assertEqual(status, 200)
        self.assertEqual(parsed['name'], 'test-profile')
        self.assertEqual(parsed['model_default'], 'gpt-4')
        self.assertEqual(parsed['provider'], 'openai')

    @patch('kanban_browser.subprocess.run', side_effect=_subprocess_side_effect)
    def test_create_profile(self, mock_run):
        status, parsed, raw = self.request('/api/profile', method='POST', data={
            'name': 'new-profile',
            'clone_from': 'default',
            'model_default': 'gpt-4o',
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed.get('created'))
        self.assertEqual(parsed['name'], 'new-profile')

    @patch('kanban_browser.subprocess.run', side_effect=_subprocess_side_effect)
    def test_update_profile(self, mock_run):
        status, parsed, raw = self.request('/api/profile/test-profile', method='POST', data={
            'model_default': 'gpt-4-turbo',
            'provider': 'openai',
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed.get('updated'))

    @patch('kanban_browser.subprocess.run', side_effect=_subprocess_side_effect)
    def test_delete_profile(self, mock_run):
        status, parsed, raw = self.request('/api/profile/test-profile', method='DELETE')
        self.assertEqual(status, 200)
        self.assertTrue(parsed.get('deleted'))

    def test_create_profile_missing_name(self):
        status, parsed, raw = self.request('/api/profile', method='POST', data={})
        self.assertEqual(status, 400)
        self.assertIn('name is required', parsed.get('error', ''))


# ---------------------------------------------------------------------------
# Task transitions
# ---------------------------------------------------------------------------

class TestTaskTransitions(IsolatedKanbanMixin, TestServerMixin, unittest.TestCase):
    """POST /api/tasks/<id>/transition — all state machine paths."""

    def setUp(self):
        _reset_mock_db()

    def tearDown(self):
        event_bus._subscribers.clear()

    def test_transition_ready_to_running(self):
        _seed_task('t_trans1', 'Task one', 'ready')
        status, parsed, raw = self.request('/api/tasks/t_trans1/transition', method='POST', data={
            'board': DEFAULT_BOARD,
            'status': 'running',
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed['transitioned'])
        self.assertEqual(parsed['status'], 'running')
        self.assertEqual(parsed['old_status'], 'ready')

    def test_transition_running_to_done(self):
        _seed_task('t_trans2', 'Task two', 'running')
        status, parsed, raw = self.request('/api/tasks/t_trans2/transition', method='POST', data={
            'board': DEFAULT_BOARD,
            'status': 'done',
            'reason': 'shipped feature',
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed['transitioned'])
        self.assertEqual(parsed['status'], 'done')

    def test_transition_running_to_blocked(self):
        _seed_task('t_trans3', 'Task three', 'running')
        status, parsed, raw = self.request('/api/tasks/t_trans3/transition', method='POST', data={
            'board': DEFAULT_BOARD,
            'status': 'blocked',
            'reason': 'needs review',
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed['transitioned'])
        self.assertEqual(parsed['status'], 'blocked')

    def test_transition_blocked_to_ready(self):
        _seed_task('t_trans4', 'Task four', 'blocked')
        status, parsed, raw = self.request('/api/tasks/t_trans4/transition', method='POST', data={
            'board': DEFAULT_BOARD,
            'status': 'ready',
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed['transitioned'])
        self.assertEqual(parsed['status'], 'ready')

    def test_transition_same_status_noop(self):
        _seed_task('t_trans5', 'Task five', 'running')
        status, parsed, raw = self.request('/api/tasks/t_trans5/transition', method='POST', data={
            'board': DEFAULT_BOARD,
            'status': 'running',
        })
        self.assertEqual(status, 200)
        self.assertFalse(parsed['transitioned'])

    def test_transition_invalid_status(self):
        _seed_task('t_trans6', 'Task six', 'todo')
        status, parsed, raw = self.request('/api/tasks/t_trans6/transition', method='POST', data={
            'board': DEFAULT_BOARD,
            'status': 'invalid-status',
        })
        self.assertEqual(status, 400)

    def test_transition_task_not_found(self):
        status, parsed, raw = self.request('/api/tasks/t_missing/transition', method='POST', data={
            'board': DEFAULT_BOARD,
            'status': 'done',
        })
        self.assertEqual(status, 404)

    def test_transition_emits_event(self):
        q = event_bus.subscribe('task-changed')
        _seed_task('t_trans7', 'Task seven', 'ready')
        status, parsed, raw = self.request('/api/tasks/t_trans7/transition', method='POST', data={
            'board': DEFAULT_BOARD,
            'status': 'running',
        })
        self.assertEqual(status, 200)
        msg = q.get(timeout=1)
        self.assertEqual(msg['data']['action'], 'transitioned')
        self.assertEqual(msg['data']['task_id'], 't_trans7')


# ---------------------------------------------------------------------------
# Chat session rename / delete / switch
# ---------------------------------------------------------------------------

class TestChatSessionLifecycle(IsolatedKanbanMixin, TestServerMixin, unittest.TestCase):
    """POST /api/chat-sessions (rename, delete), DELETE /api/chat-sessions, POST /api/chat-sessions/switch."""

    def setUp(self):
        self.test_board = f'test-chat-sess-{os.getpid()}'
        # Prime a session
        save_chat_history(self.test_board, [{'role': 'user', 'content': 'hi', 'ts': 1000}])

    def tearDown(self):
        p = chat_history_path(self.test_board)
        if p.exists():
            p.unlink()

    def test_rename_session(self):
        sessions = list_chat_sessions(self.test_board)
        sid = sessions[0]['id']
        status, parsed, raw = self.request('/api/chat-sessions', method='POST', data={
            'board': self.test_board,
            'action': 'rename',
            'session_id': sid,
            'name': 'My New Name',
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed['renamed'])
        store = _load_chat_store(self.test_board)
        self.assertEqual(store['sessions'][sid]['name'], 'My New Name')

    def test_rename_session_missing_name(self):
        sessions = list_chat_sessions(self.test_board)
        sid = sessions[0]['id']
        status, parsed, raw = self.request('/api/chat-sessions', method='POST', data={
            'board': self.test_board,
            'action': 'rename',
            'session_id': sid,
        })
        self.assertEqual(status, 400)

    def test_delete_session(self):
        sessions = list_chat_sessions(self.test_board)
        sid = sessions[0]['id']
        status, parsed, raw = self.request('/api/chat-sessions', method='POST', data={
            'board': self.test_board,
            'action': 'delete',
            'session_id': sid,
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed['deleted'])
        store = _load_chat_store(self.test_board)
        self.assertNotIn(sid, store['sessions'])

    def test_delete_session_via_delete_method(self):
        sessions = list_chat_sessions(self.test_board)
        sid = sessions[0]['id']
        status, parsed, raw = self.request(
            f'/api/chat-sessions?board={self.test_board}&session={sid}',
            method='DELETE',
        )
        self.assertEqual(status, 200)
        self.assertTrue(parsed['deleted'])

    def test_delete_session_not_found(self):
        status, parsed, raw = self.request('/api/chat-sessions', method='POST', data={
            'board': self.test_board,
            'action': 'delete',
            'session_id': 'no-such-session',
        })
        self.assertEqual(status, 404)

    def test_switch_session(self):
        # Create two sessions
        sid1 = save_chat_history(self.test_board, [{'role': 'user', 'content': 'a', 'ts': 1}], session_id=None)
        sid2 = save_chat_history(self.test_board, [{'role': 'user', 'content': 'b', 'ts': 2}], session_id=None)
        status, parsed, raw = self.request('/api/chat-sessions/switch', method='POST', data={
            'board': self.test_board,
            'session_id': sid2,
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed['switched'])
        self.assertEqual(parsed['session_id'], sid2)
        self.assertEqual(len(parsed['history']), 1)

    def test_switch_session_not_found(self):
        status, parsed, raw = self.request('/api/chat-sessions/switch', method='POST', data={
            'board': self.test_board,
            'session_id': 'ghost-session',
        })
        self.assertEqual(status, 404)


# ---------------------------------------------------------------------------
# Adaptive polling / fallback endpoints
# ---------------------------------------------------------------------------

class TestAdaptivePollingEndpoints(IsolatedKanbanMixin, TestServerMixin, unittest.TestCase):
    """
    The frontend falls back from SSE to adaptive polling.
    It calls /api/ping to check server health and /api/list to refresh tasks.
    We verify those endpoints are stable and fast.
    """

    def test_ping(self):
        status, parsed, raw = self.request('/api/ping?board=test-board')
        self.assertEqual(status, 200)
        self.assertTrue(parsed.get('ok'))
        self.assertEqual(parsed.get('board'), 'test-board')

    @patch('kanban_browser.subprocess.run', side_effect=_subprocess_side_effect)
    def test_list(self, mock_run):
        status, parsed, raw = self.request('/api/list?board=test-board&profile=default')
        self.assertEqual(status, 200)
        self.assertEqual(parsed.get('board'), 'test-board')
        self.assertIsInstance(parsed.get('tasks'), list)
        self.assertTrue(len(parsed['tasks']) > 0)

    @patch('kanban_browser.subprocess.run', side_effect=_subprocess_side_effect)
    def test_show_task(self, mock_run):
        status, parsed, raw = self.request('/api/show/t_abc123?board=test-board&profile=default')
        self.assertEqual(status, 200)
        self.assertEqual(parsed.get('id'), 't_abc123')
        self.assertIn('raw', parsed)

    @patch('kanban_browser.subprocess.run', side_effect=_subprocess_side_effect)
    def test_list_boards(self, mock_run):
        status, parsed, raw = self.request('/api/boards')
        self.assertEqual(status, 200)
        self.assertIsInstance(parsed.get('boards'), list)

    @patch('kanban_browser.subprocess.run', side_effect=_subprocess_side_effect)
    def test_list_profiles(self, mock_run):
        status, parsed, raw = self.request('/api/profiles')
        self.assertEqual(status, 200)
        self.assertIsInstance(parsed.get('profiles'), list)


class TestBoardDeletion(IsolatedKanbanMixin, TestServerMixin, unittest.TestCase):
    """DELETE /api/boards/<slug>."""

    def setUp(self):
        # Ensure the default/current board exists for tests that delete it.
        self.current_board_dir = _kb.BOARDS_ROOT / DEFAULT_BOARD
        self.current_board_dir.mkdir(parents=True, exist_ok=True)
        self.old_board = f'old-board-{os.getpid()}'
        self.old_board_dir = _kb.BOARDS_ROOT / self.old_board
        self.old_board_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.old_board_dir.exists():
            shutil.rmtree(self.old_board_dir)
        self.current_board_dir.mkdir(parents=True, exist_ok=True)

    def test_delete_non_current_board(self):
        status, parsed, raw = self.request(f'/api/boards/{self.old_board}', method='DELETE')
        self.assertEqual(status, 200)
        self.assertTrue(parsed['deleted'])
        self.assertFalse(self.old_board_dir.exists())
        self.assertEqual(parsed['current_board']['slug'], DEFAULT_BOARD)

    def test_delete_current_board_falls_back(self):
        status, parsed, raw = self.request(f'/api/boards/{DEFAULT_BOARD}', method='DELETE')
        self.assertEqual(status, 200)
        self.assertTrue(parsed['deleted'])
        self.assertFalse(self.current_board_dir.exists())
        self.assertEqual(parsed['current_board']['slug'], 'default')

    def test_delete_default_board_rejected(self):
        status, parsed, raw = self.request('/api/boards/default', method='DELETE')
        self.assertEqual(status, 400)
        self.assertIn('cannot be deleted', parsed.get('error', ''))


# ---------------------------------------------------------------------------
# Chat endpoint & history POST
# ---------------------------------------------------------------------------

class TestChatEndpoint(IsolatedKanbanMixin, TestServerMixin, unittest.TestCase):
    """POST /api/chat and POST /api/chat-history."""

    def setUp(self):
        self.test_board = f'test-chat-endpoint-{os.getpid()}'

    def tearDown(self):
        p = chat_history_path(self.test_board)
        if p.exists():
            p.unlink()

    @patch('kanban_browser.subprocess.run', side_effect=_subprocess_side_effect)
    def test_chat_persists_history(self, mock_run):
        status, parsed, raw = self.request('/api/chat', method='POST', data={
            'board': self.test_board,
            'profile': 'default',
            'message': 'Hello bot',
            'history': [],
        })
        self.assertEqual(status, 200)
        self.assertIn('reply', parsed)
        history = load_chat_history(self.test_board)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['role'], 'user')
        self.assertEqual(history[0]['content'], 'Hello bot')

    def test_post_chat_history(self):
        status, parsed, raw = self.request('/api/chat-history', method='POST', data={
            'board': self.test_board,
            'history': [{'role': 'user', 'content': 'manual', 'ts': 1}],
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed['saved'])
        self.assertEqual(parsed['count'], 1)

    def test_post_chat_history_invalid(self):
        status, parsed, raw = self.request('/api/chat-history', method='POST', data={
            'board': self.test_board,
            'history': 'not-a-list',
        })
        self.assertEqual(status, 400)


# ---------------------------------------------------------------------------
# Task creation & patch
# ---------------------------------------------------------------------------

class TestTaskCreationAndPatch(IsolatedKanbanMixin, TestServerMixin, unittest.TestCase):
    """POST /api/create-task and PATCH /api/tasks/<id>."""

    def setUp(self):
        _reset_mock_db()

    def tearDown(self):
        event_bus._subscribers.clear()

    @patch('kanban_browser.subprocess.run', side_effect=_subprocess_side_effect)
    def test_create_task(self, mock_run):
        status, parsed, raw = self.request('/api/create-task', method='POST', data={
            'board': DEFAULT_BOARD,
            'title': 'New task',
            'body': 'Details here',
            'assignee': 'overseer',
        })
        self.assertEqual(status, 200)
        self.assertIn('task', parsed)

    def test_create_task_missing_title(self):
        status, parsed, raw = self.request('/api/create-task', method='POST', data={
            'board': DEFAULT_BOARD,
            'title': '',
        })
        self.assertEqual(status, 400)
        self.assertIn('title is required', parsed.get('error', ''))

    def test_patch_task(self):
        _seed_task('t_patch1', 'Old title', 'running')
        status, parsed, raw = self.request('/api/tasks/t_patch1', method='PATCH', data={
            'board': DEFAULT_BOARD,
            'title': 'New title',
            'status': 'done',
            'assignee': 'overseer',
        })
        self.assertEqual(status, 200)
        self.assertTrue(parsed['updated'])

    def test_patch_task_no_fields(self):
        _seed_task('t_patch2', 'Title', 'todo')
        status, parsed, raw = self.request('/api/tasks/t_patch2', method='PATCH', data={
            'board': DEFAULT_BOARD,
        })
        self.assertEqual(status, 400)

    def test_patch_task_invalid_status(self):
        _seed_task('t_patch3', 'Title', 'todo')
        status, parsed, raw = self.request('/api/tasks/t_patch3', method='PATCH', data={
            'board': DEFAULT_BOARD,
            'status': 'bogus',
        })
        self.assertEqual(status, 400)

    def test_delete_task(self):
        _seed_task('t_del1', 'To delete', 'todo')
        status, parsed, raw = self.request('/api/tasks/t_del1?board=test-board', method='DELETE')
        self.assertEqual(status, 200)
        self.assertTrue(parsed['deleted'])

    def test_delete_task_not_found(self):
        status, parsed, raw = self.request('/api/tasks/t_ghost?board=test-board', method='DELETE')
        self.assertEqual(status, 404)


# ---------------------------------------------------------------------------
# Chat export & models
# ---------------------------------------------------------------------------

class TestMiscEndpoints(IsolatedKanbanMixin, TestServerMixin, unittest.TestCase):
    """Cover remaining endpoints: /api/models, /api/chat-export, etc."""

    def setUp(self):
        self.test_board = f'test-misc-{os.getpid()}'
        save_chat_history(self.test_board, [{'role': 'user', 'content': 'export me', 'ts': 1000}])

    def tearDown(self):
        p = chat_history_path(self.test_board)
        if p.exists():
            p.unlink()

    def test_list_models(self):
        status, parsed, raw = self.request('/api/models')
        self.assertEqual(status, 200)
        self.assertIn('providers', parsed)

    def test_chat_export_json(self):
        status, parsed, raw = self.request(f'/api/chat-export?board={self.test_board}&format=json')
        self.assertEqual(status, 200)
        # raw is attachment response; parsed will be string since content-disposition isn't JSON
        self.assertIn('history', raw)

    def test_chat_export_markdown(self):
        status, parsed, raw = self.request(f'/api/chat-export?board={self.test_board}&format=markdown')
        self.assertEqual(status, 200)
        self.assertIn('# Chat Export:', raw)


if __name__ == '__main__':
    unittest.main(verbosity=2)

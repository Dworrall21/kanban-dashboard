"""
Tests for decoupled chat history and task creation flows.

Verifies:
1. Chat history persistence works independently of task creation
2. Task creation works independently of chat history
3. Chat endpoint persists messages server-side
4. Task creation can optionally reference a chat session
5. Both flows can operate in isolation
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add the kanban-browser dir to the path
sys.path.insert(0, str(Path(__file__).parent))

from kanban_browser import (
    chat_history_path,
    load_chat_history,
    save_chat_history,
    hermes_create_task,
    extract_hermes_reply,
    parse_tasks,
    BOARDS_ROOT,
    CHAT_HISTORY_MAX,
)


class TestChatHistoryPersistence(unittest.TestCase):
    """Test that chat history works independently of task creation."""

    def setUp(self):
        self.test_board = f'test-chat-{os.getpid()}'
        self.history_path = chat_history_path(self.test_board)

    def tearDown(self):
        if self.history_path.exists():
            self.history_path.unlink()

    def test_save_and_load_empty_history(self):
        """Chat history can save and load an empty list."""
        save_chat_history(self.test_board, [])
        result = load_chat_history(self.test_board)
        self.assertEqual(result, [])

    def test_save_and_load_messages(self):
        """Chat history persists messages with role and content."""
        messages = [
            {'role': 'user', 'content': 'Hello', 'ts': 1000},
            {'role': 'assistant', 'content': 'Hi there!', 'ts': 1001},
        ]
        save_chat_history(self.test_board, messages)
        result = load_chat_history(self.test_board)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['role'], 'user')
        self.assertEqual(result[0]['content'], 'Hello')
        self.assertEqual(result[1]['role'], 'assistant')

    def test_history_max_limit(self):
        """Chat history respects CHAT_HISTORY_MAX limit."""
        messages = [
            {'role': 'user', 'content': f'msg-{i}', 'ts': i}
            for i in range(CHAT_HISTORY_MAX + 50)
        ]
        save_chat_history(self.test_board, messages)
        result = load_chat_history(self.test_board)
        self.assertEqual(len(result), CHAT_HISTORY_MAX)
        # Should keep the most recent messages
        self.assertEqual(result[-1]['content'], f'msg-{CHAT_HISTORY_MAX + 49}')

    def test_load_nonexistent_board(self):
        """Loading history for a board with no history returns empty list."""
        result = load_chat_history('nonexistent-board-xyz')
        self.assertEqual(result, [])

    def test_chat_history_does_not_depend_on_tasks(self):
        """Chat history can be saved/loaded without any task existing."""
        messages = [
            {'role': 'user', 'content': 'Just chatting', 'ts': 1000},
            {'role': 'assistant', 'content': 'No tasks here', 'ts': 1001},
        ]
        save_chat_history(self.test_board, messages)
        result = load_chat_history(self.test_board)
        self.assertEqual(len(result), 2)
        # No task IDs, no task references — pure chat

    def test_multiple_boards_isolated(self):
        """Chat history for different boards is isolated."""
        board_a = f'test-board-a-{os.getpid()}'
        board_b = f'test-board-b-{os.getpid()}'
        try:
            save_chat_history(board_a, [{'role': 'user', 'content': 'A', 'ts': 1}])
            save_chat_history(board_b, [{'role': 'user', 'content': 'B', 'ts': 1}])
            result_a = load_chat_history(board_a)
            result_b = load_chat_history(board_b)
            self.assertEqual(result_a[0]['content'], 'A')
            self.assertEqual(result_b[0]['content'], 'B')
        finally:
            for board in [board_a, board_b]:
                p = chat_history_path(board)
                if p.exists():
                    p.unlink()


class TestTaskCreationIndependence(unittest.TestCase):
    """Test that task creation works independently of chat history."""

    @patch('kanban_browser.subprocess.run')
    def test_create_task_minimal(self, mock_run):
        """Task creation works with just a title — no chat needed."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                'id': 't_abc123',
                'title': 'Test task',
                'status': 'todo',
                'assignee': 'default',
            }),
            stderr='',
        )
        result = hermes_create_task('test-board', 'default', 'Test task', '')
        self.assertEqual(result['id'], 't_abc123')
        self.assertEqual(result['title'], 'Test task')

    @patch('kanban_browser.subprocess.run')
    def test_create_task_with_body(self, mock_run):
        """Task creation works with title + body."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                'id': 't_def456',
                'title': 'Task with body',
                'status': 'todo',
            }),
            stderr='',
        )
        result = hermes_create_task('test-board', 'default', 'Task with body', 'Some details')
        self.assertEqual(result['title'], 'Task with body')

    @patch('kanban_browser.subprocess.run')
    def test_create_task_with_chat_session(self, mock_run):
        """Task creation can optionally reference a chat session."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                'id': 't_ghi789',
                'title': 'From chat',
                'status': 'todo',
            }),
            stderr='',
        )
        result = hermes_create_task(
            'test-board', 'default', 'From chat', 'body',
            chat_session_id='sess_abc123',
        )
        self.assertEqual(result['chat_session_id'], 'sess_abc123')

    @patch('kanban_browser.subprocess.run')
    def test_create_task_with_parent(self, mock_run):
        """Task creation can reference a parent task."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                'id': 't_child1',
                'title': 'Child task',
                'status': 'todo',
            }),
            stderr='',
        )
        result = hermes_create_task(
            'test-board', 'default', 'Child task', '',
            parent_task_id='t_parent1',
        )
        self.assertEqual(result['parent_task_id'], 't_parent1')

    @patch('kanban_browser.subprocess.run')
    def test_create_task_without_chat_or_parent(self, mock_run):
        """Task creation works with no chat session and no parent."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                'id': 't_orphan',
                'title': 'Standalone task',
                'status': 'todo',
            }),
            stderr='',
        )
        result = hermes_create_task('test-board', 'default', 'Standalone task', '')
        self.assertNotIn('chat_session_id', result)
        self.assertNotIn('parent_task_id', result)


class TestParseTasks(unittest.TestCase):
    """Test task parsing from hermes kanban list output."""

    def test_parse_empty(self):
        self.assertEqual(parse_tasks(''), [])

    def test_parse_tasks(self):
        raw = """Board: test
● t_abc123 running default Test task one
◻ t_def456 todo default Another task
✓ t_789abc done default Completed task"""
        tasks = parse_tasks(raw)
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0]['id'], 't_abc123')
        self.assertEqual(tasks[0]['status'], 'running')
        self.assertEqual(tasks[0]['title'], 'Test task one')
        self.assertEqual(tasks[2]['status'], 'done')

    def test_parse_ignores_non_task_lines(self):
        raw = """Board: test
Some header line
● t_abc123 running default Real task
Another non-task line"""
        tasks = parse_tasks(raw)
        self.assertEqual(len(tasks), 1)


class TestExtractHermesReply(unittest.TestCase):
    """Test reply extraction from hermes output."""

    def test_box_format(self):
        output = """Some preamble
╭─ Hermes ──────────────────╮
    Hello, this is the reply.
    It has multiple lines.
╰────────────────────────────╯
Some epilogue"""
        reply = extract_hermes_reply(output)
        self.assertIn('Hello', reply)
        self.assertIn('multiple lines', reply)

    def test_fallback_last_line(self):
        output = "line1\nline2\nFinal reply line"
        reply = extract_hermes_reply(output)
        self.assertEqual(reply, 'Final reply line')

    def test_empty_output(self):
        self.assertEqual(extract_hermes_reply(''), '')


class TestChatHistoryPath(unittest.TestCase):
    """Test chat history path generation."""

    def test_path_includes_board(self):
        path = chat_history_path('my-board')
        self.assertIn('my-board', str(path))
        self.assertTrue(str(path).endswith('chat-history.json'))

    def test_path_under_boards_root(self):
        path = chat_history_path('test-board')
        self.assertTrue(str(path).startswith(str(BOARDS_ROOT)))


class TestEndToEndDecoupling(unittest.TestCase):
    """
    Integration-style tests verifying chat and task flows are independent.
    These test the server-side logic without requiring a running server.
    """

    def setUp(self):
        self.test_board = f'test-e2e-{os.getpid()}'
        self.history_path = chat_history_path(self.test_board)

    def tearDown(self):
        if self.history_path.exists():
            self.history_path.unlink()

    def test_chat_then_task_independent(self):
        """
        Scenario: User chats, then creates a task.
        The task creation should not depend on chat state.
        """
        # Step 1: Chat history is saved
        chat_messages = [
            {'role': 'user', 'content': 'I need a new feature', 'ts': 1000},
            {'role': 'assistant', 'content': 'I can help with that', 'ts': 1001},
        ]
        save_chat_history(self.test_board, chat_messages)
        loaded_chat = load_chat_history(self.test_board)
        self.assertEqual(len(loaded_chat), 2)

        # Step 2: Task creation happens independently
        # (mocked — no chat reference needed)
        with patch('kanban_browser.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({'id': 't_new', 'title': 'New feature', 'status': 'todo'}),
                stderr='',
            )
            task = hermes_create_task(self.test_board, 'default', 'New feature', '')

        self.assertEqual(task['id'], 't_new')
        # Chat history is unchanged
        reloaded = load_chat_history(self.test_board)
        self.assertEqual(len(reloaded), 2)

    def test_task_then_chat_independent(self):
        """
        Scenario: User creates a task first, then chats.
        Chat should work fine without any prior task.
        """
        # Step 1: Create task (no chat)
        with patch('kanban_browser.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({'id': 't_first', 'title': 'First task', 'status': 'todo'}),
                stderr='',
            )
            task = hermes_create_task(self.test_board, 'default', 'First task', '')
        self.assertEqual(task['title'], 'First task')

        # Step 2: Chat works independently
        chat_messages = [
            {'role': 'user', 'content': 'Hello after task', 'ts': 2000},
        ]
        save_chat_history(self.test_board, chat_messages)
        loaded = load_chat_history(self.test_board)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]['content'], 'Hello after task')

    def test_chat_only_no_tasks(self):
        """
        Scenario: User only chats, never creates a task.
        Chat history should persist fine.
        """
        for i in range(5):
            save_chat_history(self.test_board, [
                {'role': 'user', 'content': f'Message {i}', 'ts': 3000 + i},
                {'role': 'assistant', 'content': f'Reply {i}', 'ts': 3001 + i},
            ])
        loaded = load_chat_history(self.test_board)
        self.assertEqual(len(loaded), 2)  # Last save overwrites

    def test_task_only_no_chat(self):
        """
        Scenario: User only creates tasks, never chats.
        Task creation should work fine.
        """
        # No chat history exists
        chat = load_chat_history(self.test_board)
        self.assertEqual(chat, [])

        # Task creation still works
        with patch('kanban_browser.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({'id': 't_solo', 'title': 'Solo task', 'status': 'todo'}),
                stderr='',
            )
            task = hermes_create_task(self.test_board, 'default', 'Solo task', '')
        self.assertEqual(task['title'], 'Solo task')


if __name__ == '__main__':
    unittest.main(verbosity=2)

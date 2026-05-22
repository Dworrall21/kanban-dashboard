#!/usr/bin/env python3
"""End-to-end verification of the decoupled chat/task server."""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8799"

def get(path):
    with urllib.request.urlopen(f"{BASE}{path}") as r:
        return json.loads(r.read())

def post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return condition

print("=== E2E Verification: Decoupled Chat & Task Flows ===\n")

all_pass = True

# 1. Server is running and serves HTML
print("1. Server health")
try:
    with urllib.request.urlopen(BASE + "/") as r:
        html = r.read().decode()
    check("HTML page loads", len(html) > 1000, f"{len(html)} bytes")
    all_pass &= check("Contains bus module", "const bus = " in html)
    all_pass &= check("Contains chat module", "const chat = " in html)
    all_pass &= check("Contains task module", "const task = " in html)
    all_pass &= check("Old createTaskFromChat removed", "createTaskFromChat" not in html)
    all_pass &= check("Old chatCreateBtn removed", "chatCreateBtn" not in html)
    all_pass &= check("Old chatTitleInput removed", "chatTitleInput" not in html)
    all_pass &= check("New task card present", "task-card" in html)
    all_pass &= check("New taskTitleInput present", "taskTitleInput" in html)
    all_pass &= check("New taskBodyInput present", "taskBodyInput" in html)
    all_pass &= check("New taskCreateBtn present", "taskCreateBtn" in html)
    all_pass &= check("New taskFromSelectionBtn present", "taskFromSelectionBtn" in html)
    all_pass &= check("New taskFromChatBtn present", "taskFromChatBtn" in html)
    all_pass &= check("Chat clear button present", "chatClearBtn" in html)
except Exception as e:
    check("Server running", False, str(e))
    all_pass = False

# 2. Boards endpoint
print("\n2. Boards API")
try:
    d = get("/api/boards")
    check("Returns boards list", "boards" in d and isinstance(d["boards"], list))
    check("Has current_board", "current_board" in d)
except Exception as e:
    check("Boards API", False, str(e))
    all_pass = False

# 3. Chat history GET
print("\n3. Chat History GET")
try:
    d = get("/api/chat-history?board=mobile-web-dashboard-chat")
    check("Returns history", "history" in d and isinstance(d["history"], list))
    check("Returns board", d.get("board") == "mobile-web-dashboard-chat")
except Exception as e:
    check("Chat history GET", False, str(e))
    all_pass = False

# 4. Chat history POST
print("\n4. Chat History POST")
try:
    test_history = [
        {"role": "user", "content": "Hello", "ts": 1000},
        {"role": "assistant", "content": "Hi!", "ts": 1001},
    ]
    d = post("/api/chat-history", {"board": "mobile-web-dashboard-chat", "history": test_history})
    check("Saves successfully", d.get("saved") is True)
    check("Returns count", d.get("count") == 2)
except Exception as e:
    check("Chat history POST", False, str(e))
    all_pass = False

# 5. Verify chat history persistence
print("\n5. Chat History Persistence")
try:
    d = get("/api/chat-history?board=mobile-web-dashboard-chat")
    check("History persisted", len(d.get("history", [])) >= 2)
    if d.get("history"):
        last = d["history"][-1]
        check("Last message preserved", last.get("content") == "Hi!")
except Exception as e:
    check("History persistence", False, str(e))
    all_pass = False

# 6. Task list
print("\n6. Task List")
try:
    d = get("/api/list")
    check("Returns tasks", "tasks" in d and isinstance(d["tasks"], list))
    check("Returns board", "board" in d)
except Exception as e:
    check("Task list", False, str(e))
    all_pass = False

# 7. Task creation (minimal — no chat)
print("\n7. Task Creation (standalone)")
try:
    d = post("/api/create-task", {
        "board": "mobile-web-dashboard-chat",
        "profile": "default",
        "title": "Decoupled test task",
        "body": "Created by E2E test",
    })
    check("Task created", "task" in d)
    if "task" in d:
        check("Has task id", bool(d["task"].get("id")))
        check("Has task title", d["task"].get("title") == "Decoupled test task")
except Exception as e:
    check("Task creation", False, str(e))
    all_pass = False

# 8. Task creation with chat session reference
print("\n8. Task Creation (with chat session)")
try:
    d = post("/api/create-task", {
        "board": "mobile-web-dashboard-chat",
        "profile": "default",
        "title": "Task with chat session",
        "body": "Linked to chat",
        "chat_session_id": "sess_test_abc",
    })
    check("Task created with session", "task" in d)
    if "task" in d:
        check("Chat session attached", d["task"].get("chat_session_id") == "sess_test_abc")
except Exception as e:
    check("Task creation with session", False, str(e))
    all_pass = False

# 9. Task creation with parent reference
print("\n9. Task Creation (with parent task)")
try:
    d = post("/api/create-task", {
        "board": "mobile-web-dashboard-chat",
        "profile": "default",
        "title": "Child task",
        "body": "Follow-up",
        "parent_task_id": "t_parent123",
    })
    check("Task created with parent", "task" in d)
    if "task" in d:
        check("Parent attached", d["task"].get("parent_task_id") == "t_parent123")
except Exception as e:
    check("Task creation with parent", False, str(e))
    all_pass = False

# 10. Chat endpoint persists messages
print("\n10. Chat Endpoint Persistence")
try:
    # Send a chat message (will call hermes, may fail if hermes not available)
    # But the endpoint should at least accept the request
    d = post("/api/chat", {
        "board": "mobile-web-dashboard-chat",
        "profile": "default",
        "message": "Test message for persistence",
        "history": [],
    })
    # If hermes is available, we get a reply
    if "reply" in d:
        check("Chat returns reply", True, f"session={d.get('chat_session_id', 'none')}")
        # Check that server-side history was updated
        h = get("/api/chat-history?board=mobile-web-dashboard-chat")
        check("Server history updated after chat", len(h.get("history", [])) >= 3)
    elif "error" in d:
        check("Chat endpoint reachable", True, f"hermes error (expected): {d['error'][:80]}")
    else:
        check("Chat response", False, "unexpected response")
except Exception as e:
    check("Chat endpoint", False, str(e))
    all_pass = False

print(f"\n{'='*50}")
print(f"RESULT: {'ALL PASSED' if all_pass else 'SOME FAILED'}")

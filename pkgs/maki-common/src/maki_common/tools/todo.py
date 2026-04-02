"""Todo list tools — NATS KV-backed task queue for night work sessions."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)


def make_todo_tools(
    todo_kv: Any,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for todo tools.

    Args:
        todo_kv: NATS KV bucket for todo storage.
    """

    async def add_todo(args: dict[str, Any]) -> dict[str, Any]:
        """Add a new todo item."""
        title = args.get("title", "")
        description = args.get("description", "")
        priority = args.get("priority", "3")
        log.info("Tool: add_todo", extra={"title": title, "priority": priority})

        if not title:
            return mcp_result("Error: title is required.")

        try:
            priority_int = int(priority)
            if not 1 <= priority_int <= 5:
                priority_int = 3
        except (ValueError, TypeError):
            priority_int = 3

        todo_id = uuid.uuid4().hex[:12]
        todo = {
            "id": todo_id,
            "title": title,
            "description": description,
            "priority": priority_int,
            "status": "pending",
            "created_at": time.time(),
            "source": "cortex",
        }

        await todo_kv.put(todo_id, json.dumps(todo).encode())
        return mcp_result(f"Todo added: [{todo_id}] (P{priority_int}) {title}")

    async def list_todos(args: dict[str, Any]) -> dict[str, Any]:
        """List all todo items, optionally filtered by status."""
        status_filter = args.get("status", "")
        log.info("Tool: list_todos", extra={"status_filter": status_filter})

        try:
            keys = await todo_kv.keys()
        except Exception:
            return mcp_result("No todos found.")

        todos = []
        for key in keys:
            try:
                entry = await todo_kv.get(key)
                todo = json.loads(entry.value.decode())
                if status_filter and todo.get("status") != status_filter:
                    continue
                todos.append(todo)
            except Exception:
                continue

        if not todos:
            status_msg = f" with status '{status_filter}'" if status_filter else ""
            return mcp_result(f"No todos found{status_msg}.")

        todos.sort(key=lambda t: (t.get("priority", 5), t.get("created_at", 0)))

        lines = []
        for t in todos:
            status = t.get("status", "?")
            priority = t.get("priority", "?")
            title = t.get("title", "?")
            todo_id = t.get("id", "?")
            desc = t.get("description", "")
            line = f"[{todo_id}] P{priority} ({status}) {title}"
            if desc:
                line += f"\n  {desc}"
            result_text = t.get("result", "")
            if result_text:
                line += f"\n  Result: {result_text}"
            lines.append(line)

        return mcp_result("\n".join(lines))

    async def update_todo(args: dict[str, Any]) -> dict[str, Any]:
        """Update a todo item's fields."""
        todo_id = args.get("id", "")
        log.info("Tool: update_todo", extra={"todo_id": todo_id})

        if not todo_id:
            return mcp_result("Error: id is required.")

        try:
            entry = await todo_kv.get(todo_id)
            todo = json.loads(entry.value.decode())
        except Exception:
            return mcp_result(f"Error: todo '{todo_id}' not found.")

        new_status = args.get("status", "")
        new_priority = args.get("priority", "")
        new_description = args.get("description", "")

        if new_status:
            todo["status"] = new_status
        if new_priority:
            try:
                todo["priority"] = int(new_priority)
            except (ValueError, TypeError):
                pass
        if new_description:
            todo["description"] = new_description

        todo["updated_at"] = time.time()
        await todo_kv.put(todo_id, json.dumps(todo).encode())
        return mcp_result(f"Todo updated: [{todo_id}] P{todo['priority']} ({todo['status']}) {todo['title']}")

    async def complete_todo(args: dict[str, Any]) -> dict[str, Any]:
        """Mark a todo as completed with an optional result summary."""
        todo_id = args.get("id", "")
        result_text = args.get("result", "")
        log.info("Tool: complete_todo", extra={"todo_id": todo_id})

        if not todo_id:
            return mcp_result("Error: id is required.")

        try:
            entry = await todo_kv.get(todo_id)
            todo = json.loads(entry.value.decode())
        except Exception:
            return mcp_result(f"Error: todo '{todo_id}' not found.")

        todo["status"] = "completed"
        todo["completed_at"] = time.time()
        if result_text:
            todo["result"] = result_text

        await todo_kv.put(todo_id, json.dumps(todo).encode())
        return mcp_result(f"Todo completed: [{todo_id}] {todo['title']}")

    return [
        (
            "add_todo",
            "Add a new todo item to the task queue. Priority 1 (highest) to 5 (lowest). "
            "These tasks are executed during night work sessions.",
            {"title": str, "description": str, "priority": str},
            add_todo,
        ),
        (
            "list_todos",
            "List all todo items. Optionally filter by status: pending, in_progress, completed.",
            {"status": str},
            list_todos,
        ),
        (
            "update_todo",
            "Update a todo item. Can change status, priority, or description.",
            {"id": str, "status": str, "priority": str, "description": str},
            update_todo,
        ),
        (
            "complete_todo",
            "Mark a todo as completed. Provide an optional result summary of what was done.",
            {"id": str, "result": str},
            complete_todo,
        ),
    ]

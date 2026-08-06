"""SQLite CRUD for conversations / messages / answers.

These three tables live in the same kb.sqlite the pipeline builds (see
docs/spec/01_data_contracts.md) but kb/build_index.py never touches them — see
that module's docstring for why a full pipeline rebuild must not destroy chat
history. This module is the only place that writes to them from the web app
side (kb/mcp_server.py's publish_answer writes directly with the same schema,
but through its own connection — no shared state beyond the file).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from . import config


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.KB_SQLITE)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- conversations -----------------------------------------------------

def create_conversation(name: str) -> int:
    conn = _connect()
    try:
        cur = conn.execute("INSERT INTO conversations (name, created_at) VALUES (?, ?)",
                          (name, _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_conversations() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM conversations ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def rename_conversation(conversation_id: int, name: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE conversations SET name = ? WHERE id = ?", (name, conversation_id))
        conn.commit()
    finally:
        conn.close()


def delete_conversation(conversation_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        conn.commit()
    finally:
        conn.close()


# --- messages ------------------------------------------------------------

def add_message(conversation_id: int, role: str, text: str | None = None,
                answer_id: str | None = None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, text, answer_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (conversation_id, role, text, answer_id, _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_messages(conversation_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d["answer_id"]:
                answer = get_answer(d["answer_id"], _conn=conn)
                d["payload"] = answer["payload"] if answer else None
            out.append(d)
        return out
    finally:
        conn.close()


# --- answers ---------------------------------------------------------------

def create_answer(kind: str, payload: dict, conversation_id: int | None = None,
                  title: str | None = None) -> str:
    answer_id = uuid.uuid4().hex[:12]
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO answers (id, kind, conversation_id, title, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (answer_id, kind, conversation_id, title,
             json.dumps(payload, ensure_ascii=False), _now()))
        conn.commit()
    finally:
        conn.close()
    return answer_id


def get_answer(answer_id: str, _conn: sqlite3.Connection | None = None) -> dict | None:
    conn = _conn or _connect()
    try:
        row = conn.execute("SELECT * FROM answers WHERE id = ?", (answer_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d
    finally:
        if _conn is None:
            conn.close()


def list_answers(kind: str | None = None) -> list[dict]:
    conn = _connect()
    try:
        if kind:
            rows = conn.execute(
                "SELECT id, kind, conversation_id, title, created_at FROM answers "
                "WHERE kind = ? ORDER BY created_at DESC", (kind,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, kind, conversation_id, title, created_at FROM answers "
                "ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

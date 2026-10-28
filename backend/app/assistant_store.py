"""Local account-isolated chat history. Independent of MySQL business transactions."""
import json
import sqlite3
from copy import deepcopy
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from backend.app.knowledge import KnowledgeError


def now():
    return datetime.now(UTC).isoformat()


def professional_labels(answer):
    """Normalize display labels without modifying questions, quotes or source excerpts."""
    result = deepcopy(answer)
    for key in ('message',):
        if isinstance(result.get(key), str): result[key] = result[key].replace('真人', '人工')
    for key in ('claims', 'fault_types', 'checks', 'part_decisions'):
        for row in result.get(key, []):
            row['text'] = row['text'].replace('真人', '人工')
    for key in ('follow_up_questions', 'limitations'):
        result[key] = [s.replace('真人', '人工') for s in result.get(key, [])]
    return result


class AssistantStore:
    def __init__(self, root):
        self.path = root / "data/assistant.sqlite3"

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start(self, *, recover=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
                    client_request_id TEXT NOT NULL, question TEXT NOT NULL, scope TEXT NOT NULL,
                    status TEXT NOT NULL, answer TEXT, error_code TEXT, error_detail TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(thread_id, client_request_id));
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_turn ON turns(thread_id) WHERE status='pending';
            """)
            if 'title_mode' not in {r['name'] for r in db.execute('PRAGMA table_info(threads)')}:
                db.execute("ALTER TABLE threads ADD COLUMN title_mode TEXT NOT NULL DEFAULT 'legacy'")
            if recover:
                db.execute("UPDATE turns SET status='failed',error_code='interrupted',error_detail=?,updated_at=? WHERE status='pending'",
                    ("服务重启，本轮尚未取得完整回答，请显式重试。", now()))

    def owned(self, db, user_id, thread_id):
        row = db.execute("SELECT * FROM threads WHERE id=? AND user_id=?", (thread_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "会话不存在或无权访问")
        return dict(row)

    def create(self, user_id):
        row = {"id": uuid4().hex, "title": "新会话", "created_at": now(), "updated_at": now()}
        with self.connection() as db:
            db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at,title_mode) VALUES (?,?,?,?,?,'pending')", (row["id"], user_id, row["title"], row["created_at"], row["updated_at"]))
        return row

    def threads(self, user_id):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT id,title,created_at,updated_at FROM threads WHERE user_id=? ORDER BY updated_at DESC LIMIT 100", (user_id,))]

    def history(self, user_id, thread_id):
        with self.connection() as db:
            thread = self.owned(db, user_id, thread_id)
            rows = [dict(r) for r in db.execute("SELECT * FROM turns WHERE thread_id=? ORDER BY created_at,id", (thread_id,))]
        for row in rows:
            row["answer"] = professional_labels(json.loads(row["answer"])) if row["answer"] else None
        return thread | {"turns": rows}

    def begin(self, user_id, thread_id, client_id, question, scope):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.owned(db, user_id, thread_id)
            old = db.execute("SELECT * FROM turns WHERE thread_id=? AND client_request_id=?", (thread_id, client_id)).fetchone()
            if old:
                if old["question"] != question or old["scope"] != scope:
                    raise KnowledgeError("turn_conflict", "相同请求标识的内容或资料范围发生变化")
                if old["status"] == "completed":
                    return old["id"], professional_labels(json.loads(old["answer"]))
                if old["status"] == "pending":
                    raise KnowledgeError("turn_in_progress", "本轮正在处理，请稍后刷新会话")
            if db.execute("SELECT 1 FROM turns WHERE thread_id=? AND status='pending'", (thread_id,)).fetchone():
                raise KnowledgeError("turn_in_progress", "会话尚有一轮正在处理，请稍后刷新")
            if old:
                latest = db.execute("SELECT id FROM turns WHERE thread_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (thread_id,)).fetchone()
                if latest["id"] != old["id"]:
                    raise KnowledgeError("turn_conflict", "只能重试最后一轮失败消息；其他问题请重新发送")
                turn_id = old["id"]
                db.execute("UPDATE turns SET status='pending',error_code=NULL,error_detail=NULL,updated_at=? WHERE id=?", (now(), turn_id))
            else:
                turn_id = uuid4().hex
                db.execute("INSERT INTO turns (id,thread_id,client_request_id,question,scope,status,created_at,updated_at) VALUES (?,?,?,?,?,'pending',?,?)",
                    (turn_id, thread_id, client_id, question, scope, now(), now()))
                db.execute("UPDATE threads SET title=CASE WHEN title_mode='pending' AND title='新会话' THEN ? ELSE title END,updated_at=? WHERE id=?", (question[:28], now(), thread_id))
        return turn_id, None

    def needs_title(self, user_id, thread_id):
        with self.connection() as db:
            thread = self.owned(db, user_id, thread_id)
            return thread['title_mode'] == 'pending' and not db.execute("SELECT 1 FROM turns WHERE thread_id=? AND status='completed'", (thread_id,)).fetchone()

    def finish(self, turn_id, *, answer=None, error=None, title=None):
        if answer is not None:
            answer = professional_labels(answer)
        with self.connection() as db:
            db.execute("UPDATE turns SET status=?,answer=?,error_code=?,error_detail=?,updated_at=? WHERE id=? AND status='pending'",
                ("completed" if answer is not None else "failed", json.dumps(answer, ensure_ascii=False) if answer is not None else None,
                 error.code if error else None, error.detail if error else None, now(), turn_id))
            db.execute("UPDATE threads SET updated_at=? WHERE id=(SELECT thread_id FROM turns WHERE id=?)", (now(), turn_id))
            if title is not None and answer is not None:
                db.execute("UPDATE threads SET title=?,title_mode='generated' WHERE title_mode='pending' AND id=(SELECT thread_id FROM turns WHERE id=? AND status='completed')", (title, turn_id))

    def delete(self, user_id, thread_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.owned(db, user_id, thread_id)
            if db.execute("SELECT 1 FROM turns WHERE thread_id=? AND status='pending'", (thread_id,)).fetchone():
                raise KnowledgeError("turn_in_progress", "会话正在处理，完成后才能删除")
            db.execute("DELETE FROM turns WHERE thread_id=?", (thread_id,))
            db.execute("DELETE FROM threads WHERE id=? AND user_id=?", (thread_id, user_id))

    def rename(self, user_id, thread_id, title):
        with self.connection() as db:
            self.owned(db, user_id, thread_id)
            db.execute("UPDATE threads SET title=?,title_mode='manual',updated_at=? WHERE id=? AND user_id=?", (title, now(), thread_id, user_id))
        return {"id": thread_id, "title": title}

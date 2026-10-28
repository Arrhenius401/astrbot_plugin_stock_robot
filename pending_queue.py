"""服务不可用时的会话等待队列；SQLite 事务保证跨重载和跨进程一致性。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

CONFIRM_SECONDS = 30 * 60
MAX_TARGETS = 3


def canonical_symbol(kind: str, raw: str) -> str:
    """合并同一港股的常用写法，并固定黄金市场简报的内部身份。"""
    value = raw.strip().upper()
    if kind == "gold":
        if value != "GOLD":
            raise ValueError("黄金分析标识无效")
        return "GOLD"
    if kind not in {"stock", "index"}:
        raise ValueError("未知分析类型")
    if kind == "stock":
        hk = re.fullmatch(r"(?:HK)?(\d{5})(?:\.HK)?", value)
        if hk:
            return hk.group(1) + ".HK"
    domestic = re.fullmatch(r"(?:SH|SZ)?(\d{6})", value)
    if domestic:
        return domestic.group(1)
    if not value or len(value) > 32 or not re.fullmatch(r"[A-Z0-9._-]+", value):
        raise ValueError("标的代码格式无效")
    return value


class PendingQueue:
    """每个会话共享三项；确认后的任务独立留存，不能由新请求撤销。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS waiting (umo TEXT PRIMARY KEY, body TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS accepted (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _get(conn: sqlite3.Connection, umo: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT body FROM waiting WHERE umo=?", (umo,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute("INSERT OR REPLACE INTO waiting VALUES (?, ?)",
                     (record["umo"], json.dumps(record, ensure_ascii=False)))

    def get(self, umo: str) -> dict[str, Any] | None:
        with self._transaction() as conn:
            return self._get(conn, umo)

    def all(self) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            return [json.loads(row[0]) for row in conn.execute("SELECT body FROM waiting")]

    def accepted(self) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            records = [json.loads(row[0]) for row in conn.execute("SELECT body FROM accepted")]
            return [record for record in records if record["state"] == "accepted"]

    def save_accepted(self, record: dict[str, Any]) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE accepted SET body=? WHERE id=?",
                         (json.dumps(record, ensure_ascii=False), record["id"]))

    def finish(self, record_id: str) -> None:
        with self._transaction() as conn:
            row = conn.execute("SELECT body FROM accepted WHERE id=?", (record_id,)).fetchone()
            if not row:
                return
            record = json.loads(row[0])
            if any(step.get("state") == "failed" for step in record.get("steps", [])):
                record.update(state="failed", ended=time.time())
                conn.execute("UPDATE accepted SET body=? WHERE id=?",
                             (json.dumps(record, ensure_ascii=False), record_id))
            else:
                conn.execute("DELETE FROM accepted WHERE id=?", (record_id,))

    def enqueue(self, umo: str, kind: str, symbol: str) -> dict[str, str] | None:
        target = {"kind": kind, "symbol": canonical_symbol(kind, symbol)}
        if not umo:
            raise ValueError("会话不能为空")
        with self._transaction() as conn:
            old = self._get(conn, umo)
            targets = [item for item in old["targets"] if item != target] if old else []
            targets.append(target)
            evicted = targets.pop(0) if len(targets) > MAX_TARGETS else None
            # 新积压请求使未投递的旧询问失效；只有新询问发送成功才开始计时。
            self._save(conn, {"id": uuid.uuid4().hex, "umo": umo,
                              "state": "waiting_service", "targets": targets,
                              "question": "", "deadline": None})
            return evicted

    def asked(self, umo: str, record_id: str, question: str, *, now: float | None = None) -> bool:
        if not question.strip():
            raise ValueError("询问不能为空")
        with self._transaction() as conn:
            record = self._get(conn, umo)
            if record is None or record["id"] != record_id or record["state"] != "waiting_service":
                return False
            record.update(state="awaiting_confirmation", question=question,
                          deadline=(time.time() if now is None else now) + CONFIRM_SECONDS)
            self._save(conn, record)
            return True

    def notification_failed(self, umo: str, record_id: str, *, now: float) -> None:
        with self._transaction() as conn:
            record = self._get(conn, umo)
            if record is None or record["id"] != record_id:
                return
            attempts = record.get("notification_attempts", 0) + 1
            record.update(notification_attempts=attempts, notification_retry_at=now + 30)
            if attempts >= 3:
                record.update(state="notification_failed", failed_at=now)
            self._save(conn, record)

    def cache_question(self, umo: str, record_id: str, text: str) -> bool:
        with self._transaction() as conn:
            record = self._get(conn, umo)
            if record is None or record["id"] != record_id:
                return False
            record["question"] = text
            self._save(conn, record)
            return True

    def discard(self, umo: str) -> bool:
        with self._transaction() as conn:
            return conn.execute("DELETE FROM waiting WHERE umo=?", (umo,)).rowcount > 0

    def confirm(self, umo: str, record_id: str, *, symbols: list[str] | None = None,
                now: float | None = None) -> dict[str, Any] | None:
        instant = time.time() if now is None else now
        with self._transaction() as conn:
            record = self._get(conn, umo)
            if (record is None or record["id"] != record_id
                    or record["state"] != "awaiting_confirmation"
                    or instant >= record["deadline"]):
                return None
            if symbols is not None:
                selected = set(symbols)
                available = {item["symbol"] for item in record["targets"]}
                if not selected or not selected <= available:
                    raise ValueError("确认标的必须属于当前等待队列")
                record["targets"] = [item for item in record["targets"] if item["symbol"] in selected]
            record.update(state="accepted", accepted_at=instant)
            conn.execute("INSERT INTO accepted VALUES (?, ?)",
                         (record["id"], json.dumps(record, ensure_ascii=False)))
            conn.execute("DELETE FROM waiting WHERE umo=?", (umo,))
            return record

    def expire(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """先取消执行资格，再返回唯一的末次提醒任务，避免提醒失败仍可确认。"""
        instant = time.time() if now is None else now
        expired = []
        with self._transaction() as conn:
            for (body,) in conn.execute("SELECT body FROM waiting").fetchall():
                record = json.loads(body)
                if record.get("failed_at") is not None and instant >= record["failed_at"] + 72 * 3600:
                    conn.execute("DELETE FROM waiting WHERE umo=?", (record["umo"],))
                    continue
                if record["deadline"] is not None and instant >= record["deadline"]:
                    conn.execute("DELETE FROM waiting WHERE umo=?", (record["umo"],))
                    expired.append(record)
            for record_id, body in conn.execute("SELECT id, body FROM accepted").fetchall():
                record = json.loads(body)
                if record["state"] == "failed" and instant >= record["ended"] + 72 * 3600:
                    conn.execute("DELETE FROM accepted WHERE id=?", (record_id,))
        return expired

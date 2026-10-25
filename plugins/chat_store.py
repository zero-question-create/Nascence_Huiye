# plugins/chat_store.py
# ========================================================================
# 聊天记录持久化：QQ / 微信共用。
#
# 目的：
#   - 每条消息（包括软件未打开时的未读）都落盘，保留原始时间戳；
#   - 记忆写入可以用消息时间作为 creation_time，时间前缀才是“当时”的；
#   - 动作向上翻历史时，可以从磁盘继续取，不受内存 transcript 上限限制。
#
# 存储：data/test/<root>/<sha1(conversation_id)>.jsonl，一行一条 JSON。
# 同一个 message_id 可能出现两次（未读预览 + 打开后的完整描述），读取时后写覆盖前写。
# ========================================================================

import hashlib
import json
import os
from pathlib import Path
from typing import List, Optional

from utils.atomic_io import atomic_append_line

PROJECT_DIR = Path(__file__).resolve().parent.parent


class ChatStore:
    def __init__(self, root_name: str):
        self.root = PROJECT_DIR / "data" / "test" / root_name

    def _path(self, conversation_id: str) -> str:
        digest = hashlib.sha1(str(conversation_id or "").encode("utf-8")).hexdigest()[:20]
        return str(self.root / f"{digest}.jsonl")

    @staticmethod
    def _normalize(record: dict) -> dict:
        record = dict(record or {})
        try:
            ts = float(record.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 1e12:
            ts /= 1000.0
        record["ts"] = ts
        record["conversation_id"] = str(record.get("conversation_id") or "")
        record["message_id"] = str(record.get("message_id") or "")
        record["sender"] = str(record.get("sender") or "")
        record["text"] = str(record.get("text") or "")
        record["line"] = str(record.get("line") or "")
        record["media_text"] = str(record.get("media_text") or "")
        return record

    def append(self, record: dict) -> bool:
        record = self._normalize(record)
        if not record["conversation_id"] or not record["line"]:
            return False
        path = self._path(record["conversation_id"])
        try:
            os.makedirs(self.root, exist_ok=True)
            return atomic_append_line(path, json.dumps(record, ensure_ascii=False))
        except Exception:
            return False

    def load(self, conversation_id: str) -> List[dict]:
        path = self._path(conversation_id)
        if not os.path.exists(path):
            return []
        by_id = {}
        anonymous = []
        first_seq = {}
        position = 0
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    position += 1
                    record = self._normalize(record)
                    # _seq 取首次出现的位置：同一 message_id 的完整描述后写覆盖预览，
                    # 但排序位置仍停在原消息处，避免同一秒内的历史被打乱。
                    if record["message_id"]:
                        seq = first_seq.get(record["message_id"])
                        if seq is None:
                            seq = position
                            first_seq[record["message_id"]] = seq
                        record["_seq"] = seq
                        by_id[record["message_id"]] = record
                    else:
                        record["_seq"] = position
                        anonymous.append(record)
        except OSError:
            return []
        records = list(by_id.values()) + anonymous
        records.sort(key=self.cursor)
        return records

    @staticmethod
    def cursor(record: dict):
        """稳定排序游标：同一秒的消息用首次落盘顺序区分，翻页不会漏读。"""
        try:
            ts = float(record.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        try:
            seq = int(record.get("_seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        return (ts, seq)

    def tail(self, conversation_id: str, limit: int = 6) -> List[dict]:
        records = self.load(conversation_id)
        return records[-max(1, int(limit)):]

    def earlier(self, conversation_id: str, before_ts: float, limit: int = 6,
                before_seq: Optional[int] = None) -> List[dict]:
        """取游标之前的记录；before_seq 为空时表示从该秒的最后一条开始。"""
        records = self.load(conversation_id)
        try:
            before_ts = float(before_ts)
        except (TypeError, ValueError):
            before_ts = 0.0
        if before_seq is None:
            before = (before_ts, float("inf"))
        else:
            try:
                seq = int(before_seq)
            except (TypeError, ValueError):
                seq = 0
            before = (before_ts, seq)
        older = [r for r in records if self.cursor(r) < before]
        return older[-max(1, int(limit)):]

    def latest_ts(self, conversation_id: str) -> float:
        records = self.load(conversation_id)
        return float(records[-1].get("ts") or 0) if records else 0.0

    def earliest_ts(self, conversation_id: str) -> float:
        records = self.load(conversation_id)
        return float(records[0].get("ts") or 0) if records else 0.0

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class ParkedTopicStore:
    DEFAULT_FOLLOWUP_HOURS = (3, 24, 72)
    DEFAULT_MAX_ATTEMPTS = 3

    def __init__(self, workspace_dir: Path):
        self.path = workspace_dir / "parked_topics.json"

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"topics": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"topics": []}
        if not isinstance(data, dict):
            return {"topics": []}
        topics = data.get("topics", [])
        if not isinstance(topics, list):
            topics = []
        return {"topics": topics}

    def _save(self, payload: dict[str, Any]):
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def list_topics(self) -> list[dict[str, Any]]:
        data = self._load()
        topics = data.get("topics", [])
        topics.sort(key=lambda item: int(item.get("slot_id", 0)))
        return topics

    def next_slot_id(self) -> int:
        topics = self.list_topics()
        used = {int(item.get("slot_id", 0)) for item in topics if int(item.get("slot_id", 0)) > 0}
        slot_id = 1
        while slot_id in used:
            slot_id += 1
        return slot_id

    def get_topic(self, slot_id: int) -> dict[str, Any] | None:
        for topic in self.list_topics():
            if int(topic.get("slot_id", 0)) == int(slot_id):
                return topic
        return None

    def create_topic(
        self,
        *,
        title: str,
        summary_short: str,
        summary_long: str,
        recent_context: str,
        last_user_text: str,
        last_assistant_text: str,
        last_exchange_text: str,
        source_session: str,
        title_user_override: str | None = None,
    ) -> dict[str, Any]:
        data = self._load()
        now = datetime.now()
        slot_id = self.next_slot_id()
        topic = {
            "slot_id": slot_id,
            "title": (title_user_override or title or f"Parked Topic {slot_id}").strip(),
            "summary_short": (summary_short or "").strip(),
            "summary_long": (summary_long or "").strip(),
            "recent_context": (recent_context or "").strip(),
            "last_user_text": (last_user_text or "").strip(),
            "last_assistant_text": (last_assistant_text or "").strip(),
            "last_exchange_text": (last_exchange_text or "").strip(),
            "source_session": (source_session or "").strip(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "loaded_at": None,
            "followup": {
                "enabled": True,
                "status": "scheduled",
                "attempts": 0,
                "max_attempts": self.DEFAULT_MAX_ATTEMPTS,
                "schedule_hours": list(self.DEFAULT_FOLLOWUP_HOURS),
                "next_at": (now + timedelta(hours=self.DEFAULT_FOLLOWUP_HOURS[0])).isoformat(),
                "last_sent_at": None,
            },
        }
        topics = list(data.get("topics", []))
        topics.append(topic)
        data["topics"] = topics
        self._save(data)
        return topic

    def delete_topic(self, slot_id: int) -> dict[str, Any] | None:
        data = self._load()
        kept: list[dict[str, Any]] = []
        removed = None
        for topic in data.get("topics", []):
            if int(topic.get("slot_id", 0)) == int(slot_id):
                removed = topic
                continue
            kept.append(topic)
        if removed is None:
            return None
        data["topics"] = kept
        self._save(data)
        return removed

    def mark_loaded(self, slot_id: int) -> dict[str, Any] | None:
        data = self._load()
        now = datetime.now().isoformat()
        updated = None
        for topic in data.get("topics", []):
            if int(topic.get("slot_id", 0)) != int(slot_id):
                continue
            topic["loaded_at"] = now
            topic["updated_at"] = now
            followup = topic.setdefault("followup", {})
            followup["enabled"] = False
            followup["status"] = "loaded"
            followup["next_at"] = None
            updated = topic
            break
        if updated is not None:
            self._save(data)
        return updated

    def due_topics(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now()
        due: list[dict[str, Any]] = []
        for topic in self.list_topics():
            followup = topic.get("followup") or {}
            if not followup.get("enabled", False):
                continue
            next_at_raw = followup.get("next_at")
            if not next_at_raw:
                continue
            try:
                next_at = datetime.fromisoformat(str(next_at_raw))
            except Exception:
                continue
            if next_at <= now:
                due.append(topic)
        return due

    def record_followup_sent(self, slot_id: int, sent_at: datetime | None = None) -> dict[str, Any] | None:
        data = self._load()
        sent_at = sent_at or datetime.now()
        updated = None
        for topic in data.get("topics", []):
            if int(topic.get("slot_id", 0)) != int(slot_id):
                continue
            followup = topic.setdefault("followup", {})
            attempts = int(followup.get("attempts", 0)) + 1
            max_attempts = int(followup.get("max_attempts", self.DEFAULT_MAX_ATTEMPTS))
            schedule_hours = followup.get("schedule_hours") or list(self.DEFAULT_FOLLOWUP_HOURS)
            followup["attempts"] = attempts
            followup["last_sent_at"] = sent_at.isoformat()
            topic["updated_at"] = sent_at.isoformat()
            if attempts >= max_attempts or attempts >= len(schedule_hours):
                followup["enabled"] = False
                followup["status"] = "completed"
                followup["next_at"] = None
            else:
                next_hours = int(schedule_hours[attempts])
                created_at = datetime.fromisoformat(topic["created_at"])
                followup["status"] = "scheduled"
                followup["next_at"] = (created_at + timedelta(hours=next_hours)).isoformat()
            updated = topic
            break
        if updated is not None:
            self._save(data)
        return updated

"""Persistent timer manager for delayed Home Assistant actions."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from ha_client import call_ha_service

TIMER_STORE_PATH = Path(os.getenv("TIMER_STORE_PATH", "/tmp/gemini-live-proxy-timers.json"))
DEFAULT_MEDIA_PLAYER = os.getenv("TIMER_MEDIA_PLAYER_ENTITY_ID", "").strip()
DEFAULT_MEDIA_URL = os.getenv("TIMER_DEFAULT_MEDIA_URL", "").strip()
DEFAULT_SCRIPT_ID = os.getenv("TIMER_DEFAULT_SCRIPT_ID", "").strip()
DEFAULT_MEDIA_CONTENT_TYPE = os.getenv("TIMER_DEFAULT_MEDIA_CONTENT_TYPE", "music").strip() or "music"


class TimerManager:
    """Manage multiple persisted timers and execute HA actions when they finish."""

    def __init__(self, store_path: Path = TIMER_STORE_PATH):
        self.store_path = store_path
        self.timers: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        await self._load()
        now = time.time()
        expired = []
        async with self.lock:
            for timer_id, timer_data in list(self.timers.items()):
                if float(timer_data["ends_at"]) <= now:
                    expired.append((timer_id, timer_data))
                else:
                    self._schedule_locked(timer_id, timer_data)
        for timer_id, timer_data in expired:
            asyncio.create_task(self._finish_timer(timer_id, timer_data))

    async def set_timer(
        self,
        seconds: float,
        label: str = "",
        action: str = "notify",
        media_player_entity_id: str = "",
        media_url: str = "",
        media_content_type: str = "",
        script_id: str = "",
    ) -> dict:
        seconds = max(1, int(seconds))
        action = (action or "notify").strip()
        if action not in ("notify", "play_media", "run_script"):
            return {"status": "error", "message": f"Unsupported timer action: {action}"}

        label = (label or "timer").strip()
        media_player_entity_id = (media_player_entity_id or DEFAULT_MEDIA_PLAYER).strip()
        media_url = (media_url or DEFAULT_MEDIA_URL).strip()
        media_content_type = (media_content_type or DEFAULT_MEDIA_CONTENT_TYPE).strip()
        script_id = (script_id or DEFAULT_SCRIPT_ID).strip()

        if action == "play_media" and (not media_player_entity_id or not media_url):
            return {
                "status": "error",
                "message": "Timer play_media requires timer_media_player_entity_id and timer_default_media_url, or explicit media_player_entity_id and media_url.",
            }
        if action == "run_script" and not script_id:
            return {
                "status": "error",
                "message": "Timer run_script requires timer_default_script_id or explicit script_id.",
            }

        timer_id = uuid.uuid4().hex[:8]
        now = time.time()
        timer_data = {
            "id": timer_id,
            "label": label,
            "created_at": now,
            "ends_at": now + seconds,
            "seconds": seconds,
            "action": action,
            "media_player_entity_id": media_player_entity_id,
            "media_url": media_url,
            "media_content_type": media_content_type,
            "script_id": script_id,
        }

        async with self.lock:
            self.timers[timer_id] = timer_data
            self._schedule_locked(timer_id, timer_data)
            await self._save_locked()

        print(f"  [timer] START id={timer_id} label='{label}' seconds={seconds} action={action}", flush=True)
        return {
            "status": "ok",
            "timer": self._public_timer(timer_data),
            "message": f"Timer {label} set for {seconds} seconds",
        }

    async def list_timers(self) -> dict:
        async with self.lock:
            timers = [self._public_timer(timer_data) for timer_data in self.timers.values()]
        timers.sort(key=lambda item: item["remaining_seconds"])
        return {"status": "ok", "timers": timers, "count": len(timers)}

    async def cancel_timer(self, timer_id: str = "", label: str = "", cancel_all: bool = False) -> dict:
        timer_id = (timer_id or "").strip()
        label = (label or "").strip().lower()
        cancelled = []

        async with self.lock:
            if cancel_all:
                ids = list(self.timers.keys())
            elif timer_id:
                ids = [timer_id] if timer_id in self.timers else []
            elif label:
                ids = [
                    tid for tid, timer_data in self.timers.items()
                    if timer_data.get("label", "").lower() == label
                ]
            else:
                return {"status": "error", "message": "Provide timer_id, label, or cancel_all=true."}

            for tid in ids:
                timer_data = self.timers.pop(tid, None)
                if not timer_data:
                    continue
                task = self.tasks.pop(tid, None)
                if task:
                    task.cancel()
                cancelled.append(self._public_timer(timer_data))

            await self._save_locked()

        print(f"  [timer] CANCELLED {len(cancelled)} timer(s)", flush=True)
        return {"status": "ok", "cancelled": cancelled, "count": len(cancelled)}

    def _schedule_locked(self, timer_id: str, timer_data: dict) -> None:
        old_task = self.tasks.pop(timer_id, None)
        if old_task:
            old_task.cancel()
        self.tasks[timer_id] = asyncio.create_task(self._run_timer(timer_id, timer_data))

    async def _run_timer(self, timer_id: str, timer_data: dict) -> None:
        try:
            await asyncio.sleep(max(0, float(timer_data["ends_at"]) - time.time()))
            await self._finish_timer(timer_id, timer_data)
        except asyncio.CancelledError:
            pass

    async def _finish_timer(self, timer_id: str, timer_data: dict) -> None:
        async with self.lock:
            current = self.timers.pop(timer_id, None)
            self.tasks.pop(timer_id, None)
            await self._save_locked()
        if current is None:
            return

        print(f"  [timer] DONE id={timer_id} label='{timer_data.get('label')}'", flush=True)
        result = await self._execute_action(timer_data)
        print(f"  [timer] ACTION id={timer_id} result={result}", flush=True)

    async def _execute_action(self, timer_data: dict) -> dict:
        action = timer_data.get("action", "notify")
        label = timer_data.get("label", "timer")
        timer_id = timer_data.get("id", "")

        if action == "play_media":
            return await call_ha_service("media_player", "play_media", {
                "entity_id": timer_data["media_player_entity_id"],
                "media_content_id": timer_data["media_url"],
                "media_content_type": timer_data.get("media_content_type") or DEFAULT_MEDIA_CONTENT_TYPE,
            })

        if action == "run_script":
            return await call_ha_service("script", "turn_on", {
                "entity_id": timer_data["script_id"],
                "variables": {
                    "timer_id": timer_id,
                    "timer_label": label,
                    "timer_action": action,
                },
            })

        return await call_ha_service("persistent_notification", "create", {
            "title": "Gemini timer finished",
            "message": f"Timer '{label}' finished.",
            "notification_id": f"gemini_timer_{timer_id}",
        })

    async def _load(self) -> None:
        if not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text())
        except Exception as err:
            print(f"  [timer] Could not load {self.store_path}: {err}", flush=True)
            return
        timers = data.get("timers", [])
        self.timers = {
            str(timer_data["id"]): timer_data
            for timer_data in timers
            if "id" in timer_data and "ends_at" in timer_data
        }
        print(f"  [timer] Loaded {len(self.timers)} persisted timer(s)", flush=True)

    async def _save_locked(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timers": list(self.timers.values())}
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.store_path)

    def _public_timer(self, timer_data: dict) -> dict:
        remaining = max(0, int(round(float(timer_data["ends_at"]) - time.time())))
        return {
            "id": timer_data["id"],
            "label": timer_data.get("label", "timer"),
            "remaining_seconds": remaining,
            "action": timer_data.get("action", "notify"),
            "media_player_entity_id": timer_data.get("media_player_entity_id", ""),
            "script_id": timer_data.get("script_id", ""),
        }

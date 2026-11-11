"""Persistent timer manager for delayed Home Assistant actions."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from ha_client import call_ha_service

TimerAction = Callable[[str, str, dict], Awaitable[dict]]

TIMER_STORE_PATH = Path(os.getenv("TIMER_STORE_PATH", "/data/timers.json"))
DEFAULT_MEDIA_PLAYER = os.getenv("TIMER_MEDIA_PLAYER_ENTITY_ID", "").strip()
DEFAULT_MEDIA_URL = os.getenv("TIMER_DEFAULT_MEDIA_URL", "").strip()
DEFAULT_SCRIPT_ID = os.getenv("TIMER_DEFAULT_SCRIPT_ID", "").strip()
DEFAULT_MEDIA_CONTENT_TYPE = os.getenv("TIMER_DEFAULT_MEDIA_CONTENT_TYPE", "music").strip() or "music"
ALARM_REPEAT_INTERVAL_SECONDS = float(os.getenv("TIMER_ALARM_REPEAT_INTERVAL_SECONDS", "3.0"))
# A speaker that rings its own alarms keeps a long-poll open on /device/alarm.
# One that has not polled for this long is treated as gone.
DEVICE_SEEN_TTL_SECONDS = float(os.getenv("TIMER_DEVICE_SEEN_TTL_SECONDS", "90"))
DEVICE_RING_MAX_SECONDS = float(os.getenv("TIMER_DEVICE_RING_MAX_SECONDS", "600"))
DEVICE_RING_GUARD_INTERVAL_SECONDS = 10.0


class TimerManager:
    """Manage multiple persisted timers and execute HA actions when they finish."""

    def __init__(self, store_path: Path = TIMER_STORE_PATH):
        self.store_path = store_path
        self.timers: dict[str, dict] = {}
        self.ringing: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.ringing_tasks: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()
        # Speakers that ring their own alarms: address -> monotonic time of last poll.
        self.devices: dict[str, float] = {}
        self.alarm_changed = asyncio.Condition()

    def device_seen(self, speaker: str) -> None:
        self.devices[speaker] = time.monotonic()

    def device_can_ring(self, speaker: str) -> bool:
        seen = self.devices.get(speaker)
        return seen is not None and time.monotonic() - seen <= DEVICE_SEEN_TTL_SECONDS

    def device_ringing(self, speaker: str) -> bool:
        return any(t.get("ring_on") == speaker for t in self.ringing.values())

    async def wait_device_alarm(self, speaker: str, known: bool, timeout: float) -> bool:
        """Long-poll for a speaker: return its ringing state once it differs from `known`."""
        self.device_seen(speaker)
        deadline = time.monotonic() + timeout
        async with self.alarm_changed:
            while self.device_ringing(speaker) == known:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self.alarm_changed.wait(), remaining)
                except asyncio.TimeoutError:
                    break
        self.device_seen(speaker)
        return self.device_ringing(speaker)

    async def stop_device_alarm(self, speaker: str) -> dict:
        """Stop the alarms ringing on one speaker (its button was pressed)."""
        stopped = []
        async with self.lock:
            for tid in [t for t, d in self.ringing.items() if d.get("ring_on") == speaker]:
                stopped.append(self._public_timer(self._pop_ringing_locked(tid), ringing=True))
            await self._save_locked()
        await self._notify_alarm_changed()
        print(f"  [timer] STOPPED {len(stopped)} alarm(s) on speaker {speaker}", flush=True)
        return {"status": "ok", "stopped": stopped, "count": len(stopped)}

    async def _notify_alarm_changed(self) -> None:
        async with self.alarm_changed:
            self.alarm_changed.notify_all()

    def _pop_ringing_locked(self, timer_id: str) -> dict | None:
        timer_data = self.ringing.pop(timer_id, None)
        task = self.ringing_tasks.pop(timer_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        return timer_data

    async def _stop_media_player_alarm(self, stopped_data: list[dict]) -> None:
        """Silence the HA media player, unless every stopped alarm rang on its own speaker."""
        if DEFAULT_MEDIA_PLAYER and any(not d.get("ring_on") for d in stopped_data):
            await call_ha_service("media_player", "media_stop", {"entity_id": DEFAULT_MEDIA_PLAYER})

    async def start(self) -> None:
        """Load persisted timers and schedule future ones."""
        await self._load()
        now = time.time()
        expired = []
        async with self.lock:
            for timer_id, timer_data in list(self.timers.items()):
                if float(timer_data["ends_at"]) <= now:
                    expired.append((timer_id, timer_data))
                else:
                    self._schedule_locked(timer_id, timer_data)
            for timer_id, timer_data in list(self.ringing.items()):
                if timer_data.get("ring_on"):
                    # Give the speaker time to poll again after an add-on restart.
                    self.device_seen(timer_data["ring_on"])
                self._schedule_ringing_locked(timer_id, timer_data)
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
        speaker: str = "",
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
            # Address of the speaker the timer was set on; it rings there if it can.
            "speaker": speaker,
        }

        async with self.lock:
            self.timers[timer_id] = timer_data
            self._schedule_locked(timer_id, timer_data)
            await self._save_locked()

        print(f"  [timer] START id={timer_id} label='{label}' seconds={seconds} action={action} "
              f"speaker={speaker or '-'}", flush=True)
        return {
            "status": "ok",
            "timer": self._public_timer(timer_data),
            "message": f"Timer {label} set for {seconds} seconds",
        }

    async def list_timers(self) -> dict:
        async with self.lock:
            timers = [self._public_timer(timer_data) for timer_data in self.timers.values()]
            ringing = [self._public_timer(timer_data, ringing=True) for timer_data in self.ringing.values()]
        timers.sort(key=lambda item: item["remaining_seconds"])
        ringing.sort(key=lambda item: item["label"])
        return {
            "status": "ok",
            "timers": timers,
            "ringing": ringing,
            "count": len(timers),
            "ringing_count": len(ringing),
        }

    async def cancel_timer(self, timer_id: str = "", label: str = "", cancel_all: bool = False) -> dict:
        timer_id = (timer_id or "").strip()
        label = (label or "").strip().lower()
        cancelled = []

        async with self.lock:
            if cancel_all:
                ids = list(self.timers.keys())
                ringing_ids = list(self.ringing.keys())
            elif timer_id:
                ids = [timer_id] if timer_id in self.timers else []
                ringing_ids = [timer_id] if timer_id in self.ringing else []
            elif label:
                ids = [
                    tid for tid, timer_data in self.timers.items()
                    if timer_data.get("label", "").lower() == label
                ]
                ringing_ids = [
                    tid for tid, timer_data in self.ringing.items()
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

            stopped = []
            stopped_data = []
            for tid in ringing_ids:
                timer_data = self._pop_ringing_locked(tid)
                if not timer_data:
                    continue
                stopped_data.append(timer_data)
                stopped.append(self._public_timer(timer_data, ringing=True))

            await self._save_locked()

        await self._notify_alarm_changed()
        await self._stop_media_player_alarm(stopped_data)

        print(f"  [timer] CANCELLED {len(cancelled)} timer(s), stopped {len(stopped)} alarm(s)", flush=True)
        return {"status": "ok", "cancelled": cancelled, "stopped": stopped, "count": len(cancelled), "stopped_count": len(stopped)}

    async def stop_alarm(self, timer_id: str = "", label: str = "", stop_all: bool = False) -> dict:
        """Stop ringing timer alarms without cancelling future timers."""
        timer_id = (timer_id or "").strip()
        label = (label or "").strip().lower()
        stopped = []

        async with self.lock:
            if stop_all or (not timer_id and not label):
                ids = list(self.ringing.keys())
            elif timer_id:
                ids = [timer_id] if timer_id in self.ringing else []
            else:
                ids = [
                    tid for tid, timer_data in self.ringing.items()
                    if timer_data.get("label", "").lower() == label
                ]

            stopped_data = []
            for tid in ids:
                timer_data = self._pop_ringing_locked(tid)
                if not timer_data:
                    continue
                stopped_data.append(timer_data)
                stopped.append(self._public_timer(timer_data, ringing=True))
            await self._save_locked()

        await self._notify_alarm_changed()
        await self._stop_media_player_alarm(stopped_data)
        print(f"  [timer] STOPPED {len(stopped)} alarm(s)", flush=True)
        return {"status": "ok", "stopped": stopped, "count": len(stopped)}

    def _schedule_locked(self, timer_id: str, timer_data: dict) -> None:
        old_task = self.tasks.pop(timer_id, None)
        if old_task:
            old_task.cancel()
        self.tasks[timer_id] = asyncio.create_task(self._run_timer(timer_id, timer_data))

    def _schedule_ringing_locked(self, timer_id: str, timer_data: dict) -> None:
        old_task = self.ringing_tasks.pop(timer_id, None)
        if old_task:
            old_task.cancel()
        ring = self._guard_device_alarm if timer_data.get("ring_on") else self._ring_alarm
        self.ringing_tasks[timer_id] = asyncio.create_task(ring(timer_id, timer_data))

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

    async def _ring_alarm(self, timer_id: str, timer_data: dict) -> None:
        """Repeat default timer alarm media until the alarm is stopped."""
        if not DEFAULT_MEDIA_PLAYER or not DEFAULT_MEDIA_URL:
            return
        try:
            while True:
                async with self.lock:
                    if timer_id not in self.ringing:
                        return
                await call_ha_service("media_player", "play_media", {
                    "entity_id": DEFAULT_MEDIA_PLAYER,
                    "media_content_id": DEFAULT_MEDIA_URL,
                    "media_content_type": DEFAULT_MEDIA_CONTENT_TYPE,
                })
                await asyncio.sleep(ALARM_REPEAT_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            pass

    async def _guard_device_alarm(self, timer_id: str, timer_data: dict) -> None:
        """Watch an alarm ringing on its own speaker.

        The speaker plays the sound itself, so nothing here makes noise. Stop the
        alarm after DEVICE_RING_MAX_SECONDS, and hand it to the HA media player
        if the speaker stops polling (unplugged, off Wi-Fi) while it rings.
        """
        speaker = timer_data.get("ring_on", "")
        try:
            while True:
                await asyncio.sleep(DEVICE_RING_GUARD_INTERVAL_SECONDS)
                since = float(timer_data.get("ringing_since", time.time()))
                expired = time.time() - since >= DEVICE_RING_MAX_SECONDS
                if not expired and self.device_can_ring(speaker):
                    continue
                async with self.lock:
                    if timer_id not in self.ringing:
                        return
                    if expired:
                        self._pop_ringing_locked(timer_id)
                        print(f"  [timer] Alarm id={timer_id} unanswered for "
                              f"{DEVICE_RING_MAX_SECONDS:.0f}s on {speaker}, stopped", flush=True)
                    else:
                        timer_data.pop("ring_on", None)
                        self.ringing_tasks.pop(timer_id, None)
                        self._schedule_ringing_locked(timer_id, timer_data)
                        print(f"  [timer] Speaker {speaker} gone, alarm id={timer_id} "
                              f"moved to the media player", flush=True)
                    await self._save_locked()
                await self._notify_alarm_changed()
                return
        except asyncio.CancelledError:
            pass

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

        notification = await call_ha_service("persistent_notification", "create", {
            "title": "Gemini timer finished",
            "message": f"Timer '{label}' finished.",
            "notification_id": f"gemini_timer_{timer_id}",
        })
        speaker = timer_data.get("speaker", "")
        ring_on_speaker = bool(speaker) and self.device_can_ring(speaker)
        if ring_on_speaker or (DEFAULT_MEDIA_PLAYER and DEFAULT_MEDIA_URL):
            timer_data["ringing_since"] = time.time()
            if ring_on_speaker:
                timer_data["ring_on"] = speaker
            async with self.lock:
                self.ringing[timer_id] = timer_data
                self._schedule_ringing_locked(timer_id, timer_data)
                await self._save_locked()
            await self._notify_alarm_changed()
            return {"status": notification.get("status", "ok"), "notification": notification, "ringing": self._public_timer(timer_data, ringing=True)}
        return notification

    async def _load(self) -> None:
        if not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text())
        except Exception as err:
            print(f"  [timer] Could not load {self.store_path}: {err}", flush=True)
            return
        timers = data.get("timers", [])
        ringing = data.get("ringing", [])
        self.timers = {
            str(timer_data["id"]): timer_data
            for timer_data in timers
            if "id" in timer_data and "ends_at" in timer_data
        }
        self.ringing = {
            str(timer_data["id"]): timer_data
            for timer_data in ringing
            if "id" in timer_data
        }
        print(f"  [timer] Loaded {len(self.timers)} persisted timer(s), {len(self.ringing)} ringing alarm(s)", flush=True)

    async def _save_locked(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timers": list(self.timers.values()), "ringing": list(self.ringing.values())}
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.store_path)

    def _public_timer(self, timer_data: dict, ringing: bool = False) -> dict:
        remaining = max(0, int(round(float(timer_data["ends_at"]) - time.time())))
        return {
            "id": timer_data["id"],
            "label": timer_data.get("label", "timer"),
            "remaining_seconds": remaining,
            "action": timer_data.get("action", "notify"),
            "state": "ringing" if ringing else "active",
            "ring_on": timer_data.get("ring_on", ""),
            "media_player_entity_id": timer_data.get("media_player_entity_id", ""),
            "script_id": timer_data.get("script_id", ""),
        }

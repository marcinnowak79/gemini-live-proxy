"""Gemini Live session manager — handles audio streaming, function calls, search."""

import asyncio
import json
import os
import time
from typing import AsyncGenerator, Callable, Awaitable

from google import genai
from google.genai import types

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Aoede")
ASSISTANT_LANGUAGE = os.getenv("ASSISTANT_LANGUAGE", "en-US")
ASSISTANT_RESPONSE_LANGUAGE = os.getenv("ASSISTANT_RESPONSE_LANGUAGE", "English")
RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION = float(os.getenv("RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION", "1.5"))
RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO = float(os.getenv("RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO", "1.2"))
RECEIVE_IDLE_TIMEOUT_GENERAL = float(os.getenv("RECEIVE_IDLE_TIMEOUT_GENERAL", "8.0"))

DEFAULT_SYSTEM_PROMPT_TEMPLATE = """You are a smart home assistant. Always speak in {response_language}. You must answer only in {response_language}.

Rules:
- Answer very briefly, preferably in one sentence.
- Always use tools for smart home control. Never say "done" without calling the appropriate tool.
- If the user does not name a room or location, prefer devices marked local=true.
- If several devices have similar names, choose the local=true device unless the user explicitly names another room/person/location.
- Only choose non-local devices when the user explicitly refers to their room, person, or unique device name.
- When the user asks for room lights, use control_room.
- When the user asks for a specific device, use control_device.
- Timers: for countdown requests, call set_timer. Use list_timers to answer timer status questions. Use cancel_timer to cancel timers.
- For requests like "play music after X minutes", call set_timer with action=play_media.
- For requests like "run a scene/script after X minutes", call set_timer with action=run_script when a script is available.
- Climate: for heating, cooling, AC or temperature changes, call set_climate.
- Time and date: use the current context below. Do not call search_web for time/date.
- Questions about current information, weather or news: call search_web, then answer with the result.
- activate_scene only when the user explicitly asks for a scene by name.

Note: many smart home lights may be exposed as switch entities rather than light entities.

=== AVAILABLE DEVICES ===
{entities}
{context}
"""

SYSTEM_PROMPT_TEMPLATE = os.getenv("SYSTEM_PROMPT_TEMPLATE", DEFAULT_SYSTEM_PROMPT_TEMPLATE)


def build_tools(room_keys: list[str], vacuum_enabled: bool = False) -> list:
    declarations = [
        types.FunctionDeclaration(
            name="control_device",
            description="Turn on/off/toggle a single HA entity.",
            parameters={"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "action": {"type": "string", "enum": ["turn_on", "turn_off", "toggle"]},
            }, "required": ["entity_id", "action"]},
        ),
        types.FunctionDeclaration(
            name="control_room",
            description="Turn on/off ALL lights in a room at once.",
            parameters={"type": "object", "properties": {
                "room": {"type": "string", "enum": room_keys if room_keys else ["default"]},
                "action": {"type": "string", "enum": ["turn_on", "turn_off"]},
            }, "required": ["room", "action"]},
        ),
        types.FunctionDeclaration(
            name="activate_scene",
            description="Activate a scene. Only when user explicitly asks by name.",
            parameters={"type": "object", "properties": {
                "scene_id": {"type": "string"},
            }, "required": ["scene_id"]},
        ),
        types.FunctionDeclaration(
            name="run_script",
            description="Run a HA script",
            parameters={"type": "object", "properties": {
                "script_id": {"type": "string"},
            }, "required": ["script_id"]},
        ),
        types.FunctionDeclaration(
            name="set_timer",
            description=(
                "Set countdown timer. Convert to seconds: 1 minuta=60, 30 sekund=30. "
                "Use action=notify for a normal timer, action=play_media to play configured music/media after the timer, "
                "or action=run_script to run a configured Home Assistant script after the timer."
            ),
            parameters={"type": "object", "properties": {
                "seconds": {"type": "number"},
                "label": {"type": "string"},
                "action": {"type": "string", "enum": ["notify", "play_media", "run_script"]},
                "media_player_entity_id": {"type": "string"},
                "media_url": {"type": "string"},
                "media_content_type": {"type": "string"},
                "script_id": {"type": "string"},
            }, "required": ["seconds"]},
        ),
        types.FunctionDeclaration(
            name="list_timers",
            description="List all active timers and their remaining time. Use for questions like 'how much time is left' or 'what timers are active'.",
            parameters={"type": "object", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="cancel_timer",
            description="Cancel active timer by id, exact label, or all timers. Use for requests like 'cancel timer', 'cancel music timer', or 'cancel all timers'.",
            parameters={"type": "object", "properties": {
                "timer_id": {"type": "string"},
                "label": {"type": "string"},
                "cancel_all": {"type": "boolean"},
            }},
        ),
        types.FunctionDeclaration(
            name="search_web",
            description="Search web for current info (weather, news). Use when user asks a question.",
            parameters={"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        ),
        types.FunctionDeclaration(
            name="set_climate",
            description="Set climate/AC temperature and mode.",
            parameters={"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "temperature": {"type": "number"},
                "hvac_mode": {"type": "string", "enum": ["off", "cool", "heat", "auto", "fan_only", "dry"]},
            }, "required": ["entity_id"]},
        ),
    ]
    if vacuum_enabled:
        declarations.append(types.FunctionDeclaration(
            name="control_vacuum",
            description=(
                "Control robot vacuum only when the user explicitly mentions the robot vacuum, "
                "odkurzacz, robot sprzatajacy, sprzatanie, or docking the vacuum. "
                "Never use this for lights, lamps, devices, or pronouns like it/her."
            ),
            parameters={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["start", "return_to_base"]},
            }, "required": ["action"]},
        ))
    return [types.Tool(function_declarations=declarations)]


class GeminiSession:
    """Manages a Gemini Live session with streaming audio."""

    def __init__(self, client: genai.Client, entity_list: str, room_lights: dict,
                 ha_context: str, history: list,
                 on_function_call: Callable,
                 voice: str | None = None,
                 on_responding: Callable | None = None,
                 vacuum_enabled: bool = False,
                 local_area_id: str = ""):
        self.client = client
        self.entity_list = entity_list
        self.room_lights = room_lights
        self.ha_context = ha_context
        self.history = history
        self.on_function_call = on_function_call
        self.voice = voice or GEMINI_VOICE
        self.on_responding = on_responding
        self.vacuum_enabled = vacuum_enabled
        self.local_area_id = local_area_id

    def _build_prompt(self) -> str:
        local_context = ""
        if self.local_area_id:
            local_context = (
                f"\nCurrent Voice PE area: {self.local_area_id}\n"
                "For commands without an explicit room/location, prefer devices marked local=true.\n"
            )
        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            entities=self.entity_list,
            context=f"{local_context}{self.ha_context}",
            response_language=ASSISTANT_RESPONSE_LANGUAGE,
        )
        if self.history:
            prompt += "\n=== OSTATNIA ROZMOWA ===\n"
            for h in self.history:
                role = "Użytkownik" if h["role"] == "user" else "Asystent"
                prompt += f"{role}: {h['text']}\n"
            prompt += "=== KONIEC ===\n"
        return prompt

    async def stream_audio(
        self,
        audio_chunks: AsyncGenerator[bytes, None],
        on_audio_out: Callable[[bytes], Awaitable[None]],
    ) -> str:
        """Stream audio to Gemini, stream response audio back via callback.

        Returns summary of what happened (for history).
        """
        room_keys = list(self.room_lights.keys())
        prompt = self._build_prompt()

        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice),
                ),
                language_code=ASSISTANT_LANGUAGE,
            ),
            system_instruction=types.Content(parts=[types.Part(text=prompt)]),
            tools=build_tools(room_keys, self.vacuum_enabled),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=500,
                ),
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            ),
        )

        function_calls_made = ""
        response_text = ""
        t0 = time.monotonic()

        async with self.client.aio.live.connect(model=GEMINI_MODEL, config=config) as session:
            response_audio_chunks = []
            send_done = False

            # Task 1: Send audio to Gemini (runs until source stops)
            async def send_audio():
                nonlocal send_done
                chunk_n = 0
                try:
                    async for chunk in audio_chunks:
                        chunk_n += 1
                        if chunk_n == 1:
                            print(f"  [gemini] Sending audio to Gemini...", flush=True)
                        await session.send_realtime_input(
                            audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"),
                        )
                    await session.send_realtime_input(audio_stream_end=True)
                    print(f"  [gemini] Audio stream ended, {chunk_n} chunks ({(time.monotonic()-t0)*1000:.0f}ms)", flush=True)
                except Exception as e:
                    print(f"  [gemini] SEND ERROR after {chunk_n} chunks: {e}", flush=True)
                finally:
                    send_done = True

            # Task 2: Receive responses from Gemini (runs until turn_complete)
            responding_signaled = False

            async def receive_response():
                nonlocal responding_signaled
                try:
                    messages = session.receive().__aiter__()
                    while True:
                        if function_calls_list and response_audio_chunks:
                            idle_timeout = RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO
                        elif function_calls_list:
                            idle_timeout = RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION
                        else:
                            idle_timeout = RECEIVE_IDLE_TIMEOUT_GENERAL
                        try:
                            message = await asyncio.wait_for(messages.__anext__(), timeout=idle_timeout)
                        except asyncio.TimeoutError:
                            print(
                                f"  [gemini] Receive idle timeout after {idle_timeout:.1f}s "
                                f"(functions={function_calls_list}, audio_chunks={len(response_audio_chunks)})",
                                flush=True,
                            )
                            break
                        except StopAsyncIteration:
                            break

                        sc = message.server_content
                        if sc:
                            if sc.model_turn:
                                # Signal that Gemini started responding (stop mic streaming)
                                if not responding_signaled:
                                    responding_signaled = True
                                    if self.on_responding:
                                        self.on_responding()
                                    print(f"  [gemini] Responding ({(time.monotonic()-t0)*1000:.0f}ms)", flush=True)
                                for part in sc.model_turn.parts:
                                    if part.inline_data:
                                        response_audio_chunks.append(part.inline_data.data)
                                        await on_audio_out(part.inline_data.data)
                                    elif part.text:
                                        response_text_parts.append(part.text)
                            if sc.turn_complete:
                                break

                        tc = message.tool_call
                        if tc:
                            if not responding_signaled:
                                responding_signaled = True
                                if self.on_responding:
                                    self.on_responding()
                                print(f"  [gemini] Tool call received, stopping mic ({(time.monotonic()-t0)*1000:.0f}ms)", flush=True)
                            responses = []
                            for fc in tc.function_calls:
                                args_dict = dict(fc.args)
                                print(f"  [gemini] FC: {fc.name}({fc.args})", flush=True)
                                function_calls_list.append(f"{fc.name}({args_dict})")
                                if fc.name == "search_web":
                                    result = await self._do_search(args_dict.get("query", ""))
                                else:
                                    result = await self.on_function_call(fc.name, args_dict)
                                responses.append(types.FunctionResponse(
                                    id=fc.id, name=fc.name, response=result,
                                ))
                            await session.send_tool_response(function_responses=responses)
                except Exception as e:
                    print(f"  [gemini] RECEIVE ERROR: {e}", flush=True)

            response_text_parts = []
            function_calls_list = []

            # Heartbeat — log if session is stuck waiting
            async def heartbeat():
                while True:
                    await asyncio.sleep(5)
                    print(f"  [gemini] ...still waiting ({(time.monotonic()-t0)*1000:.0f}ms, sent_done={send_done}, responding={responding_signaled})", flush=True)

            # Run send + receive, cancel heartbeat when done
            hb_task = asyncio.create_task(heartbeat())
            try:
                await asyncio.gather(send_audio(), receive_response())
            finally:
                hb_task.cancel()

            response_text = "".join(response_text_parts)
            function_calls_made = " ".join(function_calls_list)

        total_ms = (time.monotonic() - t0) * 1000
        print(f"  [gemini] TOTAL: {total_ms:.0f}ms", flush=True)

        if response_audio_chunks:
            total_audio = sum(len(c) for c in response_audio_chunks)
            print(f"  [gemini] Streamed {len(response_audio_chunks)} audio chunks, {total_audio}B ({total_audio/48000:.1f}s)", flush=True)

        return function_calls_made.strip() or response_text or ""

    async def _do_search(self, query: str) -> dict:
        """Search web using Gemini generate_content + Google Search."""
        print(f"  [search] {query}")
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"{query}. Answer in one sentence in {ASSISTANT_RESPONSE_LANGUAGE}.",
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            print(f"  [search] → {response.text[:100]}")
            return {"result": response.text}
        except Exception as err:
            print(f"  [search] ERROR: {err}")
            return {"error": str(err)}

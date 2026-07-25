"""Gemini Live session manager — handles audio streaming, function calls, search."""

import asyncio
import json
import os
import time
from typing import AsyncGenerator, Callable, Awaitable

from google import genai
from google.genai import types

from ai_common import (  # noqa: F401 - re-exported for callers and tests
    ASSISTANT_GENDER,
    ASSISTANT_LANGUAGE,
    ASSISTANT_NAME,
    ASSISTANT_RESPONSE_LANGUAGE,
    ASSISTANT_SPEAKING_STYLE,
    DEBUG_LOGGING,
    DEFAULT_SYSTEM_PROMPT_TEMPLATE,
    FOLLOW_UP_RESOLUTION_PROMPT,
    INPUT_LANGUAGE_LOCK_PROMPT,
    SYSTEM_PROMPT_TEMPLATE,
    build_persona_prompt,
    build_prompt,
    build_tool_specs,
    debug_log,
    web_search,
)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Charon")
RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION = float(os.getenv("RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION", "1.5"))
RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO = float(os.getenv("RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO", "1.2"))
RECEIVE_IDLE_TIMEOUT_GENERAL = float(os.getenv("RECEIVE_IDLE_TIMEOUT_GENERAL", "8.0"))
GEMINI_SILENCE_DURATION_MS = int(os.getenv("GEMINI_SILENCE_DURATION_MS", "1100"))



def build_tools(room_keys: list[str], vacuum_enabled: bool = False) -> list:
    """Adapt the shared tool catalogue to Gemini's FunctionDeclaration type."""
    declarations = [
        types.FunctionDeclaration(
            name=spec["name"],
            description=spec["description"],
            parameters=spec["parameters"],
        )
        for spec in build_tool_specs(room_keys, vacuum_enabled)
    ]
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
        return build_prompt(self.entity_list, self.ha_context, self.history,
                            self.local_area_id)

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
                # Manual activity detection — proxy runs its own VAD; Gemini's auto-VAD never
                # sees the turn end once we stop sending and waits forever. Disable it and send
                # explicit activity_start/activity_end so the turn ends deterministically.
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True),
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
                            debug_log("  [gemini] Sending audio to Gemini...")
                            await session.send_realtime_input(activity_start=types.ActivityStart())
                        await session.send_realtime_input(
                            audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"),
                        )
                    if chunk_n:
                        await session.send_realtime_input(activity_end=types.ActivityEnd())
                    else:
                        await session.send_realtime_input(audio_stream_end=True)
                    debug_log(f"  [gemini] Audio stream ended, {chunk_n} chunks ({(time.monotonic()-t0)*1000:.0f}ms)")
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
                            debug_log(
                                f"  [gemini] Receive idle timeout after {idle_timeout:.1f}s "
                                f"(functions={function_calls_list}, audio_chunks={len(response_audio_chunks)})"
                            )
                            break
                        except StopAsyncIteration:
                            break

                        msg_fields = [
                            n for n in (
                                "setup_complete", "server_content", "tool_call",
                                "tool_call_cancellation", "usage_metadata",
                                "go_away", "session_resumption_update",
                            ) if getattr(message, n, None) is not None
                        ]
                        debug_log(
                            f"  [gemini] msg fields={msg_fields} "
                            f"({(time.monotonic()-t0)*1000:.0f}ms, responding={responding_signaled})"
                        )

                        sc = message.server_content
                        if sc:
                            if sc.model_turn:
                                # Signal that Gemini started responding (stop mic streaming)
                                if not responding_signaled:
                                    responding_signaled = True
                                    if self.on_responding:
                                        self.on_responding()
                                    debug_log(f"  [gemini] Responding ({(time.monotonic()-t0)*1000:.0f}ms)")
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
                                debug_log(f"  [gemini] Tool call received, stopping mic ({(time.monotonic()-t0)*1000:.0f}ms)")
                            responses = []
                            for fc in tc.function_calls:
                                args_dict = dict(fc.args)
                                debug_log(f"  [gemini] FC: {fc.name}({fc.args})")
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
                    debug_log(f"  [gemini] ...still waiting ({(time.monotonic()-t0)*1000:.0f}ms, sent_done={send_done}, responding={responding_signaled})")

            # Run send + receive, cancel heartbeat when done
            hb_task = asyncio.create_task(heartbeat()) if DEBUG_LOGGING else None
            try:
                await asyncio.gather(send_audio(), receive_response())
            finally:
                if hb_task is not None:
                    hb_task.cancel()

            response_text = "".join(response_text_parts)
            function_calls_made = " ".join(function_calls_list)

        total_ms = (time.monotonic() - t0) * 1000
        debug_log(f"  [gemini] TOTAL: {total_ms:.0f}ms")

        if response_audio_chunks:
            total_audio = sum(len(c) for c in response_audio_chunks)
            debug_log(f"  [gemini] Streamed {len(response_audio_chunks)} audio chunks, {total_audio}B ({total_audio/48000:.1f}s)")

        return function_calls_made.strip() or response_text or ""

    async def _do_search(self, query: str) -> dict:
        """Search web using Gemini generate_content + Google Search."""
        return await web_search(query, self.client)

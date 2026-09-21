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

def _cfg(name: str, default: str) -> str:
    """bashio hands unset add-on options through as the literal string "null"."""
    value = os.getenv(name, "").strip()
    return default if not value or value.lower() == "null" else value


GEMINI_MODEL = _cfg("GEMINI_MODEL", "gemini-3.8-live")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Charon")
RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION = float(os.getenv("RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION", "1.5"))
RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO = float(os.getenv("RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO", "1.2"))
RECEIVE_IDLE_TIMEOUT_GENERAL = float(os.getenv("RECEIVE_IDLE_TIMEOUT_GENERAL", "8.0"))
# Tools are async: the result is spoken in a fresh turn ~0.5-1s after we send it.
RECEIVE_IDLE_TIMEOUT_AWAITING_REPLY = float(os.getenv("RECEIVE_IDLE_TIMEOUT_AWAITING_REPLY", "5.0"))
GEMINI_SILENCE_DURATION_MS = int(os.getenv("GEMINI_SILENCE_DURATION_MS", "3000"))
# Quiet confirmations: a successful plain device action is
# acknowledged with a short chime instead of a spoken sentence.
QUIET_CONFIRMATIONS = os.getenv("QUIET_CONFIRMATIONS", "false").lower() in ("1", "true", "yes", "on")
QUIET_TOOLS = frozenset({
    "control_device", "control_room", "activate_scene", "run_script",
    "set_climate", "control_vacuum", "cancel_timer", "stop_timer_alarm",
})

# Gemini 3.8 Live runs tools NON_BLOCKING: the model is free to speak while a call
# runs, so it must be told not to claim success before the result, and to cover
# slow lookups with a filler.
ASYNC_TOOLS_PROMPT = (
    "\n\nNarzędzia wykonują się w tle. Po wywołaniu narzędzia sterującego NIE potwierdzaj "
    "wykonania, zanim dostaniesz wynik — potwierdź dopiero na podstawie wyniku. "
    "Wyjątek: zanim wywołasz search_web albo inne narzędzie pobierające informacje z internetu "
    "lub zewnętrznego serwisu, powiedz najpierw jedno bardzo krótkie zdanie, np. „Już sprawdzam.”, "
    "a dopiero potem wywołaj narzędzie. Przy sterowaniu urządzeniami, scenami i timerami "
    "nic nie mów przed wywołaniem."
)


def make_confirm_chime(sample_rate: int = 24000) -> bytes:
    """Short soft two-note rising chime, PCM16 mono at Gemini's output rate."""
    import numpy as np
    notes = [(0.07, 880.0), (0.11, 1318.5)]
    parts = []
    for duration, freq in notes:
        t = np.arange(int(sample_rate * duration)) / sample_rate
        envelope = np.minimum(1.0, t / 0.008) * np.exp(-t * 18)
        parts.append(np.sin(2 * np.pi * freq * t) * envelope)
    wave = np.concatenate(parts + [np.zeros(int(sample_rate * 0.05))])
    return (wave * 7000).astype(np.int16).tobytes()


CONFIRM_CHIME = make_confirm_chime()


def is_quiet_success(name: str, result) -> bool:
    """A plain device action that clearly worked — nothing worth saying aloud."""
    if name not in QUIET_TOOLS or not isinstance(result, dict):
        return False
    if result.get("status") != "ok":
        return False
    # e.g. "cancel the timer" when none was running: let the model explain.
    return result.get("count", 1) != 0


def build_tools(room_keys: list[str], vacuum_enabled: bool = False) -> list:
    """Adapt the shared tool catalogue to Gemini's FunctionDeclaration type."""
    declarations = [
        types.FunctionDeclaration(
            name=spec["name"],
            description=spec["description"],
            parameters=spec["parameters"],
            behavior=types.Behavior.NON_BLOCKING,
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
        self.model = GEMINI_MODEL
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
        prompt = build_prompt(self.entity_list, self.ha_context, self.history,
                              self.local_area_id)
        return prompt + ASYNC_TOOLS_PROMPT

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
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                # Manual activity detection: the proxy already runs its own VAD and cuts the
                # mic stream. Gemini's own auto-VAD then never sees the turn end (the audio
                # just stops) and waits forever — only emitting session_resumption_update —
                # until the session times out. We disable auto-VAD and send explicit
                # activity_start/activity_end so the turn ends deterministically.
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True),
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            ),
        )

        function_calls_made = ""
        response_text = ""
        t0 = time.monotonic()

        debug_log(f"  [gemini] model={self.model}")
        async with self.client.aio.live.connect(model=self.model, config=config) as session:
            response_audio_chunks = []
            input_transcript_parts = []
            output_transcript_parts = []
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
                            # Manual VAD: mark start of user activity before the first chunk.
                            await session.send_realtime_input(activity_start=types.ActivityStart())
                        await session.send_realtime_input(
                            audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"),
                        )
                    # Manual VAD: explicitly end the turn so Gemini responds immediately
                    # (instead of waiting for its own VAD that never fires once we stop
                    # sending). This replaces audio_stream_end, which auto-VAD ignored.
                    if chunk_n:
                        await session.send_realtime_input(activity_end=types.ActivityEnd())
                    else:
                        await session.send_realtime_input(audio_stream_end=True)
                    debug_log(f"  [gemini] Audio stream ended, {chunk_n} chunks ({(time.monotonic()-t0)*1000:.0f}ms)")
                except Exception as e:
                    print(f"  [gemini] SEND ERROR after {chunk_n} chunks: {e}", flush=True)
                finally:
                    send_done = True

            # Task 2: Receive responses from Gemini (runs until the last turn completes)
            responding_signaled = False
            tool_tasks: set[asyncio.Task] = set()
            tools_running = 0
            # NON_BLOCKING tools: the model ends its turn right after the tool
            # call and speaks the result in a NEW turn once we send the response. Track
            # when the last response went out and when audio last arrived, so a turn end
            # only ends the session when no spoken follow-up is still owed.
            reply_owed = False
            last_response_at = 0.0
            last_audio_at = 0.0
            turn_open = False
            # Set by a finished tool run, so a quiet (chime-only) result can end the
            # session at once instead of waiting out an idle timeout.
            tools_done = asyncio.Event()

            async def run_tools(function_calls):
                nonlocal reply_owed, last_response_at, tools_running
                try:
                    results = []
                    for fc in function_calls:
                        args_dict = dict(fc.args)
                        try:
                            if fc.name == "search_web":
                                result = await self._do_search(args_dict.get("query", ""))
                            else:
                                result = await self.on_function_call(fc.name, args_dict)
                        except Exception as e:  # noqa: BLE001 - the model must still get an answer
                            print(f"  [gemini] TOOL ERROR {fc.name}: {e}", flush=True)
                            result = {"status": "error", "message": str(e)}
                        results.append((fc, result))
                    quiet = QUIET_CONFIRMATIONS and all(is_quiet_success(fc.name, r) for fc, r in results)
                    extra = {"scheduling": types.FunctionResponseScheduling.SILENT} if quiet else {}
                    await session.send_tool_response(function_responses=[
                        types.FunctionResponse(id=fc.id, name=fc.name, response=r, **extra)
                        for fc, r in results
                    ])
                    debug_log(f"  [gemini] Tool response sent quiet={quiet} "
                              f"({(time.monotonic()-t0)*1000:.0f}ms)")
                    if quiet:
                        response_audio_chunks.append(CONFIRM_CHIME)
                        await on_audio_out(CONFIRM_CHIME)
                    else:
                        reply_owed = True
                        last_response_at = time.monotonic()
                finally:
                    tools_running -= 1
                    tools_done.set()

            async def receive_response():
                nonlocal responding_signaled, reply_owed, last_audio_at, turn_open, tools_running
                next_msg: asyncio.Future | None = None
                try:
                    messages = session.receive().__aiter__()
                    while True:
                        if tools_running:
                            idle_timeout = RECEIVE_IDLE_TIMEOUT_GENERAL
                        elif reply_owed:
                            idle_timeout = RECEIVE_IDLE_TIMEOUT_AWAITING_REPLY
                        elif function_calls_list and response_audio_chunks:
                            idle_timeout = RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO
                        elif function_calls_list:
                            idle_timeout = RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION
                        else:
                            idle_timeout = RECEIVE_IDLE_TIMEOUT_GENERAL
                        if next_msg is None:
                            next_msg = asyncio.ensure_future(messages.__anext__())
                        tools_waiter = asyncio.ensure_future(tools_done.wait())
                        done, _ = await asyncio.wait(
                            {next_msg, tools_waiter}, timeout=idle_timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        tools_waiter.cancel()
                        if next_msg not in done:
                            if tools_waiter in done:
                                tools_done.clear()
                                # All tools finished, nothing left to say, model idle.
                                if not tools_running and not reply_owed and not turn_open:
                                    debug_log(f"  [gemini] Quiet tool result, ending "
                                              f"({(time.monotonic()-t0)*1000:.0f}ms)")
                                    break
                                continue
                            debug_log(
                                f"  [gemini] Receive idle timeout after {idle_timeout:.1f}s "
                                f"(functions={function_calls_list}, audio_chunks={len(response_audio_chunks)}, "
                                f"tools_running={tools_running}, reply_owed={reply_owed})"
                            )
                            break
                        try:
                            message = next_msg.result()
                        except StopAsyncIteration:
                            # The SDK's receive() iterator ends at every turn_complete.
                            next_msg = None
                            if tools_running or reply_owed:
                                messages = session.receive().__aiter__()
                                continue
                            break
                        next_msg = None

                        msg_fields = [
                            n for n in (
                                "setup_complete", "server_content", "tool_call",
                                "tool_call_cancellation", "usage_metadata",
                                "go_away", "session_resumption_update",
                            ) if getattr(message, n, None) is not None
                        ]
                        if message.server_content is not None:
                            msg_fields += [
                                n for n in ("generation_complete", "turn_complete", "interrupted")
                                if getattr(message.server_content, n, None)
                            ]
                        debug_log(
                            f"  [gemini] msg fields={msg_fields} "
                            f"({(time.monotonic()-t0)*1000:.0f}ms, responding={responding_signaled})"
                        )

                        sc = message.server_content
                        if sc:
                            if sc.input_transcription and sc.input_transcription.text:
                                input_transcript_parts.append(sc.input_transcription.text)
                            if sc.output_transcription and sc.output_transcription.text:
                                output_transcript_parts.append(sc.output_transcription.text)
                            if sc.model_turn:
                                turn_open = True
                                # Signal that Gemini started responding (stop mic streaming)
                                if not responding_signaled:
                                    responding_signaled = True
                                    if self.on_responding:
                                        self.on_responding()
                                    debug_log(f"  [gemini] Responding ({(time.monotonic()-t0)*1000:.0f}ms)")
                                for part in sc.model_turn.parts:
                                    if part.inline_data:
                                        response_audio_chunks.append(part.inline_data.data)
                                        last_audio_at = time.monotonic()
                                        await on_audio_out(part.inline_data.data)
                                    elif part.text:
                                        response_text_parts.append(part.text)
                            # 3.8 sends turn_complete only once the audio would have finished
                            # playing (seconds late); generation_complete means every chunk is
                            # already here, so the session can end on it.
                            turn_done = sc.turn_complete or sc.generation_complete
                            if turn_done:
                                turn_open = False
                                # Speech after the last tool response is the follow-up to it
                                # (a filler like "Już sprawdzam." comes before, so it doesn't count).
                                if reply_owed and last_audio_at > last_response_at:
                                    reply_owed = False
                                if tools_running or reply_owed:
                                    debug_log(
                                        f"  [gemini] Turn complete, awaiting tool follow-up "
                                        f"({(time.monotonic()-t0)*1000:.0f}ms)"
                                    )
                                    continue
                                break

                        tc = message.tool_call
                        if tc:
                            turn_open = True
                            if not responding_signaled:
                                responding_signaled = True
                                if self.on_responding:
                                    self.on_responding()
                                debug_log(f"  [gemini] Tool call received, stopping mic ({(time.monotonic()-t0)*1000:.0f}ms)")
                            for fc in tc.function_calls:
                                debug_log(f"  [gemini] FC: {fc.name}({fc.args})")
                                function_calls_list.append(f"{fc.name}({dict(fc.args)})")
                            tools_running += 1
                            # Run in the background so audio already queued behind the
                            # call (e.g. "Już sprawdzam.") keeps streaming meanwhile.
                            task = asyncio.create_task(run_tools(tc.function_calls))
                            tool_tasks.add(task)
                            task.add_done_callback(tool_tasks.discard)
                except Exception as e:
                    print(f"  [gemini] RECEIVE ERROR: {e}", flush=True)
                finally:
                    if next_msg is not None:
                        next_msg.cancel()
                    if tool_tasks:
                        # Never abandon a half-run HA action just because the model went quiet.
                        await asyncio.wait(set(tool_tasks), timeout=10)

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

            heard = "".join(input_transcript_parts).strip()
            said = "".join(output_transcript_parts).strip()
            print(f"  [gemini] HEARD (user): {heard!r}", flush=True)
            print(f"  [gemini] SAID (model): {said!r}", flush=True)

        total_ms = (time.monotonic() - t0) * 1000
        debug_log(f"  [gemini] TOTAL: {total_ms:.0f}ms")

        if response_audio_chunks:
            total_audio = sum(len(c) for c in response_audio_chunks)
            debug_log(f"  [gemini] Streamed {len(response_audio_chunks)} audio chunks, {total_audio}B ({total_audio/48000:.1f}s)")

        return function_calls_made.strip() or response_text or ""

    async def _do_search(self, query: str) -> dict:
        """Search web using Gemini generate_content + Google Search."""
        return await web_search(query, self.client)

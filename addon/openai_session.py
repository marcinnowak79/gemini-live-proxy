"""OpenAI Realtime backend, drop-in alternative to GeminiSession.

Exposes the same constructor keywords and the same
``stream_audio(audio_chunks, on_audio_out) -> str`` contract, so proxy_server
does not care which vendor is answering.

Two differences from Gemini are handled here and nowhere else:
  * the API refuses input below 24 kHz, so the 16 kHz device stream is
    resampled on the fly (StreamResampler);
  * turn handling is explicit (``turn_detection: null`` + commit), which maps
    directly onto the activity_start/activity_end pattern already used for
    Gemini's manual VAD.
Output audio is 24 kHz mono PCM16 — exactly what Gemini returns — so the
existing prepare_response_pcm path to the ESP32 is unchanged.
"""
import asyncio
import base64
import json
import os
import time
from typing import AsyncGenerator, Awaitable, Callable

import websockets

from ai_common import (
    QUERY_TOOLS,
    DEVICE_SAMPLE_RATE,
    RESPONSE_SAMPLE_RATE,
    StreamResampler,
    build_prompt,
    build_tool_specs,
    debug_log,
    web_search,
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-realtime-2.1-mini")
OPENAI_VOICE = os.getenv("OPENAI_VOICE", "marin")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
OPENAI_TRANSCRIBE_LANGUAGE = os.getenv("OPENAI_TRANSCRIBE_LANGUAGE", "pl")

RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION = float(os.getenv("RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION", "1.5"))
RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO = float(os.getenv("RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO", "1.2"))
RECEIVE_IDLE_TIMEOUT_GENERAL = float(os.getenv("RECEIVE_IDLE_TIMEOUT_GENERAL", "8.0"))

MAX_TOOL_ROUNDS = int(os.getenv("OPENAI_MAX_TOOL_ROUNDS", "3"))

# Gemini locks output speech to a language via speech_config.language_code.
# The Realtime API has no such parameter — the prompt is the only signal, and
# "Always speak in polsku" leaves the model guessing at pronunciation, which it
# renders with an English accent. This block carries what language_code carries
# for Gemini, so it is deliberately provider-specific rather than shared.
DEFAULT_SPEECH_STYLE_PROMPT = """
=== WYMOWA (KRYTYCZNE) ===
Jesteś polskim asystentem domowym. Mówisz WYŁĄCZNIE po polsku.
Jesteś rodowitym native speakerem języka polskiego z Warszawy. Wymawiaj każde
słowo z naturalną polską fonetyką i prozodią — polskie samogłoski nosowe (ą, ę),
zmiękczenia (ś, ć, ź, dź), szumiące (sz, cz, ż, rz) i akcent na przedostatniej
sylabie. Nigdy nie mów z angielskim akcentem ani nie wymawiaj polskich słów po
angielsku.
=== KONIEC WYMOWY ===
"""

SPEECH_STYLE_PROMPT = os.getenv("OPENAI_SPEECH_STYLE_PROMPT", DEFAULT_SPEECH_STYLE_PROMPT)


class OpenAISession:
    """Manages one OpenAI Realtime session with streaming audio."""

    def __init__(self, entity_list: str, room_lights: dict, ha_context: str,
                 history: list, on_function_call: Callable,
                 voice: str | None = None,
                 on_responding: Callable | None = None,
                 vacuum_enabled: bool = False,
                 local_area_id: str = "",
                 gemini_client=None):
        self.entity_list = entity_list
        self.room_lights = room_lights
        self.ha_context = ha_context
        self.history = history
        self.on_function_call = on_function_call
        self.voice = voice or OPENAI_VOICE
        self.on_responding = on_responding
        self.vacuum_enabled = vacuum_enabled
        self.local_area_id = local_area_id
        self.gemini_client = gemini_client   # web search stays on Gemini

    def _tools(self) -> list[dict]:
        specs = build_tool_specs(list(self.room_lights.keys()), self.vacuum_enabled)
        return [{"type": "function", **s} for s in specs]

    async def _dispatch(self, name: str, args: dict) -> dict:
        if name == "search_web":
            return await web_search(args.get("query", ""), self.gemini_client)
        return await self.on_function_call(name, args)

    async def stream_audio(
        self,
        audio_chunks: AsyncGenerator[bytes, None],
        on_audio_out: Callable[[bytes], Awaitable[None]],
    ) -> str:
        """Stream device audio to OpenAI, stream reply audio back via callback.

        Returns a summary of what happened, for conversation history.
        """
        if not OPENAI_API_KEY:
            print("  [openai] ERROR: OPENAI_API_KEY not configured", flush=True)
            return ""

        prompt = build_prompt(self.entity_list, self.ha_context, self.history,
                              self.local_area_id) + SPEECH_STYLE_PROMPT
        url = f"https://api.openai.com/v1/realtime?model={OPENAI_MODEL}".replace("https://", "wss://")
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

        function_calls_list: list[str] = []
        response_text_parts: list[str] = []
        audio_out_bytes = 0
        responding_signaled = False
        t0 = time.monotonic()

        def signal_responding(why: str):
            nonlocal responding_signaled
            if not responding_signaled:
                responding_signaled = True
                if self.on_responding:
                    self.on_responding()
                debug_log(f"  [openai] {why}, stopping mic ({(time.monotonic()-t0)*1000:.0f}ms)")

        try:
            async with websockets.connect(url, additional_headers=headers,
                                          max_size=None) as ws:
                await ws.send(json.dumps({
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "model": OPENAI_MODEL,
                        "output_modalities": ["audio"],
                        "instructions": prompt,
                        "tools": self._tools(),
                        "tool_choice": "auto",
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": RESPONSE_SAMPLE_RATE},
                                # proxy_server runs its own VAD; server VAD would
                                # fight it and cut commands short
                                "turn_detection": None,
                                "transcription": {
                                    "model": OPENAI_TRANSCRIBE_MODEL,
                                    "language": OPENAI_TRANSCRIBE_LANGUAGE,
                                },
                            },
                            "output": {
                                "format": {"type": "audio/pcm", "rate": RESPONSE_SAMPLE_RATE},
                                "voice": self.voice,
                            },
                        },
                    },
                }))

                send_done = False

                async def send_audio():
                    nonlocal send_done
                    resampler = StreamResampler(DEVICE_SAMPLE_RATE, RESPONSE_SAMPLE_RATE)
                    chunk_n = 0
                    try:
                        async for chunk in audio_chunks:
                            chunk_n += 1
                            if chunk_n == 1:
                                debug_log("  [openai] Sending audio...")
                            pcm = resampler.process(chunk)
                            if pcm:
                                await ws.send(json.dumps({
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(pcm).decode(),
                                }))
                        tail = resampler.flush()
                        if tail:
                            await ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(tail).decode(),
                            }))
                        if chunk_n:
                            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await ws.send(json.dumps({"type": "response.create"}))
                        debug_log(f"  [openai] Audio stream ended, {chunk_n} chunks "
                                  f"({(time.monotonic()-t0)*1000:.0f}ms)")
                    except Exception as e:  # noqa: BLE001
                        print(f"  [openai] SEND ERROR after {chunk_n} chunks: {e}", flush=True)
                    finally:
                        send_done = True

                async def receive_response():
                    nonlocal audio_out_bytes
                    pending: dict[str, asyncio.Task] = {}
                    pending_meta: dict[str, str] = {}
                    rounds = 0
                    got_audio = False

                    while True:
                        if pending:
                            idle = RECEIVE_IDLE_TIMEOUT_AFTER_FUNCTION
                        elif got_audio:
                            idle = RECEIVE_IDLE_TIMEOUT_AFTER_AUDIO
                        else:
                            idle = RECEIVE_IDLE_TIMEOUT_GENERAL
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=idle)
                        except asyncio.TimeoutError:
                            debug_log(f"  [openai] Receive idle timeout after {idle:.1f}s")
                            break
                        except websockets.ConnectionClosed:
                            break

                        ev = json.loads(raw)
                        et = ev.get("type")

                        if et == "error":
                            print(f"  [openai] API ERROR: {ev.get('error', {}).get('message')}",
                                  flush=True)
                            break

                        if et == "response.output_audio.delta":
                            signal_responding("Audio started")
                            pcm = base64.b64decode(ev["delta"])
                            audio_out_bytes += len(pcm)
                            got_audio = True
                            await on_audio_out(pcm)

                        elif et == "conversation.item.input_audio_transcription.completed":
                            # mirrors the Gemini addon's HEARD line used for diagnosing mishearings
                            print(f"  HEARD (user): {ev.get('transcript')}", flush=True)

                        elif et == "response.output_audio_transcript.done":
                            text = ev.get("transcript", "")
                            if text:
                                response_text_parts.append(text)

                        elif et == "response.function_call_arguments.done":
                            signal_responding("Tool call received")
                            name = ev.get("name", "")
                            call_id = ev.get("call_id", "")
                            try:
                                args = json.loads(ev.get("arguments") or "{}")
                            except json.JSONDecodeError:
                                args = {}
                            debug_log(f"  [openai] FC: {name}({args})")
                            function_calls_list.append(f"{name}({args})")
                            # start the tool immediately rather than waiting for
                            # response.done — this is the latency that matters
                            pending[call_id] = asyncio.create_task(self._dispatch(name, args))
                            pending_meta[call_id] = name

                        elif et == "response.done":
                            if not pending:
                                break
                            if rounds >= MAX_TOOL_ROUNDS:
                                print(f"  [openai] tool round limit ({MAX_TOOL_ROUNDS}) reached",
                                      flush=True)
                                break
                            rounds += 1
                            wants_spoken_result = False
                            for call_id, task in list(pending.items()):
                                try:
                                    result = await task
                                except Exception as e:  # noqa: BLE001
                                    result = {"error": str(e)}
                                if pending_meta.get(call_id) in QUERY_TOOLS:
                                    wants_spoken_result = True
                                await ws.send(json.dumps({
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": call_id,
                                        "output": json.dumps(result, ensure_ascii=False),
                                    },
                                }))
                            pending.clear()
                            pending_meta.clear()

                            # For a plain action ("zapal lampkę") the model already
                            # said what it was doing before calling the tool, so a
                            # second utterance is pure latency. Ask for one only when
                            # the tool result *is* the answer, or when nothing has
                            # been spoken yet.
                            if wants_spoken_result or not got_audio:
                                await ws.send(json.dumps({"type": "response.create"}))
                            else:
                                debug_log("  [openai] action already acknowledged, "
                                          "skipping follow-up response")
                                break

                    for task in pending.values():
                        task.cancel()

                await asyncio.gather(send_audio(), receive_response())

        except Exception as e:  # noqa: BLE001
            print(f"  [openai] SESSION ERROR: {e}", flush=True)

        total_ms = (time.monotonic() - t0) * 1000
        debug_log(f"  [openai] TOTAL: {total_ms:.0f}ms")
        if audio_out_bytes:
            debug_log(f"  [openai] Streamed {audio_out_bytes}B audio "
                      f"({audio_out_bytes/2/RESPONSE_SAMPLE_RATE:.1f}s)")

        return " ".join(function_calls_list).strip() or "".join(response_text_parts) or ""

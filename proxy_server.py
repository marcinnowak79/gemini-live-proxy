#!/usr/bin/env python3
"""
Gemini Live Proxy Server — WebSocket bridge between ESP32 and Gemini Live API.

Also supports local testing with Mac microphone/speaker.

Usage:
    python proxy_server.py              # Start proxy server (wait for ESP32)
    python proxy_server.py --local      # Local test with mic/speaker
"""

import asyncio
import json
import logging
import math
import os
import resource
import struct
import sys
import time
import uuid

import numpy as np
import websockets
from dotenv import load_dotenv
from google import genai

from protocol import *
from ha_client import get_exposed_entities, get_ha_context, execute_function, is_vacuum_enabled
from gemini_session import GeminiSession
from timer_manager import TimerManager

load_dotenv()

logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

API_KEY = os.getenv("GEMINI_API_KEY")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8765"))
VOICE = os.getenv("GEMINI_VOICE", "Aoede")
MIC_RMS_MIN_SPEECH = float(os.getenv("MIC_RMS_MIN_SPEECH", "650"))
MIC_RMS_SPEECH_RATIO = float(os.getenv("MIC_RMS_SPEECH_RATIO", "3.0"))
MIC_RMS_NOISE_ALPHA = float(os.getenv("MIC_RMS_NOISE_ALPHA", "0.08"))
MIC_RMS_INITIAL_NOISE = float(os.getenv("MIC_RMS_INITIAL_NOISE", "120"))
MIC_SILENCE_TIMEOUT_MS = int(os.getenv("MIC_SILENCE_TIMEOUT_MS", "2400"))
MIC_NO_SPEECH_TIMEOUT_MS = int(os.getenv("MIC_NO_SPEECH_TIMEOUT_MS", "3500"))
MIC_MAX_STREAM_MS = int(os.getenv("MIC_MAX_STREAM_MS", "7000"))
SESSION_TIMEOUT_SECONDS = float(os.getenv("SESSION_TIMEOUT_SECONDS", "16"))
GEMINI_RETRY_TIMEOUT_SECONDS = float(os.getenv("GEMINI_RETRY_TIMEOUT_SECONDS", "10"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "1"))
DIAG_EVENT_LOOP_LAG_WARN_MS = float(os.getenv("DIAG_EVENT_LOOP_LAG_WARN_MS", "250"))
DIAG_EVENT_LOOP_INTERVAL_SECONDS = float(os.getenv("DIAG_EVENT_LOOP_INTERVAL_SECONDS", "0.25"))
DEBUG_LOGGING = os.getenv("DEBUG_LOGGING", "false").lower() in ("1", "true", "yes", "on")


def debug_log(message: str):
    if DEBUG_LOGGING:
        print(message, flush=True)

# Conversation history (shared across sessions, 5 min timeout)
CONTEXT_TIMEOUT = 300
conversation_history = {"entries": [], "last_time": 0}
recent_actions = {"entries": [], "last_time": 0}


def get_history() -> list:
    if time.monotonic() - conversation_history["last_time"] > CONTEXT_TIMEOUT:
        conversation_history["entries"].clear()
    return conversation_history["entries"]


def add_to_history(role: str, text: str):
    conversation_history["entries"].append({"role": role, "text": text})
    conversation_history["last_time"] = time.monotonic()
    if len(conversation_history["entries"]) > 20:
        conversation_history["entries"] = conversation_history["entries"][-20:]


def remember_action(name: str, args: dict, result: dict | None = None):
    if name == "control_device":
        text = f"control_device entity_id={args.get('entity_id')} action={args.get('action')}"
    elif name == "control_room":
        text = f"control_room room={args.get('room')} action={args.get('action')}"
    elif name == "activate_scene":
        text = f"activate_scene scene_id={args.get('scene_id')}"
    elif name == "set_climate":
        text = (
            f"set_climate entity_id={args.get('entity_id')} "
            f"hvac_mode={args.get('hvac_mode')} temperature={args.get('temperature')}"
        )
    else:
        return

    if result:
        text += f" result={result}"
    recent_actions["entries"].append(text)
    recent_actions["last_time"] = time.monotonic()
    recent_actions["entries"] = recent_actions["entries"][-5:]


def get_recent_action_context() -> str:
    if time.monotonic() - recent_actions["last_time"] > CONTEXT_TIMEOUT:
        recent_actions["entries"].clear()
    if not recent_actions["entries"]:
        return ""
    lines = [
        "",
        "=== RECENT SMART HOME ACTIONS ===",
        "Use these actions to resolve follow-up pronouns like it, this, that, go, ją, je, to, tego, tamto, teraz.",
        "If the user says a follow-up command without naming a target, prefer the most recent matching entity/room below.",
    ]
    for index, entry in enumerate(reversed(recent_actions["entries"]), start=1):
        prefix = "Most recent" if index == 1 else f"Previous {index}"
        lines.append(f"{prefix}: {entry}")
    lines.append("=== END RECENT SMART HOME ACTIONS ===")
    return "\n".join(lines)


timer_manager = TimerManager()

# Streaming audio state keyed by one response session.
_audio_sessions: dict[str, tuple[asyncio.Queue, asyncio.Event]] = {}


def process_rss_mb() -> float:
    usage_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage_kb / 1024


def process_cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


async def event_loop_lag_monitor():
    worst_lag_ms = 0.0
    last_report = time.monotonic()
    while True:
        start = time.monotonic()
        await asyncio.sleep(DIAG_EVENT_LOOP_INTERVAL_SECONDS)
        elapsed = time.monotonic() - start
        lag_ms = max(0.0, (elapsed - DIAG_EVENT_LOOP_INTERVAL_SECONDS) * 1000)
        worst_lag_ms = max(worst_lag_ms, lag_ms)
        if lag_ms >= DIAG_EVENT_LOOP_LAG_WARN_MS:
            debug_log(
                f"[diag] event-loop lag {lag_ms:.0f}ms "
                f"(rss={process_rss_mb():.1f}MB)"
            )
        now = time.monotonic()
        if now - last_report >= 60:
            debug_log(
                f"[diag] event-loop worst lag last 60s: {worst_lag_ms:.0f}ms "
                f"(rss={process_rss_mb():.1f}MB)"
            )
            worst_lag_ms = 0.0
            last_report = now


def make_streaming_wav_header(sample_rate=24000, bits_per_sample=16, channels=1):
    """WAV header with max size placeholder — reader stops at EOF."""
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    header = struct.pack('<4sI4s', b'RIFF', 0x7FFFFFFF, b'WAVE')
    header += struct.pack('<4sIHHIIHH', b'fmt ', 16, 1,
                          channels, sample_rate, byte_rate, block_align, bits_per_sample)
    header += struct.pack('<4sI', b'data', 0x7FFFFFFF)
    return header


def make_error_tone_pcm(sample_rate=24000) -> bytes:
    """Generate a short local fallback tone when Gemini does not answer."""
    chunks = []
    amplitude = 9000
    pattern = [(0.18, 440), (0.08, 0), (0.18, 330), (0.08, 0), (0.22, 220)]
    for duration, freq in pattern:
        frames = int(sample_rate * duration)
        for i in range(frames):
            sample = 0 if freq == 0 else int(amplitude * math.sin(2 * math.pi * freq * i / sample_rate))
            chunks.append(struct.pack("<h", sample))
    return b"".join(chunks)


async def handle_function_call(name: str, args: dict, room_lights: dict,
                                send_audio_cb) -> dict:
    """Handle function calls — HA actions + timers."""
    if name == "set_timer":
        return await timer_manager.set_timer(
            seconds=float(args["seconds"]),
            label=args.get("label", ""),
            action=args.get("action", "notify"),
            media_player_entity_id=args.get("media_player_entity_id", ""),
            media_url=args.get("media_url", ""),
            media_content_type=args.get("media_content_type", ""),
            script_id=args.get("script_id", ""),
        )
    if name == "list_timers":
        return await timer_manager.list_timers()
    if name == "cancel_timer":
        return await timer_manager.cancel_timer(
            timer_id=args.get("timer_id", ""),
            label=args.get("label", ""),
            cancel_all=bool(args.get("cancel_all", False)),
        )
    if name == "stop_timer_alarm":
        return await timer_manager.stop_alarm(
            timer_id=args.get("timer_id", ""),
            label=args.get("label", ""),
            stop_all=bool(args.get("stop_all", False)),
        )

    return await execute_function(name, args, room_lights)


# ============================================================
# ESP32 WebSocket Handler
# ============================================================

async def handle_esp32_connection(websocket, entity_list, room_lights, local_area_id):
    """Handle one ESP32 client connection."""
    debug_log(f"[proxy] ESP32 connected: {websocket.remote_address}")

    client = genai.Client(api_key=API_KEY)

    first_chunk_sent = False
    audio_queue: asyncio.Queue | None = None
    audio_ready: asyncio.Event | None = None
    audio_path = ""

    async def send_audio_to_esp32(audio_data: bytes):
        """Stream audio chunk to ESP32 via HTTP queue."""
        nonlocal first_chunk_sent
        try:
            if audio_queue is not None:
                await audio_queue.put(audio_data)
            if not first_chunk_sent:
                first_chunk_sent = True
                if audio_ready is not None:
                    audio_ready.set()
                await websocket.send(pack_message(MSG_RESPONSE_START, audio_path.encode()))
                debug_log(f"  [stream] MSG_RESPONSE_START sent for {audio_path}")
        except Exception as e:
            print(f"  [stream] Send error: {e}", flush=True)

    def convert_chunk(mono16: bytes) -> bytes:
        """Return mono 16-bit PCM from ESP32.

        Current firmware converts I2S stereo32 to mono16 before sending it over
        WebSocket, so the proxy must not downsample it a second time.
        """
        if len(mono16) < 2:
            return b""
        aligned_len = (len(mono16) // 2) * 2
        return mono16[:aligned_len]

    def chunk_audio_levels(pcm: bytes) -> tuple[int, float]:
        if len(pcm) < 2:
            return 0, 0.0
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.int32)
        if samples.size == 0:
            return 0, 0.0
        abs_samples = np.abs(samples)
        peak = int(np.max(abs_samples))
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        return peak, rms

    try:
        while True:
            # Wait for first audio chunk from ESP32
            raw = await websocket.recv()
            msg_type, data = unpack_message(raw)

            if msg_type != MSG_AUDIO_IN:
                continue

            t0 = time.monotonic()
            debug_log("  [stream] First chunk received, opening Gemini session...")
            await websocket.send(pack_message(MSG_STATE_LISTENING))

            # Create streaming queue for this session
            session_id = uuid.uuid4().hex
            audio_queue = asyncio.Queue()
            audio_ready = asyncio.Event()
            audio_path = f"/response/{session_id}.wav"
            _audio_sessions[session_id] = (audio_queue, audio_ready)
            first_chunk_sent = False

            # Real-time streaming: read ESP32 chunks continuously into a small
            # queue, then feed Gemini from that queue. Keeping websocket reads
            # independent of Gemini backpressure prevents ESP32 write failures.
            # Gemini VAD is helpful but not fully deterministic with continuous
            # mic streams, so the proxy also closes the utterance after silence.
            chunk_count = 0
            total_bytes = 0
            stop_streaming = False
            keep_reading_esp32 = True
            thinking_sent = False
            function_call_seen = False
            buffered_pcm_chunks: list[bytes] = []
            audio_in_queue: asyncio.Queue[tuple[int, bytes, float] | None] = asyncio.Queue(maxsize=512)
            reader_task: asyncio.Task | None = None
            stream_stop_reason = "unknown"
            dropped_audio_messages = 0
            reader_message_count = 0
            reader_last_message_at: float | None = None
            reader_max_gap_ms = 0.0
            max_queue_depth = 0
            max_queue_age_ms = 0.0
            cpu_start = process_cpu_seconds()

            async def enqueue_audio_message(mt: int, payload: bytes):
                nonlocal dropped_audio_messages, reader_message_count, reader_last_message_at
                nonlocal reader_max_gap_ms, max_queue_depth
                now = time.monotonic()
                if reader_last_message_at is not None:
                    reader_max_gap_ms = max(reader_max_gap_ms, (now - reader_last_message_at) * 1000)
                reader_last_message_at = now
                reader_message_count += 1
                if audio_in_queue.full():
                    try:
                        audio_in_queue.get_nowait()
                        dropped_audio_messages += 1
                    except asyncio.QueueEmpty:
                        pass
                await audio_in_queue.put((mt, payload, now))
                max_queue_depth = max(max_queue_depth, audio_in_queue.qsize())

            async def esp32_audio_reader():
                try:
                    await enqueue_audio_message(msg_type, data)
                    while keep_reading_esp32:
                        raw_msg = await websocket.recv()
                        mt, payload = unpack_message(raw_msg)
                        await enqueue_audio_message(mt, payload)
                        if mt == MSG_AUDIO_END:
                            break
                except websockets.exceptions.ConnectionClosed:
                    debug_log("  [stream] ESP32 disconnected during audio stream")
                finally:
                    try:
                        audio_in_queue.put_nowait(None)
                    except asyncio.QueueFull:
                        pass

            reader_task = asyncio.create_task(esp32_audio_reader())

            # Create Gemini session after the ESP32 reader is already draining
            # the websocket. HA/Gemini setup can briefly block; microphone audio
            # cannot wait without back-pressuring the ESP32 websocket sender.
            def on_gemini_responding():
                nonlocal stop_streaming, stream_stop_reason
                if stream_stop_reason == "unknown":
                    stream_stop_reason = "gemini_responding"
                stop_streaming = True  # Stop sending mic audio to Gemini

            current_ha_context = await get_ha_context()
            recent_action_context = get_recent_action_context()
            session_context = f"{current_ha_context or ''}{recent_action_context}"
            if current_ha_context:
                debug_log(f"  [context] {current_ha_context.strip()}")
            else:
                debug_log("  [context] Home Assistant time context unavailable")
            if recent_action_context:
                debug_log(f"  [context] {recent_action_context.strip()}")

            async def handle_function_call_for_session(n, a):
                nonlocal function_call_seen
                function_call_seen = True
                result = await handle_function_call(n, a, room_lights, send_audio_to_esp32)
                remember_action(n, a, result)
                return result

            session = GeminiSession(
                client=client,
                entity_list=entity_list,
                room_lights=room_lights,
                ha_context=session_context,
                history=get_history(),
                on_function_call=handle_function_call_for_session,
                voice=VOICE,
                on_responding=on_gemini_responding,
                vacuum_enabled=is_vacuum_enabled(),
                local_area_id=local_area_id,
            )

            async def realtime_audio_stream():
                """Yield converted audio chunks as they arrive from ESP32."""
                nonlocal chunk_count, total_bytes, stop_streaming, max_queue_age_ms, thinking_sent
                nonlocal stream_stop_reason
                stream_started = time.monotonic()
                last_voice = stream_started
                speech_started = False
                peak_max = 0
                rms_max = 0.0
                noise_floor = MIC_RMS_INITIAL_NOISE
                speech_threshold = MIC_RMS_MIN_SPEECH

                # Keep streaming until Gemini responds (stop_streaming set by receive task)
                try:
                    while not stop_streaming:
                        now = time.monotonic()
                        elapsed_ms = (now - stream_started) * 1000
                        silence_ms = (now - last_voice) * 1000
                        if speech_started and silence_ms >= MIC_SILENCE_TIMEOUT_MS:
                            stream_stop_reason = "local_vad_silence"
                            debug_log(
                                f"  [stream] Local VAD end after {silence_ms:.0f}ms silence "
                                f"(peak={peak_max}, rms={rms_max:.0f}, noise={noise_floor:.0f}, threshold={speech_threshold:.0f})"
                            )
                            break
                        if not speech_started and elapsed_ms >= MIC_NO_SPEECH_TIMEOUT_MS:
                            stream_stop_reason = "local_vad_no_speech"
                            debug_log(
                                f"  [stream] Local VAD no speech timeout "
                                f"({elapsed_ms:.0f}ms, peak={peak_max}, rms={rms_max:.0f}, noise={noise_floor:.0f}, threshold={speech_threshold:.0f})"
                            )
                            break
                        if elapsed_ms >= MIC_MAX_STREAM_MS:
                            stream_stop_reason = "local_vad_max_stream"
                            debug_log(
                                f"  [stream] Local VAD max stream timeout "
                                f"({elapsed_ms:.0f}ms, peak={peak_max}, rms={rms_max:.0f}, noise={noise_floor:.0f}, threshold={speech_threshold:.0f})"
                            )
                            break

                        try:
                            queued = await asyncio.wait_for(audio_in_queue.get(), timeout=0.1)
                        except asyncio.TimeoutError:
                            continue
                        if queued is None:
                            if stream_stop_reason == "unknown":
                                stream_stop_reason = "reader_closed"
                            break

                        mt, d, enqueued_at = queued
                        max_queue_age_ms = max(max_queue_age_ms, (time.monotonic() - enqueued_at) * 1000)
                        if mt == MSG_AUDIO_END:
                            stream_stop_reason = "esp_audio_end"
                            break
                        if mt != MSG_AUDIO_IN:
                            continue

                        pcm = convert_chunk(d)
                        if pcm:
                            peak, rms = chunk_audio_levels(pcm)
                            peak_max = max(peak_max, peak)
                            rms_max = max(rms_max, rms)
                            speech_threshold = max(MIC_RMS_MIN_SPEECH, noise_floor * MIC_RMS_SPEECH_RATIO)
                            is_voice = rms >= speech_threshold
                            if is_voice:
                                speech_started = True
                                last_voice = time.monotonic()
                            else:
                                noise_floor = (
                                    (1.0 - MIC_RMS_NOISE_ALPHA) * noise_floor
                                    + MIC_RMS_NOISE_ALPHA * max(rms, 1.0)
                                )
                            chunk_count += 1
                            total_bytes += len(d)
                            buffered_pcm_chunks.append(pcm)
                            yield pcm
                finally:
                    stop_streaming = True
                    if not thinking_sent:
                        thinking_sent = True
                        try:
                            await websocket.send(pack_message(MSG_STATE_THINKING))
                            debug_log(
                                f"  [stream] MSG_STATE_THINKING sent; ESP32 should stop mic upload "
                                f"(reason={stream_stop_reason})"
                            )
                        except websockets.exceptions.ConnectionClosed:
                            print(
                                f"  [stream] Could not send MSG_STATE_THINKING; ESP32 already disconnected "
                                f"(reason={stream_stop_reason})",
                                flush=True,
                            )
                    debug_log(
                        f"  [stream] Sent {chunk_count} chunks, {total_bytes}B mono16, "
                        f"peak={peak_max}, rms={rms_max:.0f}, noise={noise_floor:.0f}, "
                        f"threshold={speech_threshold:.0f}, dropped={dropped_audio_messages}, "
                        f"reader_msgs={reader_message_count}, max_reader_gap={reader_max_gap_ms:.0f}ms, "
                        f"max_queue_depth={max_queue_depth}, max_queue_age={max_queue_age_ms:.0f}ms, "
                        f"stop_reason={stream_stop_reason}"
                    )

            timed_out = False
            try:
                summary = await asyncio.wait_for(
                    session.stream_audio(
                        audio_chunks=realtime_audio_stream(),
                        on_audio_out=send_audio_to_esp32,
                    ),
                    timeout=SESSION_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                print(f"  [stream] Session timeout ({SESSION_TIMEOUT_SECONDS:.0f}s)", flush=True)
                timed_out = True
                summary = ""
            finally:
                stop_streaming = True
                keep_reading_esp32 = False
                if reader_task is not None and not reader_task.done():
                    reader_task.cancel()
                    try:
                        await reader_task
                    except asyncio.CancelledError:
                        pass

            if (
                timed_out
                and GEMINI_MAX_RETRIES > 0
                and buffered_pcm_chunks
                and not function_call_seen
                and not first_chunk_sent
            ):
                debug_log(
                    f"  [stream] Retrying Gemini once with {len(buffered_pcm_chunks)} buffered audio chunk(s)",
                )

                async def buffered_audio_stream():
                    for chunk in buffered_pcm_chunks:
                        yield chunk

                retry_session = GeminiSession(
                    client=client,
                    entity_list=entity_list,
                    room_lights=room_lights,
                    ha_context=session_context,
                    history=get_history(),
                    on_function_call=handle_function_call_for_session,
                    voice=VOICE,
                    on_responding=on_gemini_responding,
                    vacuum_enabled=is_vacuum_enabled(),
                    local_area_id=local_area_id,
                )
                try:
                    summary = await asyncio.wait_for(
                        retry_session.stream_audio(
                            audio_chunks=buffered_audio_stream(),
                            on_audio_out=send_audio_to_esp32,
                        ),
                        timeout=GEMINI_RETRY_TIMEOUT_SECONDS,
                    )
                    timed_out = False
                except asyncio.TimeoutError:
                    print(f"  [stream] Gemini retry timeout ({GEMINI_RETRY_TIMEOUT_SECONDS:.0f}s)", flush=True)
                    summary = ""

            if timed_out and not first_chunk_sent:
                print("  [stream] Playing local fallback error tone", flush=True)
                fallback = make_error_tone_pcm()
                for offset in range(0, len(fallback), 4096):
                    await send_audio_to_esp32(fallback[offset:offset + 4096])

            total_ms = (time.monotonic() - t0) * 1000
            cpu_ms = (process_cpu_seconds() - cpu_start) * 1000
            debug_log(
                f"  [stream] TOTAL: {total_ms:.0f}ms, cpu={cpu_ms:.0f}ms, "
                f"result: {summary[:80] if summary else 'none'}"
            )

            # Signal end of audio stream
            if first_chunk_sent and audio_queue is not None:
                await audio_queue.put(None)  # EOF sentinel

            # Signal session end (LED cleanup) + drain stale data
            try:
                await websocket.send(pack_message(MSG_RESPONSE_END))
            except websockets.exceptions.ConnectionClosed:
                pass  # ESP32 may have disconnected — that's OK

            if summary:
                add_to_history("user", "[polecenie głosowe]")
                add_to_history("model", summary)

            async def cleanup_audio_session(sid: str):
                await asyncio.sleep(60)
                _audio_sessions.pop(sid, None)

            asyncio.create_task(cleanup_audio_session(session_id))

    except websockets.exceptions.ConnectionClosed:
        debug_log("[proxy] ESP32 disconnected")


async def run_proxy_server(entity_list, room_lights, local_area_id):
    """Run WebSocket server for ESP32 connections."""
    handler = lambda ws: handle_esp32_connection(ws, entity_list, room_lights, local_area_id)

    async with websockets.serve(handler, "0.0.0.0", PROXY_PORT):
        print(f"[proxy] Listening on ws://0.0.0.0:{PROXY_PORT}")
        print(f"[proxy] Waiting for ESP32 connection...")
        await asyncio.Future()  # run forever


# ============================================================
# Local Test Mode (Mac mic/speaker)
# ============================================================

async def run_local_test(entity_list, room_lights, local_area_id=""):
    """Test proxy locally with Mac microphone and speaker."""
    import numpy as np
    import sounddevice as sd

    client = genai.Client(api_key=API_KEY)
    print(f"\n🎤 Local test mode")
    print(f"Voice: {VOICE}")
    print(f"Entities: {len(entity_list.splitlines())}")
    print(f"\nENTER = start talking, ENTER = stop, Ctrl+C = quit\n")

    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("⏸️  ENTER to talk...")
            )

            # Record audio
            chunks = []
            recording = True

            def cb(indata, frames, t, status):
                if recording:
                    chunks.append(indata.copy())

            stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                                    blocksize=1600, callback=cb)
            print("  🔴 Recording... (ENTER to stop)")
            stream.start()
            await asyncio.get_event_loop().run_in_executor(None, lambda: input())
            recording = False
            stream.stop()
            stream.close()

            if not chunks:
                continue

            pcm = np.concatenate(chunks).tobytes()
            print(f"  📤 {len(pcm)} bytes ({len(pcm)/32000:.1f}s)")

            # Collect response audio for playback
            response_audio = []

            async def collect_audio(data: bytes):
                response_audio.append(data)

            # Audio generator (single chunk + silence)
            async def audio_gen():
                silence = b"\x00" * 32000
                full = pcm + silence
                for i in range(0, len(full), 32000):
                    yield full[i:i + 32000]

            current_ha_context = await get_ha_context()
            recent_action_context = get_recent_action_context()
            session_context = f"{current_ha_context or ''}{recent_action_context}"
            if current_ha_context:
                print(f"  [context] {current_ha_context.strip()}", flush=True)
            else:
                print("  [context] Home Assistant time context unavailable", flush=True)
            if recent_action_context:
                print(f"  [context] {recent_action_context.strip()}", flush=True)

            async def handle_local_function_call(n, a):
                result = await handle_function_call(n, a, room_lights, collect_audio)
                remember_action(n, a, result)
                return result

            session = GeminiSession(
                client=client,
                entity_list=entity_list,
                room_lights=room_lights,
                ha_context=session_context,
                history=get_history(),
                on_function_call=handle_local_function_call,
                voice=VOICE,
                vacuum_enabled=is_vacuum_enabled(),
                local_area_id=local_area_id,
            )

            summary = await session.stream_audio(
                audio_chunks=audio_gen(),
                on_audio_out=collect_audio,
            )

            if summary:
                add_to_history("user", "[polecenie głosowe]")
                add_to_history("model", summary)

            # Play collected audio
            if response_audio:
                all_audio = b"".join(response_audio)
                print(f"  🔊 Playing {len(all_audio)} bytes...")
                audio_np = np.frombuffer(all_audio, dtype=np.int16)
                sd.play(audio_np, samplerate=24000)
                sd.wait()

        except KeyboardInterrupt:
            print("\n👋 Bye!")
            break


# ============================================================
# Main
# ============================================================

async def run_audio_http_server():
    """HTTP server — serves per-session response WAV streams for ESP32 media player."""
    from aiohttp import web

    async def handle_response_wav(request):
        session_id = request.match_info["session_id"]
        debug_log(f"  [http] Streaming request {session_id} from {request.remote}")
        session = _audio_sessions.get(session_id)
        if session is None:
            return web.Response(status=404, text="No active session")
        audio_queue, audio_ready = session

        try:
            await asyncio.wait_for(audio_ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            return web.Response(status=504, text="Timeout waiting for audio")

        resp = web.StreamResponse(status=200, headers={
            'Content-Type': 'audio/wav',
            'Cache-Control': 'no-cache',
        })
        await resp.prepare(request)
        await resp.write(make_streaming_wav_header())

        bytes_sent = 0
        while True:
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                break
            if chunk is None:
                break
            await resp.write(chunk)
            bytes_sent += len(chunk)

        debug_log(f"  [http] Streamed {bytes_sent}B ({bytes_sent/48000:.1f}s audio)")
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/response/{session_id}.wav", handle_response_wav)
    runner = web.AppRunner(app)
    await runner.setup()
    http_port = int(os.getenv("HTTP_PORT", "8766"))
    site = web.TCPSite(runner, "0.0.0.0", http_port)
    await site.start()
    print(f"[http] Streaming audio server on http://0.0.0.0:{http_port}/response/<session>.wav", flush=True)


async def main():
    print("=" * 50)
    print("Gemini Live Proxy v2")
    print("=" * 50)

    # Load entities from HA
    print("\n📡 Loading entities from HA...")
    entity_list, room_lights, local_area_id = await get_exposed_entities()
    print(f"  Entities: {len(entity_list.splitlines())}")
    print(f"  Rooms: {list(room_lights.keys())}")
    print(f"  Local area: {local_area_id or 'none'}")
    await timer_manager.start()
    if DEBUG_LOGGING:
        asyncio.create_task(event_loop_lag_monitor())

    # Start HTTP audio server
    await run_audio_http_server()

    if "--local" in sys.argv:
        await run_local_test(entity_list, room_lights, local_area_id)
    else:
        await run_proxy_server(entity_list, room_lights, local_area_id)


if __name__ == "__main__":
    asyncio.run(main())

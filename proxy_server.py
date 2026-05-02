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
import os
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

API_KEY = os.getenv("GEMINI_API_KEY")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8765"))
VOICE = os.getenv("GEMINI_VOICE", "Aoede")
MIC_RMS_MIN_SPEECH = float(os.getenv("MIC_RMS_MIN_SPEECH", "650"))
MIC_RMS_SPEECH_RATIO = float(os.getenv("MIC_RMS_SPEECH_RATIO", "3.0"))
MIC_RMS_NOISE_ALPHA = float(os.getenv("MIC_RMS_NOISE_ALPHA", "0.08"))
MIC_RMS_INITIAL_NOISE = float(os.getenv("MIC_RMS_INITIAL_NOISE", "120"))
MIC_SILENCE_TIMEOUT_MS = int(os.getenv("MIC_SILENCE_TIMEOUT_MS", "1800"))
MIC_NO_SPEECH_TIMEOUT_MS = int(os.getenv("MIC_NO_SPEECH_TIMEOUT_MS", "3500"))
MIC_MAX_STREAM_MS = int(os.getenv("MIC_MAX_STREAM_MS", "7000"))
SESSION_TIMEOUT_SECONDS = float(os.getenv("SESSION_TIMEOUT_SECONDS", "16"))

# Conversation history (shared across sessions, 5 min timeout)
CONTEXT_TIMEOUT = 300
conversation_history = {"entries": [], "last_time": 0}


def get_history() -> list:
    if time.monotonic() - conversation_history["last_time"] > CONTEXT_TIMEOUT:
        conversation_history["entries"].clear()
    return conversation_history["entries"]


def add_to_history(role: str, text: str):
    conversation_history["entries"].append({"role": role, "text": text})
    conversation_history["last_time"] = time.monotonic()
    if len(conversation_history["entries"]) > 20:
        conversation_history["entries"] = conversation_history["entries"][-20:]


timer_manager = TimerManager()

# Streaming audio state keyed by one response session.
_audio_sessions: dict[str, tuple[asyncio.Queue, asyncio.Event]] = {}


def make_streaming_wav_header(sample_rate=24000, bits_per_sample=16, channels=1):
    """WAV header with max size placeholder — reader stops at EOF."""
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    header = struct.pack('<4sI4s', b'RIFF', 0x7FFFFFFF, b'WAVE')
    header += struct.pack('<4sIHHIIHH', b'fmt ', 16, 1,
                          channels, sample_rate, byte_rate, block_align, bits_per_sample)
    header += struct.pack('<4sI', b'data', 0x7FFFFFFF)
    return header


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

    return await execute_function(name, args, room_lights)


# ============================================================
# ESP32 WebSocket Handler
# ============================================================

async def handle_esp32_connection(websocket, entity_list, room_lights, local_area_id):
    """Handle one ESP32 client connection."""
    print(f"[proxy] ESP32 connected: {websocket.remote_address}")

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
                print(f"  [stream] MSG_RESPONSE_START sent for {audio_path}", flush=True)
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
            print(f"  [stream] First chunk received, opening Gemini session...", flush=True)
            await websocket.send(pack_message(MSG_STATE_LISTENING))

            # Create streaming queue for this session
            session_id = uuid.uuid4().hex
            audio_queue = asyncio.Queue()
            audio_ready = asyncio.Event()
            audio_path = f"/response/{session_id}.wav"
            _audio_sessions[session_id] = (audio_queue, audio_ready)
            first_chunk_sent = False

            # Create Gemini session
            def on_gemini_responding():
                nonlocal stop_streaming
                stop_streaming = True  # Stop sending mic audio to Gemini

            current_ha_context = await get_ha_context()
            if current_ha_context:
                print(f"  [context] {current_ha_context.strip()}", flush=True)
            else:
                print("  [context] Home Assistant time context unavailable", flush=True)

            session = GeminiSession(
                client=client,
                entity_list=entity_list,
                room_lights=room_lights,
                ha_context=current_ha_context,
                history=get_history(),
                on_function_call=lambda n, a: handle_function_call(n, a, room_lights, send_audio_to_esp32),
                voice=VOICE,
                on_responding=on_gemini_responding,
                vacuum_enabled=is_vacuum_enabled(),
                local_area_id=local_area_id,
            )

            # Real-time streaming: read ESP32 chunks continuously into a small
            # queue, then feed Gemini from that queue. Keeping websocket reads
            # independent of Gemini backpressure prevents ESP32 write failures.
            # Gemini VAD is helpful but not fully deterministic with continuous
            # mic streams, so the proxy also closes the utterance after silence.
            chunk_count = 0
            total_bytes = 0
            stop_streaming = False
            keep_reading_esp32 = True
            audio_in_queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue(maxsize=128)
            reader_task: asyncio.Task | None = None

            async def enqueue_audio_message(mt: int, payload: bytes):
                if audio_in_queue.full():
                    try:
                        audio_in_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                await audio_in_queue.put((mt, payload))

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
                    print(f"  [stream] ESP32 disconnected during audio stream", flush=True)
                finally:
                    try:
                        audio_in_queue.put_nowait(None)
                    except asyncio.QueueFull:
                        pass

            async def realtime_audio_stream():
                """Yield converted audio chunks as they arrive from ESP32."""
                nonlocal chunk_count, total_bytes, stop_streaming, reader_task
                stream_started = time.monotonic()
                last_voice = stream_started
                speech_started = False
                peak_max = 0
                rms_max = 0.0
                noise_floor = MIC_RMS_INITIAL_NOISE
                speech_threshold = MIC_RMS_MIN_SPEECH
                reader_task = asyncio.create_task(esp32_audio_reader())

                # Keep streaming until Gemini responds (stop_streaming set by receive task)
                try:
                    while not stop_streaming:
                        now = time.monotonic()
                        elapsed_ms = (now - stream_started) * 1000
                        silence_ms = (now - last_voice) * 1000
                        if speech_started and silence_ms >= MIC_SILENCE_TIMEOUT_MS:
                            print(
                                f"  [stream] Local VAD end after {silence_ms:.0f}ms silence "
                                f"(peak={peak_max}, rms={rms_max:.0f}, noise={noise_floor:.0f}, threshold={speech_threshold:.0f})",
                                flush=True,
                            )
                            break
                        if not speech_started and elapsed_ms >= MIC_NO_SPEECH_TIMEOUT_MS:
                            print(
                                f"  [stream] Local VAD no speech timeout "
                                f"({elapsed_ms:.0f}ms, peak={peak_max}, rms={rms_max:.0f}, noise={noise_floor:.0f}, threshold={speech_threshold:.0f})",
                                flush=True,
                            )
                            break
                        if elapsed_ms >= MIC_MAX_STREAM_MS:
                            print(
                                f"  [stream] Local VAD max stream timeout "
                                f"({elapsed_ms:.0f}ms, peak={peak_max}, rms={rms_max:.0f}, noise={noise_floor:.0f}, threshold={speech_threshold:.0f})",
                                flush=True,
                            )
                            break

                        try:
                            queued = await asyncio.wait_for(audio_in_queue.get(), timeout=0.1)
                        except asyncio.TimeoutError:
                            continue
                        if queued is None:
                            break

                        mt, d = queued
                        if mt == MSG_AUDIO_END:
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
                            yield pcm
                finally:
                    stop_streaming = True
                    print(
                        f"  [stream] Sent {chunk_count} chunks, {total_bytes}B mono16, "
                        f"peak={peak_max}, rms={rms_max:.0f}, noise={noise_floor:.0f}, threshold={speech_threshold:.0f}",
                        flush=True,
                    )

            await websocket.send(pack_message(MSG_STATE_THINKING))

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

            total_ms = (time.monotonic() - t0) * 1000
            print(f"  [stream] TOTAL: {total_ms:.0f}ms, result: {summary[:80] if summary else 'none'}", flush=True)

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
        print(f"[proxy] ESP32 disconnected")


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
            if current_ha_context:
                print(f"  [context] {current_ha_context.strip()}", flush=True)
            else:
                print("  [context] Home Assistant time context unavailable", flush=True)

            session = GeminiSession(
                client=client,
                entity_list=entity_list,
                room_lights=room_lights,
                ha_context=current_ha_context,
                history=get_history(),
                on_function_call=lambda n, a: handle_function_call(n, a, room_lights, collect_audio),
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
        print(f"  [http] Streaming request {session_id} from {request.remote}", flush=True)
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

        print(f"  [http] Streamed {bytes_sent}B ({bytes_sent/48000:.1f}s audio)", flush=True)
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

    # Start HTTP audio server
    await run_audio_http_server()

    if "--local" in sys.argv:
        await run_local_test(entity_list, room_lights, local_area_id)
    else:
        await run_proxy_server(entity_list, room_lights, local_area_id)


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Standalone wake-word sample capture server for Home Assistant Voice PE."""

from __future__ import annotations

import asyncio
import re
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import websockets

from protocol import MSG_AUDIO_END, MSG_AUDIO_IN, unpack_message

MSG_CAPTURE_START = 0x10


HOST = "0.0.0.0"
PORT = 8765
CAPTURE_DIR = Path("captures")
NORMALIZED_CAPTURE_DIR = Path("captures_normalized")
MAX_SECONDS = 5.0
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
TARGET_PEAK = 0.7079  # -3 dBFS
MAX_NORMALIZE_GAIN = 8.0
MIN_PEAK_TO_NORMALIZE = 300
DECLICK_HEAD_MS = 120
DECLICK_SPIKE_MS = 12
DECLICK_REST_START_MS = 180
DECLICK_RATIO = 3.0
DECLICK_MIN_PEAK = 5000


def sanitize_sample_type(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
    return value or "unknown"


def save_wav(sample_type: str, chunks: list[bytes]) -> Path:
    output_dir = CAPTURE_DIR / sanitize_sample_type(sample_type)
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().strftime("%Y%m%d_%H%M%S_%f.wav")
    path = output_dir / filename
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"".join(chunks))
    return path


def suppress_initial_click(samples: np.ndarray) -> tuple[np.ndarray, bool, int, int]:
    if samples.size == 0:
        return samples, False, 0, 0

    head_len = min(samples.size, int(SAMPLE_RATE * DECLICK_HEAD_MS / 1000))
    spike_len = min(samples.size, int(SAMPLE_RATE * DECLICK_SPIKE_MS / 1000))
    rest_start = min(samples.size, int(SAMPLE_RATE * DECLICK_REST_START_MS / 1000))
    if head_len <= 0 or spike_len <= 0 or rest_start >= samples.size:
        return samples, False, 0, 0

    abs_samples = np.abs(samples)
    head_peak = int(np.max(abs_samples[:head_len]))
    spike_peak = int(np.max(abs_samples[:spike_len]))
    rest_peak = int(np.percentile(abs_samples[rest_start:], 99.5))
    rest_peak = max(rest_peak, 1)

    if spike_peak < DECLICK_MIN_PEAK or spike_peak < rest_peak * DECLICK_RATIO:
        return samples, False, spike_peak, rest_peak

    cleaned = samples.copy()
    zero_len = min(head_len, spike_len * 3)
    cleaned[:zero_len] = 0.0
    fade_len = min(int(SAMPLE_RATE * 20 / 1000), cleaned.size - zero_len)
    if fade_len > 0:
        fade = np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
        cleaned[zero_len : zero_len + fade_len] *= fade
    return cleaned, True, head_peak, rest_peak


def normalize_pcm16(pcm: bytes) -> tuple[bytes, float, int, bool, int]:
    if not pcm:
        return pcm, 1.0, 0, False, 0
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return pcm, 1.0, 0, False, 0

    samples, declicked, head_peak, rest_peak = suppress_initial_click(samples)

    peak = int(np.max(np.abs(samples)))
    if peak < MIN_PEAK_TO_NORMALIZE:
        return samples.astype(np.int16).tobytes(), 1.0, peak, declicked, rest_peak

    target_peak = TARGET_PEAK * 32767.0
    gain = min(target_peak / peak, MAX_NORMALIZE_GAIN)
    normalized = np.clip(samples * gain, -32768, 32767).astype(np.int16)
    return normalized.tobytes(), gain, peak, declicked, rest_peak


def save_normalized_wav(sample_type: str, raw_path: Path, chunks: list[bytes]) -> tuple[Path, float, int, bool, int]:
    output_dir = NORMALIZED_CAPTURE_DIR / sanitize_sample_type(sample_type)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / raw_path.name
    normalized_pcm, gain, peak, declicked, rest_peak = normalize_pcm16(b"".join(chunks))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(normalized_pcm)
    return path, gain, peak, declicked, rest_peak


async def handle_connection(websocket):
    peer = getattr(websocket, "remote_address", None)
    print(f"[capture] connected peer={peer}", flush=True)

    sample_type = "unknown"
    chunks: list[bytes] = []
    total_bytes = 0
    started = None
    message_count = 0
    reason = "unknown"

    try:
        while True:
            timeout = 30.0 if started is None else 1.0
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            msg_type, data = unpack_message(raw)
            message_count += 1

            if msg_type == MSG_CAPTURE_START:
                sample_type = sanitize_sample_type(data.decode("utf-8", errors="replace"))
                started = time.monotonic()
                print(f"[capture] start sample_type={sample_type}", flush=True)
                continue

            if started is None:
                print(f"[capture] ignoring pre-start msg type={msg_type} len={len(data)}", flush=True)
                continue

            elapsed = time.monotonic() - started
            if message_count <= 20 or message_count % 50 == 0:
                print(
                    f"[capture] msg#{message_count} type={msg_type} len={len(data)} "
                    f"bytes={total_bytes} elapsed={elapsed:.2f}s",
                    flush=True,
                )

            if msg_type == MSG_AUDIO_END:
                reason = "audio_end"
                break

            if msg_type == MSG_AUDIO_IN:
                aligned_len = (len(data) // SAMPLE_WIDTH) * SAMPLE_WIDTH
                if aligned_len:
                    chunks.append(data[:aligned_len])
                    total_bytes += aligned_len

            if elapsed >= MAX_SECONDS:
                reason = "wall_timeout"
                break
    except asyncio.TimeoutError:
        reason = "timeout"
    except websockets.exceptions.ConnectionClosed:
        reason = "connection_closed"
    finally:
        if chunks:
            path = save_wav(sample_type, chunks)
            normalized_path, gain, peak, declicked, rest_peak = save_normalized_wav(sample_type, path, chunks)
            audio_seconds = total_bytes / (SAMPLE_RATE * SAMPLE_WIDTH)
            print(
                f"[capture] saved path={path} reason={reason} "
                f"bytes={total_bytes} audio={audio_seconds:.2f}s messages={message_count}",
                flush=True,
            )
            print(
                f"[capture] normalized path={normalized_path} peak={peak} gain={gain:.2f}x",
                flush=True,
            )
            if declicked:
                print(
                    f"[capture] declicked initial spike before normalize rest_peak={rest_peak}",
                    flush=True,
                )
        else:
            print(
                f"[capture] no audio reason={reason} bytes=0 messages={message_count}",
                flush=True,
            )


async def main():
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    NORMALIZED_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    print(
        f"[capture] listening ws://{HOST}:{PORT}; raw={CAPTURE_DIR.resolve()} "
        f"normalized={NORMALIZED_CAPTURE_DIR.resolve()}",
        flush=True,
    )
    async with websockets.serve(handle_connection, HOST, PORT, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())

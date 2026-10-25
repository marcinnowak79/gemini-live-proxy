"""Silero VAD for the microphone stream.

The proxy used to decide "is this speech" from chunk loudness alone, so a
door slam started an utterance and a quiet room with a humming fridge never
ended one. Silero is a small neural voice detector (one 2 MB ONNX file) that
tells speech from noise at any volume.

Measured on 384 real Voice PE recordings (2026-10-25): no genuine command had
less than 384 ms of speech, while every phrase the vocabulary-primed
transcriber invented out of background noise came from a clip with 0-288 ms.

Model: silero_vad.onnx (v5) from https://github.com/snakers4/silero-vad, MIT licence.
"""
import os

import numpy as np

from ai_common import debug_log

SAMPLE_RATE = 16000
FRAME = 512          # Silero v5 window at 16 kHz = 32 ms
CONTEXT = 64         # trailing samples of the previous window the model expects
FRAME_MS = FRAME * 1000 // SAMPLE_RATE

MODEL_PATH = os.getenv("SILERO_MODEL_PATH",
                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "silero_vad.onnx"))
THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.5"))

_session = None
_load_failed = False


def _load():
    global _session, _load_failed
    if _session is not None or _load_failed:
        return _session
    try:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        # One stream at a time on a Pi: a single thread is fastest and leaves
        # the other cores to the event loop.
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        _session = ort.InferenceSession(MODEL_PATH, sess_options=opts,
                                        providers=["CPUExecutionProvider"])
        print(f"[vad] Silero loaded from {MODEL_PATH}", flush=True)
    except Exception as err:  # noqa: BLE001 - the RMS detector stays as the fallback
        _load_failed = True
        print(f"[vad] Silero unavailable, falling back to loudness VAD: {err}", flush=True)
    return _session


def available() -> bool:
    return _load() is not None


class StreamVAD:
    """Speech probability for a 16 kHz PCM16 stream fed in arbitrary chunks."""

    def __init__(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self._last = 0.0
        self.speech_ms = 0

    def feed(self, pcm: bytes) -> float:
        """Return the highest speech probability among the windows completed by this chunk.

        A chunk too short to complete a window repeats the previous verdict, so
        an odd-sized chunk never reads as a moment of silence.
        """
        session = _load()
        if session is None or len(pcm) < 2:
            return self._last
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        buf = np.concatenate((self._pending, x)) if self._pending.size else x
        if buf.size < FRAME:
            self._pending = buf.copy()
            return self._last
        best = 0.0
        pos = 0
        while buf.size - pos >= FRAME:
            window = buf[pos:pos + FRAME]
            frame = np.concatenate((self._context, window))[None, :]
            prob, self._state = session.run(
                None, {"input": frame, "state": self._state, "sr": self._sr})
            p = float(prob[0][0])
            if p >= THRESHOLD:
                self.speech_ms += FRAME_MS
            best = max(best, p)
            self._context = window[-CONTEXT:]
            pos += FRAME
        self._pending = buf[pos:].copy()
        self._last = best
        return best


def speech_ms(pcm: bytes) -> int:
    """Total milliseconds of detected speech in a finished clip."""
    vad = StreamVAD()
    vad.feed(pcm)
    debug_log(f"  [vad] {vad.speech_ms} ms of speech in {len(pcm) // 32} ms of audio")
    return vad.speech_ms

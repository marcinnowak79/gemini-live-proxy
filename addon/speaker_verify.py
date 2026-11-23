"""Who is speaking, by voice — local ECAPA speaker embeddings via onnxruntime.

Experimental. The model and the household's voice patterns are NOT part of the
add-on: they live in SPEAKER_ID_DIR (default /share/speaker_id) as
  ecapa.onnx   raw 16 kHz float32 audio [1, T] -> embedding [1, 192]
  voices.json  {"voices": {"Name": [[192 floats], ...], ...}}
Voice patterns are biometric data and stay on the user's machine.

A name gets several patterns (one per microphone / recording condition); an
utterance is scored against the best-matching one.
"""

from __future__ import annotations

import json
import os
import threading
import time

import numpy as np

ENABLED = os.getenv("SPEAKER_VERIFICATION", "false").lower() == "true"
DIR = os.getenv("SPEAKER_ID_DIR", "/share/speaker_id")
OWNER = os.getenv("SPEAKER_OWNER", "Marcin")
OK = float(os.getenv("SPEAKER_OK_SCORE", "0.42"))
UNSURE = float(os.getenv("SPEAKER_UNSURE_SCORE", "0.30"))
THREADS = int(os.getenv("SPEAKER_THREADS", "0"))  # 0 = pick the faster of 2/4 at start-up
MAX_SECONDS = 8

_lock = threading.Lock()
_session = None
_voices: dict[str, np.ndarray] = {}


def available() -> bool:
    return ENABLED and os.path.exists(os.path.join(DIR, "ecapa.onnx")) \
        and os.path.exists(os.path.join(DIR, "voices.json"))


def _new_session(threads: int, model: str = "ecapa.onnx"):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1
    return ort.InferenceSession(os.path.join(DIR, model), sess_options=opts,
                                providers=["CPUExecutionProvider"])


def _load() -> None:
    global _session, _voices
    with _lock:
        if _session is not None:
            return
        t0 = time.monotonic()
        _session = _new_session(THREADS or 2)
        with open(os.path.join(DIR, "voices.json")) as f:
            raw = json.load(f)["voices"]
        _voices = {name: np.asarray(p, dtype=np.float32) for name, p in raw.items()}
        print(f"[voice] model loaded in {1000 * (time.monotonic() - t0):.0f} ms, "
              f"voices: {', '.join(f'{k}({len(v)})' for k, v in _voices.items())}", flush=True)


def verify(pcm: bytes) -> dict:
    """Blocking; call via asyncio.to_thread. pcm = 16 kHz int16 mono."""
    _load()
    t0 = time.monotonic()
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768
    x = x[-16000 * MAX_SECONDS:]
    if x.size < 8000:
        return {"decision": "error", "reason": "too short"}
    e = _session.run(None, {"wav": x[None, :]})[0].reshape(-1)
    e /= np.linalg.norm(e) or 1.0
    scores = {name: round(float((p @ e).max()), 3) for name, p in _voices.items()}
    best = max(scores, key=scores.get)
    owner = scores.get(OWNER, 0.0)
    if best == OWNER and owner >= OK:
        decision = "ok"
    elif best == OWNER and owner >= UNSURE:
        decision = "unsure"
    else:
        decision = "no"
    speaker = best if scores[best] >= OK else ""
    return {"decision": decision, "speaker": speaker, "scores": scores,
            "sec": round(x.size / 16000, 2), "ms": round(1000 * (time.monotonic() - t0))}


def warm_up_in_background() -> None:
    """Load the model and time one 3 s inference at start-up, so the first command doesn't pay for it."""
    if not available():
        if ENABLED:
            print(f"[voice] enabled but model/voices missing in {DIR}", flush=True)
        return

    def run():
        global _session
        try:
            _load()
            x = (np.random.default_rng(0).standard_normal((1, 48000)) * 0.03).astype(np.float32)
            timings = {}
            # An optional int8 copy (ecapa_int8.onnx) is timed too; on a Pi 4 it was not faster.
            models = ["ecapa.onnx"] + (["ecapa_int8.onnx"]
                                       if os.path.exists(os.path.join(DIR, "ecapa_int8.onnx")) else [])
            combos = [(m, t) for m in models for t in ([THREADS] if THREADS else [2, 4])]
            for model, threads in combos:
                s = _new_session(threads, model)
                s.run(None, {"wav": x})
                best = float("inf")
                for _ in range(3):
                    t0 = time.monotonic()
                    s.run(None, {"wav": x})
                    best = min(best, time.monotonic() - t0)
                timings[f"{model[:-5]}/{threads}t"] = (round(1000 * best), s)
            pick = min(timings, key=lambda k: timings[k][0])
            _session = timings[pick][1]
            print(f"[voice] warm-up, 3 s of audio: "
                  f"{', '.join(f'{k} {v[0]} ms' for k, v in timings.items())} "
                  f"-> using {pick}", flush=True)
        except Exception as err:  # noqa: BLE001
            print(f"[voice] warm-up ERROR: {type(err).__name__}: {err}", flush=True)

    threading.Thread(target=run, daemon=True, name="voice-warmup").start()

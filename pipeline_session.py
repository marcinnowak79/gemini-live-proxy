"""Speech-to-text -> text model -> text-to-speech, instead of one speech-to-speech model.

On 384 real recordings (2026-10-25) Gemini 3.8 Live picked the right action for
85% of commands and swapped "włącz"/"wyłącz" in 7% of them even on clean audio.
Transcribing first (gpt-4o-transcribe primed with the device names) and letting
a text model choose the tool got 96-97% with no swapped on/off at all, and
reached the tool call sooner. This mirrors how phone and speaker assistants are
built: a dedicated recogniser, then intent handling, then speech synthesis.

The trade-off is that a vocabulary-primed recogniser "hears" device names in
pure noise, so clips with almost no detected speech are dropped before they
reach it (see speech_vad).
"""
import array
import asyncio
import io
import json
import os
import re
import time
import wave
from typing import AsyncGenerator, Awaitable, Callable

import aiohttp
import numpy as np
from google import genai
from google.genai import types

import speech_vad
from ai_common import (
    DEVICE_SAMPLE_RATE,
    QUERY_TOOLS,
    build_prompt,
    build_tool_specs,
    debug_log,
    speak_times_pl,
    web_search,
)


def _cfg(name: str, default: str) -> str:
    """bashio hands unset add-on options through as the literal string "null"."""
    value = os.getenv(name, "").strip()
    return default if not value or value.lower() == "null" else value


OPENAI_API_KEY = _cfg("OPENAI_API_KEY", "")
STT_MODEL = _cfg("PIPELINE_STT_MODEL", "gpt-4o-transcribe")
LLM_MODEL = _cfg("PIPELINE_LLM_MODEL", "gemini-3.1-flash-lite")
TTS_MODEL = _cfg("PIPELINE_TTS_MODEL", "gemini-3.8-flash-lite-tts")
# Used for one sentence when TTS_MODEL fails before any audio; "off" disables.
TTS_FALLBACK_MODEL = _cfg("PIPELINE_TTS_FALLBACK_MODEL", "gemini-3.8-flash-tts")
if TTS_FALLBACK_MODEL.lower() == "off":
    TTS_FALLBACK_MODEL = ""
TTS_VOICE = _cfg("PIPELINE_TTS_VOICE", _cfg("GEMINI_VOICE", "Charon"))
MIN_SPEECH_MS = int(_cfg("PIPELINE_MIN_SPEECH_MS", "300"))
# Experimental speaker verification gate (see _auth_gate).
VERIFY_URL = _cfg("SPEAKER_VERIFY_URL", "").rstrip("/")
VERIFY_TOKEN = _cfg("SPEAKER_VERIFY_TOKEN", "")
LEARN_LOG = _cfg("SPEAKER_LEARN_LOG", "/share/asystent_nauka.jsonl")
_WHOAMI_RE = re.compile(r"^(kim jestem|kto (to )?(do ciebie )?mówi|sprawdź (mój )?głos|(czy )?(mnie )?rozpoznajesz( mnie)?|jak mam na imię|jak się nazywam|wiesz kim jestem|kto ja jestem)")
_LEARN_RE = re.compile(r"^naucz się (?P<what>.+)")
# Band handed to the recogniser, "<low>-<high>" Hz, or "off". The Voice PE
# carries a steady tone near 7.8 kHz in both XMOS channels (on every recording
# since at least 2026-10-25; room-independent), and the Echo Dot a rumble under
# 100 Hz. Cutting both changed the transcripts of the 2026-11-02 recordings:
# Voice PE "przed pochodnią" 4/4 -> "przed pokojem" 2/4, and a garbled
# "Czy światło przekopałeś?" -> "Zgaś światło w przedpokoju".
STT_BAND = _cfg("PIPELINE_STT_BAND", "100-6500").lower()
# A plain action that succeeded is confirmed with one stock "OK" instead of a
# sentence written by the model and synthesised per command.
OK_CONFIRMATION = _cfg("PIPELINE_OK_CONFIRMATION", "true").lower() == "true"
OK_TEXT = "OK."
OK_CACHE_DIR = _cfg("PIPELINE_OK_CACHE_DIR", "/data")
MAX_TOOL_ROUNDS = 3
SAY_FIELD = "say"

# Kept open between commands: a fresh TLS handshake to the STT endpoint costs
# a noticeable slice of the latency budget on every utterance.
_http: aiohttp.ClientSession | None = None


def _http_session() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    return _http

PIPELINE_PROMPT = """
=== TRYB TEKSTOWY ===
Dostajesz tekst rozpoznany z mowy użytkownika; może zawierać drobne błędy rozpoznawania —
zinterpretuj go jako najbardziej prawdopodobne polecenie po polsku. Twoja odpowiedź tekstowa
zostanie przeczytana na głos, więc pisz krótko, naturalnie, bez list i formatowania,
liczby słownie gdy to naturalne. WYJĄTEK: godzinę zawsze pisz cyframi w formacie GG:MM
(np. „Jest 20:28.”, „o 7:05”) — system sam przeczyta ją poprawnie; nie zapisuj godziny słowami.
Przy sterowaniu urządzeniami, scenami, skryptami i klimatyzacją wywołaj narzędzie i wypełnij jego
pole "say" bardzo krótkim potwierdzeniem (np. „Gaszę lampkę na biurku.”).
Przy pytaniach (stan urządzeń, wyszukiwanie, minutniki, parking) najpierw wywołaj narzędzie,
odpowiedź podaj dopiero na podstawie wyniku.
Jeśli tekst nie jest poleceniem ani pytaniem do Ciebie (urywek rozmowy, szum), nie wywołuj
żadnego narzędzia i nie odpowiadaj nic.
=== KONIEC TRYBU TEKSTOWEGO ===
"""


_ok_pcm: bytes | None = None
_ok_lock = asyncio.Lock()


async def _ok_clip(client: genai.Client) -> bytes | None:
    """The "OK" confirmation in the assistant's voice, synthesised once and kept on disk."""
    global _ok_pcm
    if _ok_pcm:
        return _ok_pcm
    async with _ok_lock:
        if _ok_pcm:
            return _ok_pcm
        path = os.path.join(OK_CACHE_DIR, f"ok_{TTS_MODEL}_{TTS_VOICE}.pcm")
        try:
            with open(path, "rb") as f:
                _ok_pcm = f.read() or None
        except OSError:
            pass
        if _ok_pcm:
            return _ok_pcm
        chunks: list[bytes] = []

        async def collect(data: bytes):
            chunks.append(data)

        await _synthesise(client, OK_TEXT, collect)
        if chunks:
            _ok_pcm = _trim_silence(b"".join(chunks))
            try:
                with open(path, "wb") as f:
                    f.write(_ok_pcm)
                print(f"  [pipeline] OK confirmation saved to {path} ({len(_ok_pcm)}B)", flush=True)
            except OSError as err:
                print(f"  [pipeline] OK confirmation not saved: {err}", flush=True)
        return _ok_pcm


def _trim_silence(pcm: bytes, rate: int = 24000, keep_ms: int = 30) -> bytes:
    """Cut the silence TTS puts around a word; a short clip should start at once."""
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    frame = rate // 100
    loud = [i for i in range(0, len(samples), frame)
            if max((abs(x) for x in samples[i:i + frame]), default=0) > 500]
    if not loud:
        return pcm
    keep = rate * keep_ms // 1000
    start = max(0, loud[0] - keep)
    end = min(len(samples), loud[-1] + frame + 3 * keep)
    return samples[start:end].tobytes()


async def _synthesise(client: genai.Client, text: str,
                      on_audio_out: Callable[[bytes], Awaitable[None]]) -> None:
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE))),
    )
    text = speak_times_pl(text)
    t_tts = time.monotonic()
    # Google's TTS now and then rejects a valid request (2026-11-12: 400
    # "Unknown name speechConfig") or runs out of per-model quota (429). Such a
    # failure is retried once on the fallback model, for this sentence only;
    # the next answer starts again on TTS_MODEL. Once audio has gone out, a
    # retry would repeat it, so a failure mid-stream is only logged.
    models = [TTS_MODEL] + ([TTS_FALLBACK_MODEL]
                            if TTS_FALLBACK_MODEL and TTS_FALLBACK_MODEL != TTS_MODEL else [])
    for attempt, model in enumerate(models):
        sent_audio = False
        first = True
        try:
            stream = await client.aio.models.generate_content_stream(
                model=model, contents=text, config=config)
            async for chunk in stream:
                if first:
                    first = False
                    debug_log(f"  [pipeline] tts first chunk {1000 * (time.monotonic() - t_tts):.0f}ms ({model})")
                for cand in chunk.candidates or []:
                    for part in (cand.content.parts if cand.content else None) or []:
                        if part.inline_data and part.inline_data.data:
                            sent_audio = True
                            await on_audio_out(part.inline_data.data)
            return
        except Exception as err:  # noqa: BLE001 - the action already happened; just log
            print(f"  [pipeline] TTS ERROR ({model}): {type(err).__name__}: {err}", flush=True)
            if sent_audio or attempt + 1 >= len(models):
                return
            print(f"  [pipeline] TTS retry on fallback model {models[attempt + 1]}", flush=True)


PIPELINE_PROMPT_OK = PIPELINE_PROMPT.replace(
    """Przy sterowaniu urządzeniami, scenami, skryptami i klimatyzacją wywołaj narzędzie i wypełnij jego
pole "say" bardzo krótkim potwierdzeniem (np. „Gaszę lampkę na biurku.”).""",
    """Przy sterowaniu urządzeniami, scenami, skryptami i klimatyzacją tylko wywołaj narzędzie, bez
tekstu — potwierdzenie „OK” odtworzy system. Jeśli wynik narzędzia pokaże błąd albo brak zmiany,
powiedz o tym jednym krótkim zdaniem.""")


def _vocabulary(entity_list: str) -> str:
    """Device and room names from the entity list, to prime the recogniser."""
    labels = set()
    for line in entity_list.splitlines():
        m = re.match(r"- \S+ \((.*)\)", line)
        if m:
            labels.update(p.strip() for p in m.group(1).split(", ") if "=" not in p)
    return ", ".join(sorted(labels))


def _fit_enums(args: dict, spec: dict | None) -> dict:
    """Snap enum arguments back onto their allowed values.

    The text model occasionally returns a value with stray punctuation, e.g.
    action="turn_off," — which Home Assistant would reject outright.
    """
    props = (spec or {}).get("parameters", {}).get("properties", {})
    fixed = dict(args)
    for key, value in args.items():
        allowed = props.get(key, {}).get("enum")
        if not allowed or not isinstance(value, str) or value in allowed:
            continue
        cleaned = value.strip().strip(".,;:!?\"' ").lower()
        if cleaned in allowed:
            print(f"  [pipeline] fixed {key}={value!r} -> {cleaned!r}", flush=True)
            fixed[key] = cleaned
    return fixed


def _speech_band(pcm: bytes) -> bytes:
    """Drop everything outside STT_BAND before transcription (whole clip, one FFT)."""
    if STT_BAND in ("", "off", "none") or len(pcm) < 64:
        return pcm
    try:
        low, high = (float(v) for v in STT_BAND.split("-"))
    except ValueError:
        return pcm
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float64)
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, 1 / DEVICE_SAMPLE_RATE)
    spectrum[(freqs < low) | (freqs > high)] = 0
    y = np.fft.irfft(spectrum, x.size)
    return np.clip(np.round(y), -32768, 32767).astype(np.int16).tobytes()


def _wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(DEVICE_SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class _HeldSpeech:
    """Synthesise a sentence now, play it only once release() is called."""

    def __init__(self, session: "PipelineSession", text: str):
        self._pending: list[bytes] = []
        self._out: Callable[[bytes], Awaitable[None]] | None = None
        self._task = asyncio.create_task(session._speak(text, self._sink))

    async def _sink(self, data: bytes):
        if self._out is None:
            self._pending.append(data)
        else:
            await self._out(data)

    async def release(self, on_audio_out: Callable[[bytes], Awaitable[None]]):
        while self._pending:
            await on_audio_out(self._pending.pop(0))
        # No await between the drained buffer and the switch, so no chunk can slip in between.
        self._out = on_audio_out
        await self._task

    def cancel(self):
        self._task.cancel()


class PipelineSession:
    """One voice command through STT -> text model with tools -> TTS."""

    def __init__(self, client: genai.Client, entity_list: str, room_lights: dict,
                 ha_context: str, history: list, on_function_call: Callable,
                 voice: str | None = None,
                 on_responding: Callable | None = None,
                 vacuum_enabled: bool = False,
                 local_area_id: str = "",
                 stream_stats: dict | None = None):
        self.client = client
        self.entity_list = entity_list
        self.room_lights = room_lights
        self.ha_context = ha_context
        self.history = history
        self.on_function_call = on_function_call
        self.on_responding = on_responding
        self.vacuum_enabled = vacuum_enabled
        self.local_area_id = local_area_id
        self.stream_stats = stream_stats
        self.voice: dict | None = None

    async def stream_audio(
        self,
        audio_chunks: AsyncGenerator[bytes, None],
        on_audio_out: Callable[[bytes], Awaitable[None]],
    ) -> str:
        """Collect the utterance, then transcribe, act and answer.

        Returns a summary of what happened, for conversation history.
        """
        # The user is still speaking: open the connections now so the handshakes
        # overlap the utterance instead of following it.
        # Left running if the user is quicker: a half-open handshake still saves time.
        asyncio.create_task(self._warm_up())
        pcm = b"".join([chunk async for chunk in audio_chunks])
        t0 = time.monotonic()
        if self.on_responding:
            self.on_responding()

        # The proxy measured speech while streaming; measure here only when it could not.
        speech = (self.stream_stats or {}).get("speech_ms") or 0
        if not speech and speech_vad.available():
            speech = speech_vad.speech_ms(pcm)
        if speech_vad.available() and speech < MIN_SPEECH_MS:
            print(f"  [pipeline] only {speech} ms of speech, not a command — skipped", flush=True)
            return ""

        verify_task = asyncio.create_task(self._verify(pcm)) if VERIFY_URL else None
        text = await self._transcribe(pcm)
        t_stt = time.monotonic()
        if verify_task:
            # Normally done long before STT. If the verifier hangs (e.g. its host is
            # asleep), an ordinary command must not wait for it: give it 0.2 s more,
            # then go on without knowing the speaker. Gated commands wait it out.
            norm = re.sub(r"[^\w\s]", "", (text or "").lower()).strip()
            gated = bool(_WHOAMI_RE.match(norm) or _LEARN_RE.match(norm))
            try:
                self.voice = await asyncio.wait_for(asyncio.shield(verify_task),
                                                    None if gated else 0.2)
            except asyncio.TimeoutError:
                print("  [auth] voice: not ready after STT, skipped for this command", flush=True)
        print(f"  [pipeline] HEARD (user): {text!r}", flush=True)
        if not text:
            return ""

        gated = await self._auth_gate(text, on_audio_out)
        if gated is not None:
            return gated

        first_audio: list[float] = []

        async def timed_audio_out(data: bytes):
            if not first_audio:
                first_audio.append(time.monotonic())
            await on_audio_out(data)

        answer, calls = await self._respond(text, timed_audio_out)
        print(f"  [pipeline] SAID (model): {answer!r}", flush=True)
        first = f"{1000 * (first_audio[0] - t0):.0f}ms" if first_audio else "none"
        print(f"  [pipeline] stt={1000 * (t_stt - t0):.0f}ms first_audio={first} "
              f"total={1000 * (time.monotonic() - t0):.0f}ms", flush=True)

        summary = " ".join(calls)
        if answer:
            summary = f"{summary} {answer}".strip()
        return f"(usłyszano: {text}) {summary}".strip() if summary else ""

    async def _verify(self, pcm: bytes) -> dict:
        """Who is speaking, by voice. Runs alongside STT, so it adds no latency."""
        try:
            async with _http_session().post(
                    f"{VERIFY_URL}/verify", data=pcm, headers={"X-Token": VERIFY_TOKEN},
                    timeout=aiohttp.ClientTimeout(total=3)) as resp:
                resp.raise_for_status()
                res = await resp.json()
        except Exception as err:  # noqa: BLE001
            print(f"  [auth] verify ERROR: {type(err).__name__}: {err}", flush=True)
            res = {"decision": "error"}
        print(f"  [auth] voice: {res}", flush=True)
        return res

    def _speaker_note(self) -> str:
        """One prompt line telling the model who is speaking (or that it is unknown)."""
        if not self.voice:
            return ""
        d = self.voice.get("decision")
        who = {"ok": "Marcin", "unsure": "prawdopodobnie Marcin (niepewne)"}.get(d) or (
            "nieznany — rozpoznanie głosu nie działa" if d == "error" else
            "Ania" if self.voice.get("ania", 0) >= 0.4 else "nierozpoznana osoba (NIE Marcin)")
        return (f"\nMÓWCA (rozpoznany po głosie w TYM poleceniu): {who}. Imię mówiącego znasz "
                "tylko z tej linii — nie bierz go z historii rozmowy, bo wcześniej mógł mówić ktoś inny. "
                "Gdy mówca jest nierozpoznany, a pyta o swoje imię, powiedz, że nie rozpoznajesz jego głosu.\n")

    async def _auth_gate(self, text: str,
                         on_audio_out: Callable[[bytes], Awaitable[None]]) -> str | None:
        """Voice check for 'kim jestem' / 'naucz się …', answered without the model.

        Deterministic on purpose: the decision must not depend on what the model
        makes of the request. Returns the history summary, or None when the
        utterance is not a gated command (or the gate is off).
        """
        if not VERIFY_URL:
            return None
        norm = re.sub(r"[^\w\s]", "", text.lower()).strip()
        whoami, learn = _WHOAMI_RE.match(norm), _LEARN_RE.match(norm)
        if not (whoami or learn):
            return None
        res = self.voice or {"decision": "error"}
        d = res.get("decision")
        print(f"  [auth] {'learn' if learn else 'whoami'}: {res}", flush=True)
        if d == "error":
            reply = "Nie mogę teraz sprawdzić głosu."
        elif whoami:
            reply = {"ok": "To ty, Marcin.",
                     "unsure": "Chyba Marcin, ale nie jestem pewien."}.get(d) or (
                "To chyba Ania." if res.get("ania", 0) >= 0.4 else "Nie rozpoznaję tego głosu.")
        else:
            what = text.split(" ", 2)[2].strip(" .!?") if len(text.split(" ", 2)) > 2 else ""
            reply = {"ok": f"Rozpoznałem cię, Marcin. Zapisałem: {what}. Zabiorę się do tego.",
                     "unsure": "Nie jestem pewien, że to ty. Powiedz to jeszcze raz, trochę dłużej."
                     }.get(d, "Tego może mnie nauczyć tylko Marcin.")
            try:
                with open(LEARN_LOG, "a") as f:
                    f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text,
                                        "what": what, "accepted": d == "ok", **res},
                                       ensure_ascii=False) + "\n")
            except OSError as err:
                print(f"  [auth] learn log ERROR: {err}", flush=True)
        await self._speak(reply, on_audio_out)
        print(f"  [pipeline] SAID (auth): {reply!r}", flush=True)
        return f"(usłyszano: {text}) {reply}"

    async def _warm_up(self) -> None:
        """Cheap authenticated requests that leave HTTPS connections open for reuse."""
        async def openai():
            if OPENAI_API_KEY:
                async with _http_session().get(
                    "https://api.openai.com/v1/models/" + STT_MODEL,
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                ) as resp:
                    await resp.read()

        async def google():
            await self.client.aio.models.get(model=LLM_MODEL)

        async def ok():
            if OK_CONFIRMATION:
                await _ok_clip(self.client)

        results = await asyncio.gather(openai(), google(), ok(), return_exceptions=True)
        for err in results:
            if isinstance(err, Exception):
                debug_log(f"  [pipeline] warm-up failed: {type(err).__name__}: {err}")

    async def _transcribe(self, pcm: bytes) -> str:
        if not OPENAI_API_KEY:
            print("  [pipeline] ERROR: openai_api_key is required for transcription", flush=True)
            return ""
        prompt = ("Polecenia głosowe do inteligentnego domu po polsku, np. włącz, wyłącz, zapal, "
                  "zgaś, klimatyzacja, nawiew, minutnik, parking. Urządzenia: "
                  + _vocabulary(self.entity_list))
        form = aiohttp.FormData()
        form.add_field("file", _wav_bytes(_speech_band(pcm)), filename="in.wav", content_type="audio/wav")
        form.add_field("model", STT_MODEL)
        form.add_field("language", "pl")
        form.add_field("prompt", prompt)
        try:
            async with _http_session().post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                data=form,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    print(f"  [pipeline] STT ERROR {resp.status}: {str(body)[:300]}", flush=True)
                    return ""
                return (body.get("text") or "").strip()
        except Exception as err:  # noqa: BLE001 - surfaced as the error tone by the proxy
            print(f"  [pipeline] STT ERROR: {type(err).__name__}: {err}", flush=True)
            return ""

    def _model_config(self) -> types.GenerateContentConfig:
        specs = build_tool_specs(list(self.room_lights.keys()), self.vacuum_enabled)
        self._specs = {spec["name"]: spec for spec in specs}
        for spec in specs:
            if spec["name"] not in QUERY_TOOLS and not OK_CONFIRMATION:
                # Text written alongside a function call is often left out; a field of
                # the call itself is not, so the confirmation needs no second round trip.
                spec["parameters"] = {**spec["parameters"], "properties": {
                    **spec["parameters"].get("properties", {}),
                    SAY_FIELD: {"type": "string", "description":
                                "Bardzo krótkie potwierdzenie po polsku do powiedzenia na głos."},
                }}
        # Podpowiedź „godzinę mów tak: …” jest dla dostawców Live; tu godzina idzie cyframi
        # i zamienia ją na słowa speak_times_pl przed TTS.
        ha_context = re.sub(r" \(godzinę mów tak:.*?\)(?=\n|$)", "", self.ha_context)
        ha_context += self._speaker_note()
        prompt = build_prompt(self.entity_list, ha_context, self.history,
                              self.local_area_id) + (PIPELINE_PROMPT_OK if OK_CONFIRMATION
                                                     else PIPELINE_PROMPT)
        thinking = (types.ThinkingConfig(thinking_budget=0) if LLM_MODEL.startswith("gemini-2.5")
                    else types.ThinkingConfig(thinking_level="minimal"))
        return types.GenerateContentConfig(
            system_instruction=prompt,
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**s) for s in specs])],
            temperature=0,
            thinking_config=thinking,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def _call_tool(self, fc) -> dict:
        args = _fit_enums({k: v for k, v in (fc.args or {}).items() if k != SAY_FIELD},
                          self._specs.get(fc.name))
        try:
            if fc.name == "search_web":
                return await web_search(args.get("query", ""), self.client)
            return await self.on_function_call(fc.name, args)
        except Exception as err:  # noqa: BLE001 - the model must still get an answer
            print(f"  [pipeline] TOOL ERROR {fc.name}: {err}", flush=True)
            return {"status": "error", "message": str(err)}

    async def _respond(self, text: str,
                       on_audio_out: Callable[[bytes], Awaitable[None]]) -> tuple[str, list[str]]:
        """Let the text model act on the transcript and speak its answer."""
        config = self._model_config()
        contents = [types.Content(role="user", parts=[types.Part(text=text)])]
        made: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            t_llm = time.monotonic()
            try:
                resp = await self.client.aio.models.generate_content(
                    model=LLM_MODEL, contents=contents, config=config)
            except Exception as err:  # noqa: BLE001
                print(f"  [pipeline] LLM ERROR: {type(err).__name__}: {err}", flush=True)
                return "", made
            print(f"  [pipeline] llm={1000 * (time.monotonic() - t_llm):.0f}ms", flush=True)
            calls = resp.function_calls or []
            parts = (resp.candidates[0].content.parts if resp.candidates and resp.candidates[0].content
                     else None) or []
            said = "".join(p.text for p in parts if p.text and not p.thought).strip()
            if not calls:
                if said:
                    await self._speak(said, on_audio_out)
                return said, made

            for fc in calls:
                args = _fit_enums({k: v for k, v in (fc.args or {}).items() if k != SAY_FIELD},
                                  self._specs.get(fc.name))
                debug_log(f"  [pipeline] FC: {fc.name}({args})")
                made.append(f"{fc.name}({args})")
            plain = all(fc.name not in QUERY_TOOLS for fc in calls)
            if plain and OK_CONFIRMATION:
                results = await asyncio.gather(*(self._call_tool(fc) for fc in calls))
                if all(isinstance(r, dict) and r.get("status") == "ok" and not r.get("no_change")
                       for r in results):
                    clip = await _ok_clip(self.client)
                    if clip:
                        await on_audio_out(clip)
                    else:
                        await self._speak(OK_TEXT, on_audio_out)
                    return OK_TEXT, made
                # Failed or nothing changed: let the model say so in its own words.
                contents.append(resp.candidates[0].content)
                contents.append(types.Content(role="user", parts=[
                    types.Part(function_response=types.FunctionResponse(
                        id=fc.id, name=fc.name, response=r if isinstance(r, dict) else {"result": r}))
                    for fc, r in zip(calls, results)
                ]))
                continue

            if not said:
                said = " ".join(str(fc.args[SAY_FIELD]).strip() for fc in calls
                                if fc.args and fc.args.get(SAY_FIELD)).strip()

            # A plain action comes with its confirmation already written. Synthesise it
            # while Home Assistant carries the action out, but hold the audio back
            # until the action is confirmed: never announce something that failed.
            held = _HeldSpeech(self, said) if plain and said else None
            results = await asyncio.gather(*(self._call_tool(fc) for fc in calls))
            ok = all(isinstance(r, dict) and r.get("status") == "ok" and not r.get("no_change")
                     for r in results)
            if held is not None:
                if ok:
                    await held.release(on_audio_out)
                    return said, made
                held.cancel()

            contents.append(resp.candidates[0].content)
            contents.append(types.Content(role="user", parts=[
                types.Part(function_response=types.FunctionResponse(
                    id=fc.id, name=fc.name, response=r if isinstance(r, dict) else {"result": r}))
                for fc, r in zip(calls, results)
            ]))
        return "", made

    async def _speak(self, text: str, on_audio_out: Callable[[bytes], Awaitable[None]]) -> None:
        await _synthesise(self.client, text, on_audio_out)

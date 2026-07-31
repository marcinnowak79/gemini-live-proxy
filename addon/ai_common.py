"""Provider-neutral pieces shared by the Gemini and OpenAI backends.

The system prompt, the tool catalogue and the web-search helper have nothing to
do with which vendor answers the audio, so they live here. Each provider module
adapts these plain dicts to its own SDK types.
"""
import json
import os

from plugins import registry as plugin_registry

MODEL_PROVIDERS = ("gemini", "openai")

ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Dżefrej")
ASSISTANT_GENDER = os.getenv("ASSISTANT_GENDER", "male")
ASSISTANT_SPEAKING_STYLE = os.getenv(
    "ASSISTANT_SPEAKING_STYLE",
    "Steady, efficient, and unhurried. Tone is empathetic, crisp, reassuring, "
    "and lightly dry/sarcastic when appropriate. Avoid exaggerated enthusiasm, "
    "theatrical delivery, and long explanations.",
)
ASSISTANT_LANGUAGE = os.getenv("ASSISTANT_LANGUAGE", "en-US")
ASSISTANT_RESPONSE_LANGUAGE = os.getenv("ASSISTANT_RESPONSE_LANGUAGE", "English")
DEBUG_LOGGING = os.getenv("DEBUG_LOGGING", "false").lower() in ("1", "true", "yes", "on")

# Both providers stream 24 kHz mono PCM16 back; the ESP32 path resamples to 48 kHz.
RESPONSE_SAMPLE_RATE = 24000
# The ESP32 microphone is fixed at 16 kHz by micro_wake_word.
DEVICE_SAMPLE_RATE = 16000


def debug_log(message: str):
    if DEBUG_LOGGING:
        print(message, flush=True)


DEFAULT_SYSTEM_PROMPT_TEMPLATE = """You are a smart home assistant. Always speak in {response_language}. You must answer only in {response_language}.

Rules:
- Answer very briefly, preferably in one sentence.
- Always use tools for smart home control. Never say "done" without calling the appropriate tool.
- If the user does not name a room or location, prefer devices marked local=true.
- If several devices have similar names, choose the local=true device unless the user explicitly names another room/person/location.
- Only choose non-local devices when the user explicitly refers to their room, person, or unique device name.
- For follow-up commands with pronouns such as it, this, that, go, ją, je, to, tego, tamto, teraz, use RECENT SMART HOME ACTIONS to infer the target.
- If the user says "turn it off", "zgaś go", "wyłącz ją", or similar after turning something on, call the same target with action=turn_off.
- When the user asks for room lights, use control_room.
- When the user asks for a specific device, use control_device.
- When the user asks whether a device is on/off or asks for a current device value, call get_device_state.
- When the user asks what is on/off in a room, call get_room_state.
- Timers: for countdown requests, call set_timer. Use list_timers to answer timer status questions. Use cancel_timer to cancel timers.
- When a timer alarm is ringing and the user says "stop", "enough", "wystarczy", "stop timer", or similar, call stop_timer_alarm.
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

FOLLOW_UP_RESOLUTION_PROMPT = """
=== FOLLOW-UP TARGET RESOLUTION ===
Use RECENT SMART HOME ACTIONS to resolve short follow-up commands that refer to a previous target.
This is especially important for pronouns and ellipsis such as it, this, that, go, ją, je, to, tego, tamto, teraz.
If the user says "turn it off", "zgaś go", "wyłącz ją", "a teraz zgaś", or similar after a successful turn_on action, call the same entity with action=turn_off.
If the user says a follow-up command without naming a room or device, prefer the most recent matching entity or room from RECENT SMART HOME ACTIONS.
=== END FOLLOW-UP TARGET RESOLUTION ===
"""

INPUT_LANGUAGE_LOCK_PROMPT = """
=== INPUT LANGUAGE (CRITICAL) ===
The user ALWAYS speaks Polish (pl-PL). Interpret every spoken input as Polish.
Even if a phrase sounds like French, German, English, or another language, assume it is
Polish that was mispronounced or imperfectly recognized, and map it to the closest Polish
smart-home command. Never switch your interpretation to another language. All device and
room names are Polish. If the audio is unclear, prefer the most likely Polish smart-home
command over an unrelated, nonsensical, or non-Polish interpretation.

Two Polish commands sound almost identical but are OPPOSITE in meaning — never confuse them:
- "wyłącz" / "wyłączyć" = turn OFF.
- "włącz" / "włączyć" = turn ON.
Listen carefully to the first syllable: "WY-łącz" (off) has an extra "y" vowel that "włącz" (on) lacks.

Air conditioning ("klimatyzacja"): when asked to turn it ON without a named mode, call
set_climate with hvac_mode=cool. Use hvac_mode=heat ONLY if the user explicitly says heating
("grzanie", "ogrzewanie", "ciepło", "grzej"). For "wyłącz klimatyzację" use hvac_mode=off.
=== END INPUT LANGUAGE ===
"""


def build_persona_prompt() -> str:
    name = ASSISTANT_NAME.strip() or "Dżefrej"
    gender = ASSISTANT_GENDER.strip().lower()
    style = ASSISTANT_SPEAKING_STYLE.strip()

    if gender == "male":
        gender_instruction = (
            f"Your name is {name}. You are male. If you refer to yourself, always use masculine "
            "grammatical forms. In Polish, never describe yourself with feminine forms such as "
            '"zrobiłam", "jestem gotowa", "odpowiedziałam". Use masculine forms such as '
            '"zrobiłem", "jestem gotowy", "odpowiedziałem".'
        )
    elif gender == "female":
        gender_instruction = (
            f"Your name is {name}. You are female. If you refer to yourself, use feminine "
            "grammatical forms where the response language requires gender."
        )
    else:
        gender_instruction = (
            f"Your name is {name}. Avoid unnecessarily gendered self-references unless the "
            "user explicitly asks about your persona."
        )

    prompt = "\n=== ASSISTANT PERSONA AND SPEAKING STYLE ===\n"
    prompt += gender_instruction + "\n"
    if style:
        prompt += f"Speaking style: {style}\n"
    prompt += "=== END ASSISTANT PERSONA AND SPEAKING STYLE ===\n"
    return prompt


def build_prompt(entity_list: str, ha_context: str, history: list,
                 local_area_id: str = "") -> str:
    """Assemble the full system prompt. Identical for every provider."""
    local_context = ""
    if local_area_id:
        local_context = (
            f"\nCurrent Voice PE area: {local_area_id}\n"
            "For commands without an explicit room/location, prefer devices marked local=true.\n"
        )
    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        entities=entity_list,
        context=f"{local_context}{ha_context}",
        response_language=ASSISTANT_RESPONSE_LANGUAGE,
        assistant_name=ASSISTANT_NAME,
        assistant_gender=ASSISTANT_GENDER,
        assistant_speaking_style=ASSISTANT_SPEAKING_STYLE,
    )
    prompt += build_persona_prompt()
    prompt += INPUT_LANGUAGE_LOCK_PROMPT
    if history:
        prompt += "\n=== OSTATNIA ROZMOWA ===\n"
        for h in history:
            role = "Użytkownik" if h["role"] == "user" else "Asystent"
            prompt += f"{role}: {h['text']}\n"
        prompt += "=== KONIEC ===\n"
    prompt += FOLLOW_UP_RESOLUTION_PROMPT
    prompt += plugin_registry.prompt_block()
    return prompt


def build_tool_specs(room_keys: list[str], vacuum_enabled: bool = False) -> list[dict]:
    """The tool catalogue as plain JSON Schema — the single source of truth.

    Gemini wraps these in FunctionDeclaration, OpenAI sends them almost as-is.
    """
    rooms = room_keys if room_keys else ["default"]
    specs = [
        {
            "name": "control_device",
            "description": "Turn on/off/toggle a single HA entity.",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "action": {"type": "string", "enum": ["turn_on", "turn_off", "toggle"]},
            }, "required": ["entity_id", "action"]},
        },
        {
            "name": "control_room",
            "description": "Turn on/off ALL lights in a room at once.",
            "parameters": {"type": "object", "properties": {
                "room": {"type": "string", "enum": rooms},
                "action": {"type": "string", "enum": ["turn_on", "turn_off"]},
            }, "required": ["room", "action"]},
        },
        {
            "name": "get_device_state",
            "description": (
                "Get the current Home Assistant state of one entity on demand. "
                "Use for questions asking whether a device is on/off or asking for current values "
                "such as temperature, battery, media state, or availability."
            ),
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string"},
            }, "required": ["entity_id"]},
        },
        {
            "name": "get_printer_status",
            "description": (
                "Get current Prusa Core One printer status in one call. "
                "Use when the user asks about printer state, print progress, nozzle or hotend "
                "temperature, what is printing, or when the print is expected to finish."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "get_room_state",
            "description": (
                "Get current states for all light/switch entities in a room on demand. "
                "Use for questions like whether lights are on in a room or what is still on."
            ),
            "parameters": {"type": "object", "properties": {
                "room": {"type": "string", "enum": rooms},
            }, "required": ["room"]},
        },
        {
            "name": "activate_scene",
            "description": "Activate a scene. Only when user explicitly asks by name.",
            "parameters": {"type": "object", "properties": {
                "scene_id": {"type": "string"},
            }, "required": ["scene_id"]},
        },
        {
            "name": "run_script",
            "description": "Run a HA script",
            "parameters": {"type": "object", "properties": {
                "script_id": {"type": "string"},
            }, "required": ["script_id"]},
        },
        {
            "name": "set_timer",
            "description": (
                "Set countdown timer. Convert to seconds: 1 minuta=60, 30 sekund=30. "
                "Use action=notify for a normal timer, action=play_media to play configured music/media after the timer, "
                "or action=run_script to run a configured Home Assistant script after the timer."
            ),
            "parameters": {"type": "object", "properties": {
                "seconds": {"type": "number"},
                "label": {"type": "string"},
                "action": {"type": "string", "enum": ["notify", "play_media", "run_script"]},
                "media_player_entity_id": {"type": "string"},
                "media_url": {"type": "string"},
                "media_content_type": {"type": "string"},
                "script_id": {"type": "string"},
            }, "required": ["seconds"]},
        },
        {
            "name": "list_timers",
            "description": "List all active timers and their remaining time. Use for questions like 'how much time is left' or 'what timers are active'.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "cancel_timer",
            "description": "Cancel active timer by id, exact label, or all timers. Use for requests like 'cancel timer', 'cancel music timer', or 'cancel all timers'.",
            "parameters": {"type": "object", "properties": {
                "timer_id": {"type": "string"},
                "label": {"type": "string"},
                "cancel_all": {"type": "boolean"},
            }},
        },
        {
            "name": "stop_timer_alarm",
            "description": "Stop ringing timer alarm audio. Use when the user says enough, stop, wystarczy, stop timer, or asks to silence a finished timer.",
            "parameters": {"type": "object", "properties": {
                "timer_id": {"type": "string"},
                "label": {"type": "string"},
                "stop_all": {"type": "boolean"},
            }},
        },
        {
            "name": "search_web",
            "description": "Search web for current info (weather, news). Use when user asks a question.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        },
        {
            "name": "set_climate",
            "description": "Set climate/AC temperature and mode.",
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "temperature": {"type": "number"},
                "hvac_mode": {"type": "string", "enum": ["off", "cool", "heat", "auto", "fan_only", "dry"]},
            }, "required": ["entity_id"]},
        },
    ]
    if vacuum_enabled:
        specs.append({
            "name": "control_vacuum",
            "description": (
                "Control robot vacuum only when the user explicitly mentions the robot vacuum, "
                "odkurzacz, robot sprzatajacy, sprzatanie, or docking the vacuum. "
                "Never use this for lights, lamps, devices, or pronouns like it/her."
            ),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["start", "return_to_base"]},
            }, "required": ["action"]},
        })
    specs.extend(plugin_registry.tool_specs())
    return specs


# Tools whose *result* is the answer the user is waiting to hear. Everything
# else is an action the assistant already acknowledged out loud before calling
# it, so narrating the result a second time only adds latency and chatter.
BUILTIN_QUERY_TOOLS = frozenset({
    "get_device_state",
    "get_room_state",
    "get_printer_status",
    "list_timers",
    "search_web",
})


class _QueryTools:
    """The query-tool set, resolved live so plugins can contribute to it.

    Plugins load after this module is imported, and a plugin action whose
    outcome is uncertain — a booking that may find nothing free — belongs in
    here just as much as get_device_state does.
    """

    def __contains__(self, name: object) -> bool:
        return name in BUILTIN_QUERY_TOOLS or name in plugin_registry.query_tools()

    def __iter__(self):
        return iter(BUILTIN_QUERY_TOOLS | plugin_registry.query_tools())


QUERY_TOOLS = _QueryTools()


class StreamResampler:
    """Linear-interpolation resampler that survives chunk boundaries.

    The ESP32 captures at 16 kHz but the OpenAI Realtime API refuses anything
    below 24 kHz. Resampling each chunk independently would drop a fraction of a
    sample at every boundary and add a faint click 25 times a second, so the
    trailing sample is carried into the next call.
    """

    def __init__(self, src_rate: int, dst_rate: int):
        import numpy as np

        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.ratio = src_rate / dst_rate
        self._buf = np.zeros(0, dtype=np.float32)   # input samples not yet consumed
        self._pos = 0.0                             # read position inside _buf

    def process(self, pcm: bytes) -> bytes:
        import numpy as np

        if self.src_rate == self.dst_rate or not pcm:
            return pcm
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        buf = np.concatenate((self._buf, x)) if self._buf.size else x
        if buf.size < 2:
            self._buf = buf
            return b""

        # how many output samples fit before we run past the last input sample
        n_out = int(np.floor((buf.size - 1 - self._pos) / self.ratio)) + 1
        if n_out <= 0:
            self._buf = buf
            return b""

        idx = self._pos + np.arange(n_out, dtype=np.float64) * self.ratio
        y = np.interp(idx, np.arange(buf.size, dtype=np.float64), buf)

        next_pos = self._pos + n_out * self.ratio
        keep = int(np.floor(next_pos))
        self._buf = buf[keep:].copy()
        self._pos = next_pos - keep
        return np.clip(np.round(y), -32768, 32767).astype(np.int16).tobytes()

    def flush(self) -> bytes:
        """Emit whatever is left once the input stream ends."""
        import numpy as np

        if self.src_rate == self.dst_rate or self._buf.size == 0:
            return b""
        y = self._buf[int(self._pos):] if self._pos < self._buf.size else np.zeros(0)
        self._buf = np.zeros(0, dtype=np.float32)
        self._pos = 0.0
        if y.size == 0:
            return b""
        return np.clip(np.round(y), -32768, 32767).astype(np.int16).tobytes()


async def web_search(query: str, gemini_client=None) -> dict:
    """Grounded web search, used by both providers.

    Kept on Gemini regardless of the active voice provider: it is a plain
    request/response call, it already works, and it means switching voice
    providers does not silently change what the assistant knows.
    """
    debug_log(f"  [search] {query}")
    if gemini_client is None:
        return {"error": "web search unavailable (no Gemini API key configured)"}
    try:
        from google.genai import types

        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"{query}. Answer in one sentence in {ASSISTANT_RESPONSE_LANGUAGE}.",
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        debug_log(f"  [search] -> {response.text[:100]}")
        return {"result": response.text}
    except Exception as err:  # noqa: BLE001 - surfaced to the model as a tool result
        print(f"  [search] ERROR: {err}", flush=True)
        return {"error": str(err)}


def json_dumps_compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

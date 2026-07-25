"""Chooses which voice backend answers a given session.

The provider is resolved once per session, immediately before connecting, so
flipping the Home Assistant selector takes effect on the very next command with
no addon restart. If the entity is missing or unreadable the configured default
is used — a broken helper must never take the assistant down.
"""
import os

import ha_client
from ai_common import debug_log
from gemini_session import GeminiSession
from openai_session import OpenAISession

def _cfg(name: str, default: str = "") -> str:
    """bashio hands unset add-on options through as the literal string "null"."""
    value = os.getenv(name, default).strip()
    return "" if value.lower() == "null" else value


DEFAULT_PROVIDER = _cfg("AI_PROVIDER", "gemini").lower()
PROVIDER_ENTITY = _cfg("AI_PROVIDER_ENTITY")

# What a Home Assistant input_select option may say -> canonical provider name.
_ALIASES = {
    "gemini": "gemini",
    "google": "gemini",
    "openai": "openai",
    "chatgpt": "openai",
    "gpt": "openai",
}


def normalize_provider(value: str | None) -> str | None:
    """Map a free-form selector label onto a provider id."""
    if not value:
        return None
    text = value.strip().lower()
    if text in _ALIASES:
        return _ALIASES[text]
    for needle, provider in _ALIASES.items():
        if needle in text:
            return provider
    return None


async def resolve_provider() -> str:
    """Read the live selector, falling back to the configured default."""
    default = normalize_provider(DEFAULT_PROVIDER) or "gemini"
    if not PROVIDER_ENTITY:
        return default
    try:
        state = await ha_client.get_entity_state(PROVIDER_ENTITY)
    except Exception as err:  # noqa: BLE001 - never let the helper break a command
        print(f"  [provider] cannot read {PROVIDER_ENTITY}: {err}", flush=True)
        return default
    chosen = normalize_provider(state)
    if chosen is None:
        if state is not None:
            print(f"  [provider] unrecognized value {state!r} in {PROVIDER_ENTITY}, "
                  f"using {default}", flush=True)
        return default
    debug_log(f"  [provider] {PROVIDER_ENTITY} = {state!r} -> {chosen}")
    return chosen


def create_session(provider: str, *, gemini_client, entity_list: str, room_lights: dict,
                   ha_context: str, history: list, on_function_call,
                   voice: str | None = None, on_responding=None,
                   vacuum_enabled: bool = False, local_area_id: str = ""):
    """Build a session object for the given provider.

    Both classes expose the same stream_audio(audio_chunks, on_audio_out)
    contract, so the caller needs no further branching.
    """
    if provider == "openai":
        return OpenAISession(
            entity_list=entity_list,
            room_lights=room_lights,
            ha_context=ha_context,
            history=history,
            on_function_call=on_function_call,
            # `voice` is the caller's Gemini voice (e.g. Charon) and is meaningless
            # here; OpenAISession falls back to OPENAI_VOICE when given None
            voice=None,
            on_responding=on_responding,
            vacuum_enabled=vacuum_enabled,
            local_area_id=local_area_id,
            gemini_client=gemini_client,
        )
    return GeminiSession(
        client=gemini_client,
        entity_list=entity_list,
        room_lights=room_lights,
        ha_context=ha_context,
        history=history,
        on_function_call=on_function_call,
        voice=voice,
        on_responding=on_responding,
        vacuum_enabled=vacuum_enabled,
        local_area_id=local_area_id,
    )

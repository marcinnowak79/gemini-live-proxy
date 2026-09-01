"""xAI Grok Voice backend.

The endpoint speaks the OpenAI Realtime protocol (same events, same audio
framing), so the entire streaming state machine is inherited from
OpenAISession — only the connection profile below differs. Billing is a flat
rate per audio minute, not per token.

Deliberate differences from the OpenAI profile:
  * default voice is empty — the server picks its own default, which cannot
    become invalid when xAI renames voices;
  * input transcription is off by default (XAI_TRANSCRIBE_MODEL="") because
    xAI does not host OpenAI's gpt-4o-transcribe; without it the
    "HEARD (user)" diagnostic line is simply absent;
  * the Polish pronunciation block is kept — Grok autodetects language, but
    the block anchors the accent and costs nothing.
"""
import os

from openai_session import DEFAULT_SPEECH_STYLE_PROMPT, OpenAISession


class GrokSession(OpenAISession):
    """One xAI Grok Voice session over the Realtime-compatible endpoint."""

    provider = "grok"
    ws_url_base = "wss://api.x.ai/v1/realtime"
    api_key = os.getenv("XAI_API_KEY", "")
    model = os.getenv("XAI_MODEL", "grok-voice-latest")
    default_voice = os.getenv("XAI_VOICE", "")
    transcribe_model = os.getenv("XAI_TRANSCRIBE_MODEL", "")
    transcribe_language = os.getenv("XAI_TRANSCRIBE_LANGUAGE", "pl")
    speech_style_prompt = os.getenv("XAI_SPEECH_STYLE_PROMPT",
                                    DEFAULT_SPEECH_STYLE_PROMPT)

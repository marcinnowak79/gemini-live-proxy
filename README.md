# Gemini Live Proxy for Home Assistant Voice PE

Experimental first beta of a Gemini Live based voice flow for Home Assistant Voice PE.

This project exists because I wanted to reduce perceived response latency and make the conversation feel more natural. The standard Home Assistant Voice Assistant flow did not give me the streaming behavior I wanted: it records the utterance, sends it after capture, then waits for the response. This add-on is paired with custom ESPHome firmware so the device can stream microphone audio continuously to Gemini Live and play response audio as soon as it is available.

The matching firmware fork is here:

- [Home Assistant Voice PE Gemini firmware](https://github.com/marcinnowak79/home-assistant-voice-pe/tree/gemini-live-proxy)

This is not an official Home Assistant, ESPHome, Nabu Casa, or Google project. It was vibe-coded as a working experiment and is published mainly as inspiration for people exploring lower-latency voice assistant flows. Treat it as beta software: read the code, adapt it to your setup, and do not assume production-level stability or security hardening.

## What It Does

WebSocket bridge between an ESPHome firmware running on Home Assistant Voice PE and the Gemini Live API. The Home Assistant add-on exposes:

- `8765/tcp` - WebSocket audio/control channel for the ESP32 firmware
- `8766/tcp` - HTTP streaming endpoint used by the ESP32 media player for responses

## Install as a Home Assistant Add-on

1. Copy the `addon/` directory into your Home Assistant add-ons directory, or publish this repository and add it as a Home Assistant add-on repository.
2. Rebuild/install the add-on.
3. Configure the required option:
   - `gemini_api_key`: Google Gemini API key
4. Optional options:
   - `gemini_model`: Gemini Live model name
   - `gemini_voice`: Gemini voice name
   - `assistant_language`: BCP-47 language code, for example `pl-PL`
   - `assistant_response_language`: language phrase used in the prompt, for example `English`
   - `system_prompt_template`: full system prompt template shown to Gemini
   - `room_aliases_json`: JSON object mapping entity ID prefixes to room names
   - `vacuum_entity_id`: Home Assistant vacuum entity; enables the vacuum tool when set
   - `ha_exposed_only`: when true, only entities exposed to Assist are sent to Gemini
   - `timer_media_player_entity_id`: default media player for timer media actions
   - `timer_default_media_url`: default URL played for "play music after timer" commands
   - `timer_default_script_id`: default script for delayed script timer actions

Example `room_aliases_json`:

```json
{"living_room":"living room","bedroom":"bedroom","kitchen":"kitchen"}
```

The add-on reads `/config/.storage/core.entity_registry` and prefers Home Assistant `area_id` for room grouping. Prefix aliases are only a fallback for entities without an area.

The prompt template supports these placeholders:

- `{entities}` - list of Home Assistant entities available to Gemini
- `{context}` - current Home Assistant time zone, date/time and location context
- `{response_language}` - value from `assistant_response_language`

Keep `{entities}` and `{context}` in custom prompts unless you intentionally want to hide devices or context from Gemini.

### Local Add-on Deployment over SSH

On Home Assistant OS / Supervised installs, local add-ons live under `/addons/local`.

```bash
ssh root@homeassistant.local 'mkdir -p /addons/local/gemini-live-proxy'
rsync -av --delete addon/ root@homeassistant.local:/addons/local/gemini-live-proxy/
ssh root@homeassistant.local 'ha addons reload'
ssh root@homeassistant.local 'ha addons rebuild local_gemini_live_proxy'
ssh root@homeassistant.local 'ha addons start local_gemini_live_proxy'
```

If you change `config.yaml` options or bump the add-on version, run:

```bash
ssh root@homeassistant.local 'ha store reload'
ssh root@homeassistant.local 'ha addons update local_gemini_live_proxy'
ssh root@homeassistant.local 'ha addons restart local_gemini_live_proxy'
```

Verify the add-on:

```bash
ssh root@homeassistant.local 'ha addons info local_gemini_live_proxy'
ssh root@homeassistant.local 'ha addons logs local_gemini_live_proxy'
nc -zv homeassistant.local 8765
nc -zv homeassistant.local 8766
```

The `ha addons` command may print a deprecation warning and suggest `ha apps`; both command groups currently target the same Supervisor API.

## Documentation and Releases

- Detailed add-on documentation: [`addon/DOCS.md`](addon/DOCS.md)
- Changelog: [`addon/CHANGELOG.md`](addon/CHANGELOG.md)
- Release checklist: [`RELEASE.md`](RELEASE.md)
- Security notes: [`SECURITY.md`](SECURITY.md)

For public releases, bump `version` in `addon/config.yaml`, update `addon/CHANGELOG.md`, tag the commit as `vX.Y.Z`, and publish a GitHub Release. For a better public installation experience, publish prebuilt images to GHCR and set the `image` field in `addon/config.yaml`.

## ESPHome Firmware

In the ESPHome `secrets.yaml`, set the proxy URL:

```yaml
gemini_proxy_url: "ws://homeassistant.local:8765"
```

If `homeassistant.local` is not resolvable from the device, use the Home Assistant IP address instead.

## Standalone Development

Copy `.env.example` to `.env` and fill in local values. Do not commit `.env`.

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
python proxy_server.py
```

For standalone mode, set `HA_ENTITY_REGISTRY_PATH` to a local copy of Home Assistant's `core.entity_registry`.

## Security Notes

Do not publish:

- `.env`
- ESPHome `secrets.yaml`
- Home Assistant tokens
- Google API keys
- full Home Assistant `.storage/` contents

If a secret was ever committed or shared, rotate it before publishing.

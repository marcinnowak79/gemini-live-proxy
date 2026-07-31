# Gemini Live Proxy Documentation

Gemini Live Proxy connects Home Assistant Voice PE firmware to the Gemini Live API.

The add-on runs two local services:

- WebSocket server on `8765/tcp` for microphone audio and control messages from the ESP32.
- HTTP server on `8766/tcp` for response audio streamed back to the ESP32 media player.

The Home Assistant add-on UI includes English help text for every configuration
option. The same English text is available for both English and Polish Home
Assistant UI locales. Hover or open the help icon next to an option to see what
it controls, how it affects runtime behavior, and typical values.

`response_prebuffer_ms` controls how much Gemini response audio is collected
before playback starts on the ESP32. Increase it when response audio stutters or
appears to restart; decrease it only if minimizing first-audio latency matters
more than playback smoothness.

## Requirements

- Home Assistant OS or Home Assistant Supervised with Supervisor add-ons enabled.
- A Gemini API key with access to the Gemini Live model configured in the add-on.
- ESPHome firmware built from the matching `home-assistant-voice-gemini.yaml` configuration.
- The Voice PE device and Home Assistant host must be reachable on the same network.

## Add-on Options

### `ai_provider`

Which backend answers voice commands: `gemini` (default) or `openai`. Both use
the same system prompt, the same tools and the same Home Assistant integration —
only the model behind the socket changes.

### `ai_provider_entity`

Optional. An `input_select` entity read at the start of **every** session, which
overrides `ai_provider`. This lets you switch backends from a dashboard without
restarting the add-on, so you can compare them on consecutive commands.

Create a dropdown helper (Settings → Devices & Services → Helpers) with options
such as `Gemini` and `ChatGPT`, then point this option at it, e.g.
`input_select.asystent_model`. Matching is fuzzy: `Gemini`/`Google` select
Gemini, and `ChatGPT`/`OpenAI`/`GPT` select OpenAI.

If the entity is missing, unreadable, or holds an unrecognized value, the
add-on falls back to `ai_provider` and logs why — a broken helper never takes
the assistant down.

### `openai_api_key`

Required when OpenAI is selected. Note that the `search_web` tool still runs on
Gemini, so keep `gemini_api_key` set even when OpenAI answers the audio.

### `openai_model`

Defaults to `gpt-realtime-2.1`. Do not drop to `gpt-realtime-2.1-mini` for a
setup with many similarly-named entities: measured on this installation (44
exposed entities, the command "włącz lampkę na biurku", 10 runs each), the mini
model picked the correct entity 7/10 times, repeatedly turning on a desk lamp in
another room instead. The full model scored 10/10, as did Gemini.

Strengthening the "prefer local=true" prompt rule did **not** help the mini model
(6/10), so this is a capacity limit rather than a wording problem.

### `openai_voice`

Defaults to `cedar`. Other options include `marin`, `alloy`, `sage`, `verse`,
`echo`, `shimmer`. The Gemini voice setting (`gemini_voice`) does not apply
here — the two providers have completely separate voice catalogues.

**Polish pronunciation.** Gemini pins output speech with
`speech_config.language_code = pl-PL`; the Realtime API has **no equivalent
parameter**, so the prompt is the only signal. The stock instruction
("Always speak in {response_language}") is not enough and the model reads Polish
with an English accent. `openai_session.py` therefore appends its own
`SPEECH_STYLE_PROMPT` describing Polish phonetics (nasal vowels, soft
consonants, penultimate stress). Override it with the
`OPENAI_SPEECH_STYLE_PROMPT` environment variable if you switch languages;
do not simply delete it, or pronunciation regresses.

### `gemini_api_key`

Required. Your Google Gemini API key.

Do not share logs or screenshots that expose this value.

### `gemini_model`

Gemini Live model name. The default is:

```text
gemini-3.1-flash-live-preview
```

### `gemini_voice`

Gemini prebuilt voice name. The default is:

```text
Charon
```

Restart the add-on after changing the voice.

### `assistant_name`

Persona name inserted into the assistant instruction block.

### `assistant_gender`

Controls grammatical self-reference instructions. Supported values:

- `male`
- `female`
- `neutral`

For Polish, `male` instructs the assistant to use forms such as `zrobiłem` and `jestem gotowy`.

### `assistant_speaking_style`

Short tone/style instruction appended to the prompt. Use this for guidance like:

```text
Steady, efficient, and unhurried. Tone is empathetic, crisp, reassuring, and lightly dry/sarcastic when appropriate.
```

### `assistant_language`

BCP-47 language code used for speech configuration.

Examples:

```text
en-US
pl-PL
de-DE
```

### `assistant_response_language`

Language phrase inserted into the system prompt.

Examples:

```text
English
Polish
German
```

### `system_prompt_template`

Full system prompt template sent to Gemini.

Supported placeholders:

- `{entities}` - entity list exposed to Gemini.
- `{context}` - Home Assistant time, timezone and location context.
- `{response_language}` - value from `assistant_response_language`.
- `{assistant_name}`, `{assistant_gender}`, `{assistant_speaking_style}` - optional persona placeholders.

Keep `{entities}` and `{context}` unless you intentionally want to hide devices or context from Gemini.

Most users should edit `assistant_name`, `assistant_gender`, `assistant_speaking_style`, and `gemini_voice` instead of replacing the whole prompt.

### `room_aliases_json`

Optional JSON object mapping entity ID prefixes to room names.

Example:

```json
{"living_room":"living room","bedroom":"bedroom","kitchen":"kitchen"}
```

The add-on prefers Home Assistant `area_id` where available. Prefix aliases are a fallback for entities without area metadata.

### `vacuum_entity_id`

Optional Home Assistant vacuum entity ID. Setting this enables the `control_vacuum` tool.

Example:

```text
vacuum.robot_vacuum
```

Leave empty if you do not want Gemini to control a vacuum.

### `ha_exposed_only`

When `true`, Gemini receives only entities exposed to Home Assistant Assist/Conversation.

This is the recommended default because it reduces prompt size and avoids exposing private or technical entities. Set it to `false` only if you deliberately want Gemini to see every supported entity from the Home Assistant entity registry.

### Timer options

The add-on supports multiple delayed timers. Timers are persisted in the add-on data directory and are restored after an add-on restart.

`timer_media_player_entity_id` is the default Home Assistant media player used by timer actions that play audio.

Example:

```text
media_player.home_assistant_voice_0a32f9_media_player
```

`timer_default_media_url` is the default URL played when the user asks for music/media after a timer without naming a specific URL.

Example:

```text
http://homeassistant.local:8123/local/timer_music.mp3
```

`timer_default_media_content_type` is passed to `media_player.play_media`. The default is `music`.

`timer_alarm_repeat_interval_seconds` controls how often the default timer alarm media is replayed while a finished timer is ringing. The default is `3`.

`timer_default_script_id` is the default Home Assistant script called for timer requests that should run a script after the delay.

Example:

```text
script.timer_play_music
```

Voice commands supported by the timer tool include:

- setting multiple timers
- asking what timers are active
- asking how much time is left
- cancelling a named timer
- cancelling all timers
- stopping a ringing timer alarm
- playing configured media after a timer
- running a configured script after a timer

### Plugin options

Plugins add capabilities that are not smart-home control — the office parking
booking is the first one. They live outside the image, in a directory under
`/share`, so adding one is a copy plus a restart instead of a rebuild and a
release, and their credentials survive add-on updates.

`plugins_dir` is where the proxy looks for them. The default is
`/share/asystent_plugins`. Each subdirectory containing a `plugin.py` is loaded;
one that fails to import, raises or hangs is logged and skipped, and the
assistant carries on without it.

`plugin_timeout_seconds` bounds a single plugin call. The default is `6`, capped
at 7.5 — beyond that the voice provider stops waiting for the tool and the
assistant goes silent, which is worse than an error it can narrate.

`plugin_api_token` enables `POST /plugins/<id>/secret/<name>` on the HTTP port,
used to push in credentials that expire and can only be minted elsewhere:

```bash
curl -X POST http://<ha-host>:8766/plugins/parking/secret/cookie \
     -H "X-Plugin-Token: <plugin_api_token>" --data-binary "$COOKIE"
```

The endpoint only routes; the plugin decides whether to accept the value. Left
empty — the default — it returns 403 and writes nothing.

## ESPHome Configuration

Set the proxy URL in your ESPHome `secrets.yaml`:

```yaml
gemini_proxy_url: "ws://homeassistant.local:8765"
```

If mDNS does not work in your network, use the Home Assistant IP address:

```yaml
gemini_proxy_url: "ws://192.168.1.10:8765"
```

## Troubleshooting

### Add-on starts but the device does not respond

Check that ports are reachable from your computer or from the same network:

```bash
nc -zv homeassistant.local 8765
nc -zv homeassistant.local 8766
```

Check add-on logs:

```bash
ha addons logs local_gemini_live_proxy
```

### Gemini sees the wrong devices

Keep `ha_exposed_only` enabled and expose the desired entities to Assist in Home Assistant. If entities do not have Home Assistant areas, configure `room_aliases_json`.

### The Home Assistant UI shows an update that fails

For local add-ons, make sure only one add-on folder with the same `slug` exists under `/addons/local`. Duplicate local folders with the same slug can confuse Supervisor update/build resolution.

### WebSocket handshake errors appear after port checks

Plain TCP checks such as `nc -zv` connect to the WebSocket port without sending a WebSocket HTTP upgrade request. This can create harmless `opening handshake failed` log entries.

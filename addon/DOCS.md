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

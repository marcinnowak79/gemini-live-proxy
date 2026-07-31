# Changelog

All notable changes to this add-on are documented here.

## 1.2.0

- Add a plugin system for capabilities that are not smart-home control. Plugins
  live in `/share/asystent_plugins/` and contribute their tools, their prompt
  lines and their query-tool flags at startup, so adding one is a copy plus a
  restart rather than an image rebuild and a release. A plugin that fails to
  import, raises or hangs is logged and skipped; the assistant keeps working
  without it, and tool names are namespaced so a plugin cannot shadow
  `control_device` or any other built-in.
- Blocking plugin code runs in a worker thread, keeping a plain HTTP client out
  of the audio path, and every call is bounded by a timeout below the window
  the voice providers allow for a dispatched tool.
- Add `POST /plugins/<id>/secret/<name>` on the HTTP port for credentials that
  expire and can only be minted elsewhere. Guarded by the new
  `plugin_api_token` option and disabled entirely when it is unset; the plugin
  itself decides whether to accept the pushed value.
- New options: `plugins_dir`, `plugin_timeout_seconds`, `plugin_api_token`.
- Plugins themselves are not part of this repository: they are deployed
  straight to `/share/asystent_plugins/`, which keeps site-specific code and
  credentials out of a public image.

## 1.1.1

- Fix OpenAI commands that were spoken but never executed. The Realtime API
  sends its `function_call` items only after it closes the audio item, and it
  can stay silent for up to ~2 s in between — longer than the 1.2 s post-audio
  idle timeout, so the session was closed while the tool call was still on its
  way. Multi-entity commands ("wyłącz klimatyzację wszędzie") speak longest and
  stalled the most, which is why they failed while single-room commands worked.
  While a response is open, only the general idle timeout now applies.
- A tool call that was already dispatched is no longer cancelled when the
  receive loop exits, so the command still reaches Home Assistant.

## 1.1.0

- Add OpenAI Realtime as an alternative voice backend alongside Gemini Live.
  Both share one system prompt and one tool catalogue; only the model differs.
- Add `ai_provider` to pick the backend, and `ai_provider_entity` to switch it
  live from a Home Assistant dropdown without restarting the add-on.
- Add `openai_api_key`, `openai_model` (default `gpt-realtime-2.1-mini`) and
  `openai_voice` (default `marin`).
- The 16 kHz device stream is resampled to the 24 kHz the OpenAI API requires;
  no firmware or hardware change is needed. Reply audio is 24 kHz for both
  providers, so the existing ESP32 playback path is untouched.
- `search_web` continues to run on Gemini regardless of the selected provider,
  so switching backends does not change what the assistant can look up.

## 1.0.25

- Added input/output audio transcription logging (`HEARD`/`SAID`) to diagnose why a spoken command did or did not trigger an action.
- Lengthened the silence windows so users who wait for the wake confirmation sound before speaking are not cut off: Gemini end-of-turn silence 1100 -> 3000 ms, local mic VAD silence 2400 -> 3500 ms, max stream 7000 -> 8000 ms.

## 1.0.24

- Added `save_input_audio` option: persist incoming wake-word audio to `/share/gemini_in/` (with `share:rw` mount) for false-trigger and command debugging.

## 1.0.23

- Increased the default Gemini session timeout for slower web/search answers.
- Disabled timeout retries by default so a stalled web/search request fails once instead of repeating after the device has already disconnected.
- Exposed session timeout and retry count as add-on options.

## 1.0.22

- Increased the default response audio prebuffer and exposed it as an add-on option to reduce playback stutter.

## 1.0.21

- Removed wake-word sample capture options and capture-only recording support from the main Gemini Live proxy add-on.

## 1.0.20

- Added English UI help text under the Polish locale so Home Assistant installations using Polish can display the same option descriptions.
- Changed network port translations to the Home Assistant documented string format.

## 1.0.19

- Added English Home Assistant add-on UI help text for every configuration option and exposed network port.

## 1.0.12

- Added on-demand Home Assistant state tools for individual devices and room light/switch groups.
- Added persistent multi-timer support with voice-listable and voice-cancellable timers.
- Added delayed timer actions for Home Assistant `media_player.play_media` and `script.turn_on`.
- Play the configured default media on normal timer completion, not only for explicit media timers.
- Keep normal timer alarms ringing until stopped with `stop_timer_alarm`, `cancel_timer`, or `cancel_all`.

## 1.0.11

- Refresh Home Assistant time/date context for every voice session instead of reusing the timestamp from add-on startup.

## 1.0.10

- Detect the Voice PE device area from Home Assistant device registry and mark same-area entities as `local=true`.
- Instruct Gemini to prefer local devices for commands without an explicit room/location.

## 1.0.9

- Replaced peak-only local VAD with RMS-based adaptive noise tracking to better tolerate steady background noise such as fans or 3D printers.
- Lowered the hard microphone stream cap to reduce long no-response turns.

## 1.0.8

- Stop microphone streaming immediately when Gemini emits a tool call, not only when response audio starts.
- Tightened the vacuum tool description so follow-up pronouns for lamps are not routed to the robot vacuum.

## 1.0.7

- Removed a competing WebSocket drain read that could crash the connection handler after a successful command.
- Reduced the default hard session timeout so failed/no-response turns return the LED to idle sooner.
- Store function call arguments in short conversation history so follow-up commands like "turn it off" have the last controlled entity available.

## 1.0.6

- Keep draining ESP32 microphone frames after Gemini starts responding so the device does not hit WebSocket write backpressure and reset the session.

## 1.0.5

- Added receive-side idle timeouts after function calls and response audio so Gemini Live sessions do not hang waiting for a delayed `turn_complete`.
- Verify switch/light states after Home Assistant control calls before reporting success back to Gemini.

## 1.0.4

- Decoupled ESP32 WebSocket audio reads from Gemini streaming backpressure to avoid mid-command disconnects.
- Increased the default local silence timeout to tolerate natural pauses while speaking.

## 1.0.3

- Added local microphone silence detection to end commands faster when Gemini VAD does not close the turn.
- Added Home Assistant service-call logging for function calls and room light groups.
- Return an error when a requested room has no configured light/switch entities instead of reporting a successful no-op.
- Restored the missing async import used by climate service calls.

## 1.0.2

- Added configurable assistant language and response language.
- Added configurable system prompt template.
- Added `ha_exposed_only` to limit Gemini to entities exposed to Home Assistant Assist.
- Moved room aliases and vacuum entity configuration out of source code and into add-on options.
- Changed public defaults and documentation to English.
- Kept per-session HTTP response streams for stable reconnect/retry behavior.

## 1.0.1

- Added per-session response audio URLs.
- Improved ESP32 audio streaming stability.
- Added safer state handling for Voice PE LED behavior.

## 1.0.0

- Initial local add-on version.
- WebSocket bridge from ESP32 Voice PE firmware to Gemini Live.
- HTTP streaming endpoint for response audio playback.

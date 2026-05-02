# Changelog

All notable changes to this add-on are documented here.

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

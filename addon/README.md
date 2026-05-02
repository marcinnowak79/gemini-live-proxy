# Gemini Live Proxy

Gemini Live Proxy bridges custom Home Assistant Voice PE firmware with the Gemini Live API.

This is an experimental first beta, published as inspiration for lower-latency voice assistant flows. It was built to avoid the usual record-then-send-then-wait interaction pattern and make spoken conversations feel more responsive by streaming microphone audio and response audio through a local Home Assistant add-on.

Related repositories:

- [Gemini Live Proxy add-on](https://github.com/marcinnowak79/gemini-live-proxy)
- [Home Assistant Voice PE Gemini firmware](https://github.com/marcinnowak79/home-assistant-voice-pe/tree/gemini-live-proxy)

This is not an official Home Assistant, ESPHome, Nabu Casa, or Google project. It was vibe-coded as a working experiment and should be treated as beta software.

It receives microphone audio from ESPHome over WebSocket, streams it to Gemini Live, executes selected Home Assistant service calls, and streams response audio back to the Voice PE media player.

See [`DOCS.md`](DOCS.md) for installation, configuration and troubleshooting.

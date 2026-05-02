# Security Policy

## Sensitive Data

Do not publish:

- Gemini API keys
- Home Assistant long-lived access tokens
- Home Assistant Supervisor tokens
- ESPHome `secrets.yaml`
- local `.env` files
- Home Assistant `.storage/` contents
- private local IP addresses if you do not want to reveal your network layout

If any secret was committed, pushed or shared, rotate it immediately.

## Entity Exposure

The add-on can expose Home Assistant entity names and aliases to Gemini. Keep `ha_exposed_only` enabled unless you intentionally want Gemini to see all supported entity registry entries.

## Reporting Issues

Before opening a public issue, remove API keys, tokens, local IP addresses and private entity names from logs.

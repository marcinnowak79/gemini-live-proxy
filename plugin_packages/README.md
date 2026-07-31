# Plugins

Capabilities that are not smart-home control and have no business being compiled
into the addon image. Each one is a directory that gets copied to
`/share/asystent_plugins/` on the Home Assistant box; the proxy scans that
directory at startup, merges the plugins' tools into the catalogue and their
prompt lines into the system prompt.

Adding a plugin is a copy plus an addon restart. No image rebuild, no release,
no CI. Credentials live next to the plugin in `/share`, so they survive addon
updates — which the container filesystem does not.

## Writing one

A plugin is a directory containing `plugin.py`:

```python
MANIFEST = {
    "id": "parking",                        # also the mandatory tool-name prefix
    "prompt": "- Parking: ... → parking_status.",
    "tools": [                              # same JSON Schema shape as the built-ins
        {"name": "parking_status", "description": "...", "parameters": {...}},
    ],
    "speak_result": ["parking_status"],     # results the assistant reads out loud
    "timeout_s": 7,                         # optional, default 6, hard ceiling 7.5
}

def call(name, args) -> dict:               # sync or async, both work
    ...

def on_secret(name, value) -> dict:         # optional, see "Rotating credentials"
    ...
```

Rules worth knowing before you write the second one:

- **Tool names must start with `<id>_`.** That is what makes dispatch a dict
  lookup and stops a plugin from shadowing `control_device`. Mis-prefixed tools
  are dropped with a line in the log.
- **`speak_result` is for uncertain outcomes.** By default the assistant
  acknowledges an action before the tool returns; anything whose answer depends
  on the result — a query, or a booking that may find nothing free — must be
  listed here or the assistant will confidently narrate a guess.
- **Blocking code is fine.** A non-async `call` is run in a worker thread, so a
  plain `requests` client will not stall the audio stream.
- **Stay under the timeout.** Both voice providers stop waiting for a dispatched
  tool after roughly 8 s, hence the 7.5 s ceiling. A plugin that performs a
  write should check its own elapsed time before committing, so a slow API
  cannot produce an action the user was told did not happen.
- **Failure is contained.** A plugin that will not import, raises, or hangs is
  logged and skipped. Errors come back to the model as `{"status": "error"}` so
  it can say what went wrong instead of going silent.

## Deploying

```bash
scp -r <plugin-dir> homeassistant:/share/asystent_plugins/
ssh homeassistant "ha addons restart local_gemini_live_proxy"
```

Plugin directories are deliberately not kept in this repository. They tend to
carry site-specific identifiers and credentials, and this repo is public — so
they live outside it and are copied straight to `/share`. This file documents
the contract; the plugins themselves live next to the deployment.

Confirm in the addon log: `[plugins] loaded: parking(2 tools)`.

## Rotating credentials

Plugins whose credentials expire can accept a push instead of polling. Set
`plugin_api_token` in the addon options, then:

```
POST http://<ha-host>:8766/plugins/<plugin_id>/secret/<name>
X-Plugin-Token: <plugin_api_token>
<the secret as the raw body>
```

The core only routes; the plugin's `on_secret` decides. The parking plugin
validates the new cookie against the live API and rolls back to the previous one
if it fails, so a push that arrives at a bad moment cannot break a working
setup. With no token configured the endpoint returns 403 and writes nothing.

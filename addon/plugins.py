"""Runtime-loaded capability plugins.

The built-in catalogue covers the smart home. Everything else — booking the
office parking space, whatever comes after it — has no business being compiled
into the image: it changes on its own schedule, it carries its own credentials,
and baking it in would force a full addon release for a two-line fix. Plugins
live in a directory under /share instead, so adding one is a copy plus a
restart, and their credentials survive an addon update.

A plugin is a directory containing plugin.py that exports:

    MANIFEST = {
        "id": "parking",                    # also the mandatory tool-name prefix
        "prompt": "- ...",                  # lines appended to the system prompt
        "tools": [ {...JSON Schema...} ],   # same shape as build_tool_specs()
        "speak_result": ["parking_status"], # results the assistant reads out loud
        "timeout_s": 7,                     # optional, defaults to 6
    }

    def call(name, args) -> dict            # sync or async, both work
    def on_secret(name, value) -> dict      # optional, see proxy_server

Failure is always local. A plugin that will not import, declares a malformed
manifest, raises, or hangs gets logged and skipped; the assistant keeps working
without it. That is the whole point of the boundary — a broken parking cookie
must never cost you the lights.
"""
import asyncio
import importlib.util
import inspect
import os
import sys
from pathlib import Path

def _cfg(name: str, default: str) -> str:
    """bashio hands unset add-on options through as the literal string "null"."""
    value = os.getenv(name, "").strip()
    return default if not value or value.lower() == "null" else value


PLUGINS_DIR = Path(_cfg("PLUGINS_DIR", "/share/asystent_plugins"))
try:
    DEFAULT_TIMEOUT_S = float(_cfg("PLUGIN_TIMEOUT_SECONDS", "6"))
except ValueError:
    DEFAULT_TIMEOUT_S = 6.0

# Both providers give a dispatched tool call roughly 8 s to finish before they
# stop waiting for it (RECEIVE_IDLE_TIMEOUT_GENERAL in openai_session). A plugin
# that outlives that window produces a silent assistant, which is worse than an
# error it can narrate, so the manifest cannot ask for more than this.
TIMEOUT_CEILING_S = 7.5


class Plugin:
    """One loaded plugin: its module, its validated manifest, its tools."""

    def __init__(self, plugin_id: str, module, manifest: dict, tools: list[dict]):
        self.id = plugin_id
        self.module = module
        self.manifest = manifest
        self.tools = tools
        self.timeout_s = min(
            float(manifest.get("timeout_s") or DEFAULT_TIMEOUT_S), TIMEOUT_CEILING_S
        )

    async def invoke(self, name: str, args: dict) -> dict:
        """Call the plugin, keeping synchronous plugins off the event loop.

        Most plugins wrap a plain blocking HTTP client. Running that inline would
        stall the audio stream for the duration of the request, so anything that
        is not a coroutine goes to a worker thread.
        """
        handler = self.module.call
        if inspect.iscoroutinefunction(handler):
            return await handler(name, args)
        return await asyncio.to_thread(handler, name, args)

    async def deliver_secret(self, name: str, value: str) -> dict:
        handler = getattr(self.module, "on_secret", None)
        if handler is None:
            return {"status": "error", "message": f"plugin {self.id} accepts no secrets"}
        if inspect.iscoroutinefunction(handler):
            return await handler(name, value)
        return await asyncio.to_thread(handler, name, value)


class PluginRegistry:
    """Holds the loaded plugins and answers the questions the core asks."""

    def __init__(self):
        self.plugins: dict[str, Plugin] = {}
        self._routes: dict[str, Plugin] = {}

    # -- loading ---------------------------------------------------------

    def load(self, directory: Path | None = None) -> None:
        """Scan the plugins directory. Safe to call again to pick up changes."""
        root = Path(directory) if directory else PLUGINS_DIR
        self.plugins.clear()
        self._routes.clear()

        if not root.is_dir():
            print(f"[plugins] no plugin directory at {root}, none loaded", flush=True)
            return

        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            if not (entry / "plugin.py").is_file():
                continue
            try:
                self._load_one(entry)
            except Exception as err:  # noqa: BLE001 - one bad plugin must not stop the rest
                print(f"[plugins] {entry.name}: FAILED to load: {err}", flush=True)

        if self.plugins:
            summary = ", ".join(
                f"{p.id}({len(p.tools)} tools)" for p in self.plugins.values()
            )
            print(f"[plugins] loaded: {summary}", flush=True)

    def _load_one(self, directory: Path) -> None:
        plugin_file = directory / "plugin.py"
        module_name = f"asystent_plugin_{directory.name}"

        # The plugin's own directory goes on sys.path so it can import whatever
        # it vendors alongside itself without packaging ceremony.
        dir_str = str(directory)
        if dir_str not in sys.path:
            sys.path.insert(0, dir_str)

        spec = importlib.util.spec_from_file_location(module_name, plugin_file)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot build import spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        manifest = getattr(module, "MANIFEST", None)
        if not isinstance(manifest, dict):
            raise RuntimeError("MANIFEST missing or not a dict")
        if not callable(getattr(module, "call", None)):
            raise RuntimeError("no call(name, args) function")

        plugin_id = str(manifest.get("id") or directory.name).strip()
        if not plugin_id:
            raise RuntimeError("empty manifest id")
        if plugin_id in self.plugins:
            raise RuntimeError(f"duplicate plugin id {plugin_id}")

        tools = self._validate_tools(plugin_id, manifest.get("tools") or [])
        if not tools:
            raise RuntimeError("no usable tools declared")

        plugin = Plugin(plugin_id, module, manifest, tools)
        self.plugins[plugin_id] = plugin
        for tool in tools:
            self._routes[tool["name"]] = plugin

    def _validate_tools(self, plugin_id: str, tools) -> list[dict]:
        """Keep the well-formed tools, drop the rest with a reason in the log.

        Tool names carry the plugin id as a prefix. That is what makes routing a
        dict lookup instead of a guess, and it keeps a plugin from shadowing
        control_device or any other built-in.
        """
        prefix = f"{plugin_id}_"
        usable: list[dict] = []
        for tool in tools if isinstance(tools, list) else []:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "")
            if not name.startswith(prefix):
                print(
                    f"[plugins] {plugin_id}: skipping tool {name!r}, "
                    f"name must start with {prefix!r}",
                    flush=True,
                )
                continue
            if name in self._routes:
                print(f"[plugins] {plugin_id}: skipping duplicate tool {name!r}", flush=True)
                continue
            if "parameters" not in tool:
                tool = {**tool, "parameters": {"type": "object", "properties": {}}}
            usable.append(tool)
        return usable

    # -- what the core asks for ------------------------------------------

    def tool_specs(self) -> list[dict]:
        return [tool for plugin in self.plugins.values() for tool in plugin.tools]

    def query_tools(self) -> set[str]:
        """Tools whose result is the answer the user is waiting to hear."""
        names = set()
        for plugin in self.plugins.values():
            declared = plugin.manifest.get("speak_result") or []
            own = {tool["name"] for tool in plugin.tools}
            names.update(name for name in declared if name in own)
        return names

    def prompt_block(self) -> str:
        """The prompt fragment every plugin contributes, or nothing at all."""
        lines = [
            str(plugin.manifest.get("prompt") or "").strip()
            for plugin in self.plugins.values()
        ]
        lines = [line for line in lines if line]
        if not lines:
            return ""
        return "\n=== ADDITIONAL CAPABILITIES ===\n" + "\n".join(lines) + \
               "\n=== END ADDITIONAL CAPABILITIES ===\n"

    def owns(self, name: str) -> bool:
        return name in self._routes

    def get(self, plugin_id: str) -> Plugin | None:
        return self.plugins.get(plugin_id)

    # -- dispatch --------------------------------------------------------

    async def call(self, name: str, args: dict) -> dict:
        """Run a plugin tool. Every failure comes back as data, never an exception.

        The model is far better at saying "the parking system did not answer"
        than the proxy is at recovering from a plugin that threw mid-session.
        """
        plugin = self._routes.get(name)
        if plugin is None:
            return {"status": "error", "message": f"Unknown plugin tool: {name}"}
        try:
            return await asyncio.wait_for(
                plugin.invoke(name, args or {}), timeout=plugin.timeout_s
            )
        except asyncio.TimeoutError:
            print(f"[plugins] {plugin.id}: {name} timed out after {plugin.timeout_s}s", flush=True)
            return {
                "status": "error",
                "message": f"{plugin.id} did not answer in time",
            }
        except Exception as err:  # noqa: BLE001 - surfaced to the model as a tool result
            print(f"[plugins] {plugin.id}: {name} raised: {err}", flush=True)
            return {"status": "error", "message": str(err)}


registry = PluginRegistry()

"""Home Assistant REST API client - reads entities and executes actions."""

import asyncio
import json
import os

import aiohttp

HA_URL = os.getenv("HA_URL", "http://supervisor/core")
HA_TOKEN = os.getenv("HA_TOKEN", os.getenv("SUPERVISOR_TOKEN", ""))
ENTITY_REGISTRY_PATH = os.getenv("HA_ENTITY_REGISTRY_PATH", "/config/.storage/core.entity_registry")
DEVICE_REGISTRY_PATH = os.getenv("HA_DEVICE_REGISTRY_PATH", "/config/.storage/core.device_registry")
EXPOSED_ONLY = os.getenv("HA_EXPOSED_ONLY", "true").lower() not in ("0", "false", "no")
VACUUM_ENTITY_ID = os.getenv("VACUUM_ENTITY_ID", "").strip()
LOCAL_AREA_ID = os.getenv("LOCAL_AREA_ID", "").strip()

DEFAULT_ACTIONABLE_DOMAINS = {"switch", "light", "climate", "scene", "script", "vacuum", "media_player"}
ACTIONABLE_DOMAINS = {
    item.strip()
    for item in os.getenv("HA_ACTIONABLE_DOMAINS", ",".join(sorted(DEFAULT_ACTIONABLE_DOMAINS))).split(",")
    if item.strip()
}


def _load_room_aliases() -> dict[str, str]:
    raw = os.getenv("ROOM_ALIASES_JSON", "{}").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        print(f"[ha] Invalid ROOM_ALIASES_JSON: {err}", flush=True)
        return {}
    if not isinstance(data, dict):
        print("[ha] ROOM_ALIASES_JSON must be a JSON object", flush=True)
        return {}
    return {str(prefix): str(room) for prefix, room in data.items()}


PREFIX_TO_ROOM = _load_room_aliases()

HEADERS = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def is_vacuum_enabled() -> bool:
    return bool(VACUUM_ENTITY_ID)


def _load_device_areas() -> tuple[dict[str, str], str]:
    """Return device_id->area_id and the detected local Voice PE area."""
    try:
        with open(DEVICE_REGISTRY_PATH) as f:
            data = json.load(f)
    except Exception as err:
        print(f"[ha] Could not read device registry: {err}", flush=True)
        return {}, LOCAL_AREA_ID

    device_areas = {}
    detected_local_area = LOCAL_AREA_ID
    for device in data.get("data", {}).get("devices", []):
        device_id = device.get("id", "")
        area_id = device.get("area_id") or ""
        if device_id and area_id:
            device_areas[device_id] = area_id

        if detected_local_area:
            continue
        name = " ".join(str(device.get(key) or "") for key in ("name_by_user", "name", "model", "manufacturer"))
        if "home assistant voice" in name.lower() or "voice pe" in name.lower():
            detected_local_area = area_id

    return device_areas, detected_local_area


async def get_exposed_entities() -> tuple[str, dict[str, list[str]], str]:
    """Fetch exposed entities from HA. Returns (entity_list_text, room_lights_map)."""
    with open(ENTITY_REGISTRY_PATH) as f:
        data = json.load(f)

    device_areas, local_area_id = _load_device_areas()
    entities = data["data"]["entities"]
    lines = []
    room_lights = {}

    for e in sorted(entities, key=lambda x: x.get("entity_id", "")):
        opts = e.get("options", {}).get("conversation", {})
        if EXPOSED_ONLY and not opts.get("should_expose", False):
            continue
        eid = e.get("entity_id", "")
        domain = eid.split(".")[0]
        if domain not in ACTIONABLE_DOMAINS:
            continue

        aliases = e.get("aliases", [])
        name = e.get("name") or e.get("original_name") or ""
        label = ", ".join(aliases) if aliases else name
        area_id = e.get("area_id") or device_areas.get(e.get("device_id", ""), "")
        details = []
        if label:
            details.append(label)
        if area_id:
            details.append(f"area={area_id}")
        if local_area_id and area_id == local_area_id:
            details.append("local=true")
        lines.append(f"- {eid} ({', '.join(details)})")

        # Build room groups from HA area_id first, then optional user aliases.
        if domain in ("switch", "light"):
            if area_id:
                room_lights.setdefault(area_id, []).append(eid)
                continue
            name_part = eid.split(".", 1)[1] if "." in eid else ""
            for prefix, room in PREFIX_TO_ROOM.items():
                if name_part.startswith(prefix):
                    room_lights.setdefault(room, []).append(eid)
                    break

    room_summary = {room: len(entities) for room, entities in sorted(room_lights.items())}
    print(
        f"[ha] Loaded {len(lines)} actionable entities; local_area={local_area_id or 'none'}; room light groups: {room_summary}",
        flush=True,
    )
    return "\n".join(lines), room_lights, local_area_id


async def get_ha_context() -> str:
    """Get time, timezone, location from HA config."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{HA_URL}/api/config", headers=HEADERS) as resp:
                config = await resp.json()

        from datetime import datetime
        import zoneinfo
        tz = zoneinfo.ZoneInfo(config["time_zone"])
        now = datetime.now(tz)
        time_str = now.strftime("%A, %d %B %Y, %H:%M")
        lat = config.get("latitude", 0)
        lon = config.get("longitude", 0)
        return f"\nAktualny czas: {time_str}\nStrefa: {config['time_zone']}\nWspółrzędne: {lat:.2f}, {lon:.2f}\n"
    except Exception:
        return ""


async def call_ha_service(domain: str, service: str, data: dict) -> dict:
    """Call HA service."""
    url = f"{HA_URL}/api/services/{domain}/{service}"
    print(f"[ha] Calling {domain}.{service}: {data}", flush=True)
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=data, headers=HEADERS) as resp:
            if resp.status == 200:
                print(f"[ha] {domain}.{service} OK", flush=True)
                return {"status": "ok"}
            text = await resp.text()
            print(f"[ha] {domain}.{service} ERROR HTTP {resp.status}: {text}", flush=True)
            return {"status": "error", "message": f"HTTP {resp.status}: {text}"}


async def get_entity_state(entity_id: str) -> str | None:
    """Read a single HA entity state."""
    url = f"{HA_URL}/api/states/{entity_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=HEADERS) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"[ha] State read failed for {entity_id}: HTTP {resp.status}: {text}", flush=True)
                return None
            data = await resp.json()
            return data.get("state")


async def verify_entity_states(entity_ids: list[str], action: str) -> dict:
    """Verify HA state after a switch/light action."""
    if action not in ("turn_on", "turn_off"):
        states = {entity_id: await get_entity_state(entity_id) for entity_id in entity_ids}
        print(f"[ha] Post-action states for {action}: {states}", flush=True)
        return {"verified": True, "states": states}

    expected = "on" if action == "turn_on" else "off"
    for delay in (0.3, 0.7, 1.2):
        await asyncio.sleep(delay)
        states = {entity_id: await get_entity_state(entity_id) for entity_id in entity_ids}
        mismatched = {
            entity_id: state
            for entity_id, state in states.items()
            if state is not None and state != expected
        }
        if not mismatched:
            print(f"[ha] Verified {action}: {states}", flush=True)
            return {"verified": True, "expected": expected, "states": states}

    print(f"[ha] Verification failed for {action}: expected={expected}, states={states}", flush=True)
    return {
        "verified": False,
        "expected": expected,
        "states": states,
        "message": f"Home Assistant accepted the service call, but not all entities reached state {expected}",
    }


async def call_and_verify_ha_service(action: str, entity_ids: str | list[str]) -> dict:
    """Call homeassistant action and verify final state where possible."""
    normalized = entity_ids if isinstance(entity_ids, list) else [entity_ids]
    result = await call_ha_service("homeassistant", action, {"entity_id": entity_ids})
    if result.get("status") != "ok":
        return result

    verification = await verify_entity_states(normalized, action)
    if not verification.get("verified", False):
        return {"status": "error", **verification}
    return {"status": "ok", **verification}


async def execute_function(name: str, args: dict, room_lights: dict) -> dict:
    """Execute a Gemini function call against HA."""
    print(f"[ha] Function {name}: {args}", flush=True)
    if name == "control_device":
        return await call_and_verify_ha_service(args["action"], args["entity_id"])

    elif name == "control_room":
        entities = room_lights.get(args["room"], [])
        if entities:
            print(f"[ha] control_room room={args['room']} entities={entities}", flush=True)
            return await call_and_verify_ha_service(args["action"], entities)
        available_rooms = sorted(room_lights.keys())
        print(
            f"[ha] control_room no entities for room={args['room']}; available={available_rooms}",
            flush=True,
        )
        return {
            "status": "error",
            "message": f"No light/switch entities configured for room {args['room']}",
            "available_rooms": available_rooms,
        }

    elif name == "activate_scene":
        return await call_ha_service("scene", "turn_on", {"entity_id": args["scene_id"]})

    elif name == "run_script":
        return await call_ha_service("script", "turn_on", {"entity_id": args["script_id"]})

    elif name == "set_climate":
        entity_id = args["entity_id"]
        tasks = []
        if "hvac_mode" in args:
            tasks.append(call_ha_service("climate", "set_hvac_mode",
                                         {"entity_id": entity_id, "hvac_mode": args["hvac_mode"]}))
        if "temperature" in args:
            tasks.append(call_ha_service("climate", "set_temperature",
                                         {"entity_id": entity_id, "temperature": args["temperature"]}))
        if tasks:
            await asyncio.gather(*tasks)
        return {"status": "ok"}

    elif name == "control_vacuum":
        if not VACUUM_ENTITY_ID:
            return {"status": "error", "message": "vacuum_entity_id is not configured"}
        return await call_ha_service("vacuum", args["action"],
                                     {"entity_id": VACUUM_ENTITY_ID})

    return {"status": "error", "message": f"Unknown: {name}"}

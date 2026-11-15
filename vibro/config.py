import json
import math
import re
from pathlib import Path

def read_json(path):
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)

def number(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name}: expected finite number >= {minimum}")
    return value

def load_settings(path):
    data = read_json(path)
    for name in ("preview", "feedback"):
        pattern = data["haptics"][name]
        for field in ("on_ms", "off_ms"):
            value = pattern[field]
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 60000:
                raise ValueError(f"haptics.{name}.{field}: integer 1..60000 required")
    count = data["haptics"]["preview"]["count"]
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
        raise ValueError("preview.count: integer 1..100 required")
    ble = data["ble"]
    number(ble["command_timeout_s"], "command_timeout_s", 0.1)
    number(ble["heartbeat_s"], "heartbeat_s", 0.1)
    if ble["heartbeat_s"] >= 2:
        raise ValueError("heartbeat_s must be < 2 (firmware watchdog is 4 seconds)")
    led = data["led"]
    for key in ("on_ms", "off_ms"):
        number(led[key], f"led.{key}", 1)
    count = led["count"]
    if not isinstance(count, int) or count < 2:
        raise ValueError("led.count must be >= 2")
    indices = led["left_indices"] + led["right_indices"]
    if not led["left_indices"] or not led["right_indices"] or len(indices) != len(set(indices)):
        raise ValueError("LED sides must be nonempty and disjoint")
    if any(type(i) is not int or not 0 <= i < count for i in indices):
        raise ValueError("LED index outside configured strip")
    if len(led["color"]) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in led["color"]):
        raise ValueError("LED color must contain 3 bytes")
    number(data["max_dispatch_lag_s"], "max_dispatch_lag_s", 0.01)
    return data

def validate_addresses(settings):
    addresses = [settings["ble"][f"{side}_mac"] for side in ("left", "right")]
    if any(not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", x) for x in addresses):
        raise ValueError("Set both glove BLE MAC addresses in settings.json")
    if addresses[0].lower() == addresses[1].lower():
        raise ValueError("Left and right MAC addresses must differ")
    return addresses

def load_route(path, allow_draft=False):
    data = read_json(path)
    if not isinstance(data.get("route_id"), str) or not data["route_id"]:
        raise ValueError("route_id is required")
    if not allow_draft and data.get("ready") is not True:
        raise ValueError("Route timestamps are not finalized (ready=false). Use demo.json for simulation.")
    events = data.get("events", [])
    if not events:
        raise ValueError("Route needs timestamped events; real route templates are intentionally empty")
    last_pass = -1
    ids = set()
    for event in events:
        if not isinstance(event.get("id"), str) or not event["id"] or event["id"] in ids:
            raise ValueError("Event IDs must be nonempty and unique")
        ids.add(event["id"])
        if event["direction"] not in ("left", "right"):
            raise ValueError("direction must be left or right")
        if event["expected_action"] not in ("turn", "straight"):
            raise ValueError("expected_action must be turn or straight")
        pre, auto, end = [number(event[k], k) for k in ("preview_s", "auto_on_s", "pass_s")]
        if not last_pass < pre < auto < end:
            raise ValueError("Require previous pass < preview < auto_on < pass")
        cue = event.get("cue_s")
        if event["expected_action"] == "straight" and cue is None:
            raise ValueError("Straight scenarios need cue_s")
        if cue is not None:
            number(cue, "cue_s")
            if cue > pre:
                raise ValueError("Video cue must appear by preview_s for the agreed 'never turn on' criterion")
        last_pass = end
    duration = number(data["duration_s"], "duration_s")
    if duration < last_pass:
        raise ValueError("duration_s precedes last pass")
    return data

def timeline(route):
    items = []
    for event in route["events"]:
        if event.get("cue_s") is not None:
            items.append((event["cue_s"], 0, "cue", event))
        items.extend([(event["preview_s"], 1, "preview", event),
                      (event["auto_on_s"], 2, "auto", event),
                      (event["pass_s"], 3, "pass", event)])
    items.append((route["duration_s"], 4, "end", None))
    return sorted(items, key=lambda x: (x[0], x[1]))

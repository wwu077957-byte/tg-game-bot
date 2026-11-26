from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any


DEFAULT_SCENES = {
    "welcome": "你好 {name}，欢迎你！我是{product}这边的，看到消息后尽快回复。",
    "night": "你好 {name}，我已经休息了，明早 {time} 前回复你。",
    "busy": "你好 {name}，我正在处理事情，看到消息后尽快回复。",
    "sales": "你好 {name}，售后问题可以直接发给我，我看到后尽快处理。",
    "offline": "你好 {name}，收到你的消息了，我稍后回复。",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "idle_minutes": 15,
    "queue_threshold": 3,
    "start_hour": 23,
    "end_hour": 9,
    "tz_offset": 8,
    "product": "",
    "simple_reply": "",
    "scene_mode": "auto",
    "scenes": DEFAULT_SCENES,
    "cooldown_seconds": 1800,
    "daily_cap": 3,
}

SCENE_LABELS = {
    "welcome": "首次接触/欢迎语",
    "night": "夜间",
    "busy": "忙碌",
    "sales": "成交/售后",
    "offline": "其他离线",
}

SCENE_LABELS_EN = {
    "welcome": "First contact/welcome",
    "night": "Nighttime",
    "busy": "Busy",
    "sales": "Sales/after-sales",
    "offline": "Other offline",
}


def scene_labels(language: str = "zh") -> dict[str, str]:
    """Return fixed UI labels for the selected language, never reply templates."""
    return SCENE_LABELS_EN if language == "en" else SCENE_LABELS

_ALLOWED_SCENES = set(DEFAULT_SCENES)
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def default_config() -> dict[str, Any]:
    return {**DEFAULT_CONFIG, "scenes": dict(DEFAULT_SCENES)}


def normalize_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Merge saved settings over defaults and clamp values to safe ranges."""
    result = default_config()
    if isinstance(config, dict):
        result.update({key: value for key, value in config.items() if key in DEFAULT_CONFIG and key != "scenes"})
        scenes = config.get("scenes")
        if isinstance(scenes, dict):
            for key, value in scenes.items():
                if key in _ALLOWED_SCENES and isinstance(value, str):
                    result["scenes"][key] = value[:1000]
    result["idle_minutes"] = _clamp_int(result.get("idle_minutes"), 15, 1, 1440)
    result["queue_threshold"] = _clamp_int(result.get("queue_threshold"), 3, 1, 100)
    result["start_hour"] = _clamp_int(result.get("start_hour"), 23, 0, 23)
    result["end_hour"] = _clamp_int(result.get("end_hour"), 9, 0, 23)
    result["tz_offset"] = _clamp_int(result.get("tz_offset"), 8, -12, 14)
    result["product"] = str(result.get("product") or "")[:120]
    result["simple_reply"] = str(result.get("simple_reply") or "")[:1000]
    result["scene_mode"] = result.get("scene_mode") if result.get("scene_mode") in {"auto", "sales"} else "auto"
    result["cooldown_seconds"] = _clamp_int(result.get("cooldown_seconds"), 1800, 60, 86400)
    result["daily_cap"] = _clamp_int(result.get("daily_cap"), 3, 1, 20)
    return result


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def in_time_window(hour: int, start_hour: int, end_hour: int) -> bool:
    """Check a local-hour window; start==end means the window is disabled."""
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def local_datetime(now: float, tz_offset: int) -> datetime:
    return datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=tz_offset)


def is_night(now: float, config: dict[str, Any]) -> bool:
    local = local_datetime(now, int(config["tz_offset"]))
    return in_time_window(local.hour, int(config["start_hour"]), int(config["end_hour"]))


def offline_reason(
    config: dict[str, Any],
    *,
    enabled: bool,
    manual_auto: bool,
    last_human_at: float,
    paused_until: float,
    pending_count: int,
    now: float | None = None,
) -> str | None:
    """Return the first active reason; time, idle, queue, and manual are OR triggers.

    A recent human takeover suppresses every trigger until the configured idle interval
    has elapsed, ensuring the account cannot race the owner while they are active.
    """
    current = time.time() if now is None else now
    if not enabled or paused_until > current:
        return None
    if manual_auto:
        return "manual"
    if is_night(current, config):
        return "schedule"
    if pending_count > int(config["queue_threshold"]):
        return "queue"
    if last_human_at and current - last_human_at >= int(config["idle_minutes"]) * 60:
        return "idle"
    return None


def choose_scene(
    config: dict[str, Any], *, first_contact: bool, manual_auto: bool = False, now: float | None = None
) -> str:
    current = time.time() if now is None else now
    if first_contact:
        return "welcome"
    if is_night(current, config):
        return "night"
    if config.get("scene_mode") == "sales":
        return "sales"
    if manual_auto:
        return "offline"
    return "busy"


def render_template(template: str, *, name: str, now: float, config: dict[str, Any]) -> str:
    local = local_datetime(now, int(config["tz_offset"]))
    values = {
        "name": name.strip() or "你好",
        "time": f"{local.hour:02d}:{local.minute:02d}",
        "product": str(config.get("product") or ""),
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    # Don't leak misspelled/unsupported placeholders into the recipient's chat.
    rendered = _PLACEHOLDER.sub("", rendered)
    return rendered.strip()[:4096]

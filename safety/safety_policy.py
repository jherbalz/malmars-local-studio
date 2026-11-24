"""Content-screening policy for Malmars Local Studio.

The gate runs on this PC only: a pinned CPU image classifier plus a prompt
blocklist. It is fail-closed by default, because a screen that silently fails
open is worse than no screen at all.

The user can turn the gate off at their own discretion. That switch lives in
the Studio's own configuration file and behind the Advanced section of the
setup page - never on a front-facing dashboard, so it cannot be switched off
by accident by someone who does not know what they are changing.

Two levels of control:

- "image_safety.enabled" - the master switch. False means no image and no
  prompt is screened. This is the user-discretion setting.
- "image_safety.mode" - what happens when screening is on but the local model
  cannot be loaded. "block" fails closed (the default). "warn" lets the
  request through and records the fact. "warn" exists so a customer without
  the model downloaded is not completely stuck.

The effective policy is resolved at request time from the Studio own
license_config.json. Nothing is cached at import, so editing the file and
reloading a page takes effect immediately.
"""

import json
import os
from pathlib import Path

# Modes are checked in order; anything unknown is treated as "block".
MODES = ("block", "warn")

DEFAULT_THRESHOLD = 0.20
DEFAULT_MODEL_ID = "Falconsai/nsfw_image_detection"


def _config_path(root=None):
    """The Studio own license_config.json, wherever the Studio lives."""
    if root:
        return Path(root) / "license_config.json"
    configured = os.environ.get("JL_STUDIO_ROOT")
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path(__file__).resolve().parent)
    for candidate in candidates:
        path = candidate / "license_config.json"
        if path.is_file():
            return path
    return Path(__file__).resolve().parent / "license_config.json"


def _read_config(root=None):
    try:
        raw = _config_path(root).read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def policy(root=None):
    """The effective content-screening policy for this Studio.

    Returns a dict with "enabled", "mode", "threshold" and "model".
    Unknown or missing configuration falls back to the safe default: screening
    on, fail closed.
    """
    section = _read_config(root).get("image_safety")
    section = section if isinstance(section, dict) else {}

    enabled = section.get("enabled")
    if not isinstance(enabled, bool):
        enabled = True

    # A LOCKED screen cannot be switched off - not by the Advanced section,
    # not by anything else. That is the school setting, where an adult
    # pins it ON so a student cannot pass --no-screen. It is OFF by
    # default, because the adult who owns the machine is always allowed to
    # turn the screen off.
    locked = section.get("locked")
    if not isinstance(locked, bool):
        locked = False
    if locked:
        enabled = True

    mode = section.get("mode")
    if mode not in MODES:
        mode = "block"

    threshold = section.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        threshold = DEFAULT_THRESHOLD
    threshold = float(threshold)
    if not 0.0 < threshold < 1.0:
        threshold = DEFAULT_THRESHOLD

    model = section.get("model")
    if not isinstance(model, str) or not model.strip():
        model = DEFAULT_MODEL_ID

    return {
        "enabled": enabled,
        "locked": locked,
        "mode": mode,
        "threshold": threshold,
        "model": model.strip(),
    }


def screening_enabled(root=None):
    """True when the user has screening switched on."""
    return bool(policy(root)["enabled"])


def fail_closed(root=None):
    """True when a screening failure must block the request.

    Only meaningful while screening is enabled. A user who turned screening
    off has already made their choice, so a missing model is not an error.
    """
    current = policy(root)
    if not current["enabled"]:
        return False
    return current["mode"] == "block"


def describe(root=None):
    """A short, safe-to-show summary for the setup page."""
    current = policy(root)
    return {
        "enabled": current["enabled"],
        "locked": current.get("locked", False),
        "mode": current["mode"],
        "threshold": current["threshold"],
        "model": current["model"],
        "config_file": str(_config_path(root)),
    }


def update(root=None, updates=None):
    """Write the user's own screening preference back into the Studio config.

    Only the keys inside "image_safety" are touched. Everything else in the
    file - including the active tier - is written back exactly as it was
    read, so this can never change what an edition is allowed to run.
    """
    updates = updates if isinstance(updates, dict) else {}
    path = _config_path(root)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        raw = "{}"
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    section = data.get("image_safety")
    if not isinstance(section, dict):
        section = {}

    if "enabled" in updates:
        enabled = updates["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        if not enabled and section.get("locked") is True:
            raise ValueError(
                "The content screen is locked on. Set \"locked\" to false "
                "in this file first.")
        section["enabled"] = enabled
    if "mode" in updates:
        mode = updates["mode"]
        if mode not in MODES:
            raise ValueError("mode must be one of: %s" % ", ".join(MODES))
        section["mode"] = mode
    if "threshold" in updates:
        value = updates["threshold"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("threshold must be a number")
        value = float(value)
        if not 0.0 < value < 1.0:
            raise ValueError("threshold must be between 0 and 1")
        section["threshold"] = value

    data["image_safety"] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "policy": describe(root)}

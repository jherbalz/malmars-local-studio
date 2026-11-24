"""Fail-closed client for the local Malmars Local safety service."""

import http.client
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

def _find_studio_root(start):
    """The folder that holds this Studio's own license_config.json.

    In the workspace the client sits in apps/, one level under the Studio
    root. In an installed edition it sits at the root itself. Walking up to
    the folder that holds the configuration file covers both layouts, so the
    customer's own on/off choice is honoured wherever the tree is installed.
    """
    for candidate in [start, *start.parents]:
        if (candidate / "license_config.json").is_file():
            return candidate
    return start


STUDIO_ROOT = _find_studio_root(Path(__file__).resolve().parent)
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))
import safety_policy


class SafetyError(RuntimeError):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


MAX_IMAGE_BYTES = 64 * 1024 * 1024


def _post(path, body, content_type):
    url = urlparse(os.environ.get("JL_SAFETY_URL", "http://127.0.0.1:8788"))
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost"):
        raise SafetyError("Local safety service URL is not trusted.")
    connection = http.client.HTTPConnection(url.hostname, url.port or 8788, timeout=75)
    try:
        connection.request("POST", path, body=body, headers={
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Host": "%s:%d" % (url.hostname, url.port or 8788),
        })
        response = connection.getresponse()
        data = response.read(64 * 1024)
        try:
            result = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            result = {}
        if response.status != 200:
            raise SafetyError(result.get("error", "Local safety screening unavailable."),
                              response.status)
        if not isinstance(result, dict) or result.get("allowed") is not True:
            raise SafetyError("Content blocked by the local safety policy.", 422)
        return result
    except SafetyError:
        raise
    except OSError as exc:
        raise SafetyError("Local safety screening is unavailable; request was blocked.") from exc
    finally:
        connection.close()


def check_prompt(text):
    if not safety_policy.screening_enabled(STUDIO_ROOT):
        # The user turned the local content gate off in the Studio settings.
        return {"allowed": True, "screened": False}
    return _post("/v1/check-prompt", json.dumps({"text": text}).encode("utf-8"),
                 "application/json")


def check_image_bytes(data, content_type):
    if not safety_policy.screening_enabled(STUDIO_ROOT):
        # The user turned the local content gate off in the Studio settings.
        return {"allowed": True, "screened": False}
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise SafetyError("Image is empty.", 415)
    if len(data) > MAX_IMAGE_BYTES:
        raise SafetyError("Image exceeds the 64 MiB safety-screening limit.", 413)
    return _post("/v1/check-image", data, content_type)


def check_image_file(path, content_type=None):
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SafetyError("Image file is unavailable for screening.", 400) from exc
    if size <= 0:
        raise SafetyError("Image is empty.", 415)
    if size > MAX_IMAGE_BYTES:
        raise SafetyError("Image exceeds the 64 MiB safety-screening limit.", 413)
    content_type = content_type or {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }.get(path.suffix.lower())
    if not content_type:
        raise SafetyError("Unsupported image type.", 415)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SafetyError("Image file could not be read for screening.", 400) from exc
    return check_image_bytes(data, content_type)

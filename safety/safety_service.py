"""Loopback-only, CPU image and prompt screening for Malmars Local Studio."""

from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlparse

import safety_policy

ROOT = Path(__file__).resolve().parent
MODEL_ID = "Falconsai/nsfw_image_detection"
MODEL_REVISION = "96cb0d0342c7afb80cab76ecc58b265fa44da256"
MODEL_BYTES = 343_223_968
MODEL_SHA256 = "97b2ce64ec146884b37f98ee7944ca4891aa72f6827dc0cb10684a1cbecd5830"
NSFW_THRESHOLD = 0.20
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}

PROMPT_BLOCKLIST = (
    re.compile(
        r"\b(?:porn(?:ography|ographic)?|hentai|erotic|sex(?:ual(?:ly)?)?|"
        r"nude|naked|nudity|masturbat\w*|orgasm\w*|genitals?|penis|vagina|"
        r"vulva|fetish|explicit\s+(?:sexual|adult))\b", re.I
    ),
    re.compile(
        r"\b(?:graphic\s+violence|gore|dismember\w*|behead\w*|torture|"
        r"mutilat\w*|self[- ]harm|suicid\w*)\b", re.I
    ),
)

_MODEL = None
_MODEL_LOCK = threading.Lock()
_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_LIMIT = 256


class SafetyError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def hf_home():
    return Path(os.environ.get("HF_HOME") or ROOT / "models" / "hf_cache").resolve()


def snapshot_path():
    return (hf_home() / "hub" / "models--Falconsai--nsfw_image_detection"
            / "snapshots" / MODEL_REVISION)


def model_status():
    folder = snapshot_path()
    weights = folder / "model.safetensors"
    if not folder.is_dir() or not weights.is_file():
        return {"installed": False, "revision": MODEL_REVISION}
    try:
        if weights.stat().st_size != MODEL_BYTES:
            return {"installed": False, "revision": MODEL_REVISION, "reason": "size"}
        digest = hashlib.sha256()
        with weights.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            return {"installed": False, "revision": MODEL_REVISION, "reason": "hash"}
        required = ("config.json", "preprocessor_config.json")
        if not all((folder / name).is_file() for name in required):
            return {"installed": False, "revision": MODEL_REVISION, "reason": "files"}
        return {"installed": True, "revision": MODEL_REVISION, "model": MODEL_ID}
    except OSError:
        return {"installed": False, "revision": MODEL_REVISION, "reason": "unreadable"}


def check_prompt(text):
    if not isinstance(text, str) or len(text) > 24_000:
        raise SafetyError("Prompt text is invalid or too long.", 400)
    current = safety_policy.policy(ROOT)
    if not current["enabled"]:
        # The user turned the local content gate off in the Studio settings.
        return {"allowed": True, "screened": False}
    for pattern in PROMPT_BLOCKLIST:
        if pattern.search(text):
            raise SafetyError("Prompt blocked by the local content policy.", 422)
    return {"allowed": True, "screened": True}


def _load_model():
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        if not model_status()["installed"]:
            raise SafetyError("Local safety model is missing or failed integrity checks.", 503)
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModelForImageClassification
            torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
            folder = str(snapshot_path())
            processor = AutoImageProcessor.from_pretrained(
                folder, local_files_only=True, trust_remote_code=False
            )
            model = AutoModelForImageClassification.from_pretrained(
                folder, local_files_only=True, trust_remote_code=False,
                use_safetensors=True, low_cpu_mem_usage=True,
            ).to("cpu")
            model.eval()
            _MODEL = (torch, processor, model)
            return _MODEL
        except Exception as exc:
            raise SafetyError("Local safety model could not be loaded.", 503) from exc


def check_image(data, content_type):
    current = safety_policy.policy(ROOT)
    if not current["enabled"]:
        # The user turned the local content gate off in the Studio settings.
        return {"allowed": True, "screened": False, "label": "not-screened",
                "score": 0.0, "threshold": current["threshold"],
                "revision": MODEL_REVISION}
    if content_type not in IMAGE_TYPES:
        raise SafetyError("Only JPEG, PNG, and WebP images can be screened.", 415)
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise SafetyError("Image is empty or exceeds the 64 MiB limit.", 413)
    digest = hashlib.sha256(data).hexdigest()
    with _CACHE_LOCK:
        cached = _CACHE.get(digest)
        if cached is not None:
            _CACHE.move_to_end(digest)
            return dict(cached)
    try:
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise SafetyError("Image format does not match an allowed type.", 415)
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise SafetyError("Image dimensions exceed the screening limit.", 413)
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image = image.convert("RGB")
    except SafetyError:
        raise
    except Exception as exc:
        raise SafetyError("Image could not be decoded for screening.", 415) from exc

    if not model_status()["installed"] and not safety_policy.fail_closed(ROOT):
        # Screening is on, the local model is not installed, and the configured
        # mode says let the request through and record the fact.
        return {"allowed": True, "screened": False, "label": "not-screened",
                "score": 0.0, "threshold": safety_policy.policy(ROOT)["threshold"],
                "revision": MODEL_REVISION,
                "reason": "local model not installed"}
    torch, processor, model = _load_model()
    try:
        inputs = processor(images=image, return_tensors="pt")
        with torch.inference_mode():
            probabilities = model(**inputs).logits.softmax(dim=-1)[0]
        labels = model.config.id2label
        scores = {
            str(labels[index]).casefold(): float(probabilities[index].item())
            for index in range(len(probabilities))
        }
        nsfw_score = scores.get("nsfw")
        if nsfw_score is None:
            raise ValueError("Model has no nsfw label.")
        threshold = safety_policy.policy(ROOT)["threshold"]
        result = {
            "allowed": nsfw_score < threshold,
            "label": "nsfw" if nsfw_score >= threshold else "normal",
            "score": round(nsfw_score, 6),
            "threshold": threshold,
            "revision": MODEL_REVISION,
            "screened": True,
        }
    except Exception as exc:
        if not safety_policy.fail_closed(ROOT):
            # Screening is on but the local model cannot be used, and the
            # configured mode says let the request through and record it.
            return {"allowed": True, "screened": False, "label": "not-screened",
                    "score": 0.0, "threshold": safety_policy.policy(ROOT)["threshold"],
                    "revision": MODEL_REVISION,
                    "reason": "local model unavailable"}
        raise SafetyError("Local image screening failed.", 503) from exc
    with _CACHE_LOCK:
        _CACHE[digest] = result
        _CACHE.move_to_end(digest)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return dict(result)


def _local_peer(address):
    host = address[0] if address else ""
    return host in ("127.0.0.1", "::1") or host.startswith("::ffff:127.")


def create_server(host="127.0.0.1", port=8788):
    class Handler(BaseHTTPRequestHandler):
        server_version = "JLSafety/1"

        def log_message(self, fmt, *args):
            pass

        def _authorized(self):
            if not _local_peer(self.client_address):
                return False
            if self.headers.get("Host") not in (
                "127.0.0.1:%d" % port, "localhost:%d" % port
            ):
                return False
            origin = self.headers.get("Origin")
            return not origin or origin in (
                "http://127.0.0.1:%d" % port,
                "http://localhost:%d" % port,
            )

        def _send(self, status, value):
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authorized():
                return self._send(403, {"error": "Local safety service only."})
            if self.path != "/health":
                return self._send(404, {"error": "Not found."})
            status = model_status()
            return self._send(200, {
                "ok": True, "service": "jl-studio-safety",
                "root": str(ROOT), **status,
                "policy": safety_policy.describe(ROOT),
            })

        def do_POST(self):
            if not self._authorized():
                return self._send(403, {"error": "Local safety service only."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_IMAGE_BYTES:
                    raise SafetyError("Request body is empty or too large.", 413)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise SafetyError("Request body was incomplete.", 400)
                if self.path == "/v1/check-image":
                    result = check_image(body, self.headers.get("Content-Type", "").split(";")[0].lower())
                elif self.path == "/v1/check-prompt":
                    if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
                        raise SafetyError("Expected application/json.", 415)
                    value = json.loads(body.decode("utf-8"))
                    result = check_prompt(value.get("text") if isinstance(value, dict) else None)
                else:
                    return self._send(404, {"error": "Not found."})
                return self._send(200, result)
            except SafetyError as exc:
                return self._send(exc.status, {"error": str(exc)})
            except Exception:
                return self._send(400, {"error": "Invalid screening request."})

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    server = create_server(
        os.environ.get("JL_SAFETY_HOST", "127.0.0.1"),
        int(os.environ.get("JL_SAFETY_PORT", "8788")),
    )
    server.serve_forever()

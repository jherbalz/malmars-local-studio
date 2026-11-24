"""Offline licence for the Print Station.

The licence is a signed string, not an account and not a server call. The
app holds only the public key, so it can check a licence and cannot make
one. Nothing is switched off when there is no licence - the app does the
same work either way. The licence only says who the install is licensed to.

    JH1.<payload>.<signature>

The payload is the licence details as JSON. The signature is an RSA
signature over it, made by the vendor's private key.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from wglocations import bundle_dir, data_dir

APP = data_dir()
LICENCE_FILE = APP / "licence.json"
PUBLIC_KEY_FILE = bundle_dir() / "public_key.json"
PREFIX = "JH1."
PRODUCT = "Malmars Local Print Station"


def blank(message: str = "") -> dict:
    return {"licensed": False, "licencee": "", "seats": 0, "issued": "",
            "expires": "", "product": PRODUCT, "message": message}


def _public_key() -> dict:
    if not PUBLIC_KEY_FILE.is_file():
        return {}
    try:
        return json.loads(PUBLIC_KEY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signable(payload: bytes, key_len: int) -> bytes:
    digest = hashlib.sha256(payload).digest()
    info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    return b"\x00\x01" + b"\xff" * (key_len - len(info) - 3) + b"\x00" + info


def verify(code: str) -> dict:
    """Check a licence string. Returns the licence, or why it did not pass."""
    out = blank()
    code = (code or "").strip()
    if not code:
        out["message"] = "no licence entered"
        return out
    if not code.startswith(PREFIX):
        out["message"] = "that is not a licence code"
        return out
    body = code[len(PREFIX):]
    if "." not in body:
        out["message"] = "that licence code is damaged"
        return out
    payload_b64, signature_b64 = body.split(".", 1)
    try:
        payload = _b64decode(payload_b64)
        signature = _b64decode(signature_b64)
    except Exception:
        out["message"] = "that licence code is damaged"
        return out
    key = _public_key()
    if not key:
        out["message"] = "this install has no public key"
        return out
    try:
        n = int(key["n"])
        e = int(key.get("e", 65537))
    except Exception:
        out["message"] = "this install has a damaged public key"
        return out
    key_len = (n.bit_length() + 7) // 8
    try:
        value = pow(int.from_bytes(signature, "big"), e, n)
        recovered = value.to_bytes(key_len, "big")
    except Exception:
        out["message"] = "that licence code could not be read"
        return out
    if recovered != _signable(payload, key_len):
        out["message"] = "that licence code is not valid"
        return out
    try:
        data = json.loads(payload.decode("utf-8"))
    except Exception:
        out["message"] = "that licence code is damaged"
        return out
    if not isinstance(data, dict):
        out["message"] = "that licence code is damaged"
        return out
    out.update({
        "licensed": True,
        "licencee": str(data.get("licencee", "")),
        "seats": int(data.get("seats", 0) or 0),
        "issued": str(data.get("issued", "")),
        "expires": str(data.get("expires", "")),
        "product": str(data.get("product", PRODUCT)),
        "message": "licence accepted",
    })
    return out


def load() -> dict:
    """The licence on this machine, if there is one."""
    if not LICENCE_FILE.is_file():
        return blank("no licence file on this install")
    try:
        data = json.loads(LICENCE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return blank("the licence file on this install is damaged")
    code = data.get("code", "") if isinstance(data, dict) else ""
    return verify(code)


def save(code: str) -> dict:
    """Take a licence code, check it, and keep it if it is good."""
    result = verify(code)
    if result.get("licensed"):
        LICENCE_FILE.write_text(json.dumps({"code": code.strip()}, indent=1),
                                encoding="utf-8")
    return result

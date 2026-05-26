"""Tiny helper to call the ITI Mutual Fund jeeth API (encrypted payloads).

The ITI Mutual Fund SPA (https://itiamc.com) calls its backend at
``/jeeth/api/v1/catalog/<endpoint>`` with an AES-128-CBC encrypted JSON
payload wrapped as ``{"eData": base64(ciphertext)}``. The key and IV are
hard-coded in the Angular bundle (``main.<hash>.js``):

    key = b"aar6tzij8o1snaar"   (Latin1)
    iv  = b"0123456789ABCDEF"   (Latin1)

Plaintext shape:
    {<caller fields>, "guid": <32-char random>, "timeStamp": <ms epoch>}

Response shape:
    {"status": 0, "data": {...}}    when successful (eData wrapped)
    {"status": -100, "message": "..."} on error (NOT eData wrapped)

This module is a probe tool only — it lives outside the mfs package and
is never imported by the runtime adapter. Used during calibration to
discover the per-month factsheet URL.
"""
from __future__ import annotations

import base64
import json
import secrets
import sys
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

KEY = b"aar6tzij8o1snaar"
IV = b"0123456789ABCDEF"
BASE = "https://itiamc.com/jeeth/api/v1/catalog"


def aes_encrypt(plaintext: str) -> str:
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(ct).decode("ascii")


def aes_decrypt(b64_ct: str) -> str:
    raw = base64.b64decode(b64_ct)
    dec = Cipher(algorithms.AES(KEY), modes.CBC(IV)).decryptor()
    padded = dec.update(raw) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def _random_guid() -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(32))


def post(endpoint: str, payload: dict):
    body = dict(payload)
    body["guid"] = _random_guid()
    body["timeStamp"] = 1748175000000  # arbitrary stable epoch ms
    pt = json.dumps(body, separators=(",", ":"))
    e_data = aes_encrypt(pt)
    wrapped = json.dumps({"eData": e_data}).encode()
    req = urllib.request.Request(
        f"{BASE}/{endpoint}",
        data=wrapped,
        headers={
            "Content-Type": "application/json",
            "Origin": "https://itiamc.com",
            "Referer": "https://itiamc.com/",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        r = urllib.request.urlopen(req, timeout=30)
        raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        print("HTTPError", e.code, raw[:400], file=sys.stderr)
        return None
    j = json.loads(raw.decode())
    if isinstance(j, dict) and "eData" in j and j["eData"]:
        plain = aes_decrypt(j["eData"])
        return json.loads(plain)
    return j


if __name__ == "__main__":
    ep = sys.argv[1] if len(sys.argv) > 1 else "digitalfactsheet"
    payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    res = post(ep, payload)
    print(json.dumps(res, indent=2, default=str)[:8000] if res is not None else "(no data)")

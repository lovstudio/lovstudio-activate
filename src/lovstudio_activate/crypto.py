"""AES-256-GCM decryption for brand skills.

Reads MANIFEST.enc.json + per-file .enc blobs, returns plaintext.
The decryption key is passed in by the caller (already fetched from the server);
this module never persists it and never reads it from disk.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SkillManifest:
    def __init__(self, skill_dir: Path):
        manifest_path = skill_dir / "MANIFEST.enc.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"MANIFEST.enc.json not found in {skill_dir}")
        data = json.loads(manifest_path.read_text())
        self.skill_dir = skill_dir
        self.skill_id: int = data["skill_id"]
        self.skill_version_id: int = data["skill_version_id"]
        self.skill_name: str | None = data.get("skill_name")
        self.skill_version: str | None = data.get("skill_version")
        self.cipher: str = data.get("cipher", "aes-256-gcm")
        self.files: dict = data["files"]

    def file_meta(self, rel_path: str) -> dict:
        meta = self.files.get(rel_path)
        if not meta:
            raise KeyError(f"'{rel_path}' not in manifest")
        return meta


def decrypt_file(manifest: SkillManifest, rel_path: str, key: bytes) -> bytes:
    """Decrypt one file to plaintext bytes. Verifies SHA256 checksum."""
    meta = manifest.file_meta(rel_path)
    enc_path = manifest.skill_dir / (rel_path + ".enc")
    ciphertext = enc_path.read_bytes()
    iv = base64.b64decode(meta["iv"])
    tag = base64.b64decode(meta["tag"])
    plaintext = AESGCM(key).decrypt(iv, ciphertext + tag, associated_data=None)

    expected = meta.get("original_checksum")
    if expected:
        actual = hashlib.sha256(plaintext).hexdigest()
        if actual != expected:
            raise ValueError(f"checksum mismatch for {rel_path}")
    return plaintext

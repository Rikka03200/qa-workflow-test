"""Credential encryption helpers.

Only authenticated encryption is allowed. If `cryptography` is missing, callers
get a clear setup error instead of an unsafe fallback.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


class CredentialCryptoError(RuntimeError):
    pass


@dataclass(frozen=True)
class EncryptedSecret:
    ciphertext: bytes
    key_version: str


def _fernet_classes():
    try:
        from cryptography.fernet import Fernet, InvalidToken, MultiFernet
        return Fernet, InvalidToken, MultiFernet
    except Exception as exc:  # noqa: BLE001
        raise CredentialCryptoError(
            "缺少 cryptography；请安装 webapp/requirements-web.txt 后再启用凭证加密存储"
        ) from exc


def generate_key() -> str:
    Fernet, _InvalidToken, _MultiFernet = _fernet_classes()
    return Fernet.generate_key().decode("ascii")


def _normalize_key(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise CredentialCryptoError("Fernet key is empty")
    try:
        base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise CredentialCryptoError("Fernet key must be urlsafe-base64 encoded") from exc
    return raw


def keys_from_env() -> list[str]:
    raw = os.environ.get("QA_FERNET_KEYS") or os.environ.get("QA_CREDENTIAL_KEYS") or ""
    return [_normalize_key(part) for part in raw.split(",") if part.strip()]


def load_or_create_local_key(path: Path) -> str:
    """Load a gitignored local dev key or create one with secure randomness."""
    if path.exists():
        return _normalize_key(path.read_text(encoding="utf-8").strip())
    key = generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    return key


def configured_keys(local_key_path: Path | None = None) -> list[str]:
    keys = keys_from_env()
    if keys:
        return keys
    if local_key_path is not None:
        return [load_or_create_local_key(local_key_path)]
    raise CredentialCryptoError("未配置 QA_FERNET_KEYS")


class CredentialCipher:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise CredentialCryptoError("至少需要一个 Fernet key")
        Fernet, _InvalidToken, MultiFernet = _fernet_classes()
        self._fernet_cls = Fernet
        self._invalid_token = _InvalidToken
        self._primary = _normalize_key(keys[0])
        self._cipher = MultiFernet([Fernet(_normalize_key(k).encode("ascii")) for k in keys])

    @property
    def key_version(self) -> str:
        # Do not expose the key. A short random-looking prefix is enough to tell
        # which configured key encrypted a row during rotation.
        return self._primary[:8]

    def encrypt(self, value: str | bytes | None) -> EncryptedSecret:
        if value is None:
            value = ""
        if isinstance(value, str):
            value = value.encode("utf-8")
        return EncryptedSecret(ciphertext=self._cipher.encrypt(value), key_version=self.key_version)

    def decrypt(self, ciphertext: bytes | str | None) -> str:
        if not ciphertext:
            return ""
        if isinstance(ciphertext, str):
            ciphertext = ciphertext.encode("utf-8")
        try:
            return self._cipher.decrypt(ciphertext).decode("utf-8")
        except self._invalid_token as exc:
            raise CredentialCryptoError("凭证密文无法解密，可能密钥不匹配或数据被篡改") from exc


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:3]}••••{value[-3:]}"


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)

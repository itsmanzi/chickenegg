import base64
import os

from cryptography.fernet import Fernet, InvalidToken


def _load_fernet() -> Fernet | None:
    raw = (os.getenv("INCOME_RADAR_FERNET_KEY") or "").strip()
    if not raw:
        return None
    try:
        return Fernet(raw.encode("utf-8"))
    except Exception:
        return None


def generate_fernet_key() -> str:
    return Fernet.generate_key().decode("utf-8")


def encrypt_note(plain: str) -> str:
    f = _load_fernet()
    if not f or not plain:
        return plain
    return f.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_note(stored: str) -> str:
    f = _load_fernet()
    if not f or not stored:
        return stored
    try:
        return f.decrypt(stored.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return "[encrypted — key missing or wrong INCOME_RADAR_FERNET_KEY]"

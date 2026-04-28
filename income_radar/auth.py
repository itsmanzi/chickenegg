import os
import secrets

SESSION_UNLOCKED = "ir_unlocked"


def pin_configured() -> bool:
    return bool((os.getenv("INCOME_RADAR_PIN") or "").strip())


def health_endpoint_public() -> bool:
    return os.getenv("INCOME_RADAR_HEALTH_OPEN", "").strip().lower() in ("1", "true", "yes", "on")


def verify_pin(given: str, expected: str) -> bool:
    if not expected or given is None:
        return False
    g = given.strip().encode("utf-8")
    e = expected.encode("utf-8")
    if len(g) != len(e):
        return False
    return secrets.compare_digest(g, e)

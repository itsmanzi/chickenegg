import io

from app import app, _apply_server_hard_stop, _verify_gumroad_webhook_auth


HARD_STOP_CASES = [
    {"input": "meterkast repareren", "should_stop": True},
    {"input": "gasleiding vervangen", "should_stop": True},
    {"input": "zekering vervangen", "should_stop": True},
    {"input": "kraan repareren", "should_stop": False},
    {"input": "muur schilderen", "should_stop": False},
]


def test_hard_stop_triggers():
    for case in HARD_STOP_CASES:
        result, triggers = _apply_server_hard_stop(
            {"task": case["input"], "steps": []},
            case["input"],
            "nl",
        )
        assert bool(triggers) == case["should_stop"], f"Hard stop failed for: {case['input']}"
        if case["should_stop"]:
            assert result.get("hazard_level") == "danger"


def test_hard_stop_removes_steps():
    result, triggers = _apply_server_hard_stop(
        {"task": "meterkast repareren", "steps": ["stap 1", "stap 2"]},
        "meterkast repareren",
        "nl",
    )
    assert triggers
    steps = result.get("steps") or []
    assert steps, "Hard-stop should provide explicit stop guidance steps"


def test_analyze_endpoint_exists():
    client = app.test_client()
    payload = {
        "image": (io.BytesIO(b"fake-image"), "test.jpg"),
        "language": "nl",
        "device_fingerprint": "pytest-fingerprint",
    }
    response = client.post("/analyze", data=payload, content_type="multipart/form-data")
    assert response.status_code != 500


def test_gumroad_webhook_requires_secret_or_explicit_dev_flag(monkeypatch):
    """Misconfigured deployments must not accept unsigned Gumroad posts (Pro license forgery)."""
    monkeypatch.delenv("GUMROAD_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("GUMROAD_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_GUMROAD_WEBHOOK", raising=False)
    with app.test_request_context("/webhooks/gumroad", method="POST"):
        assert _verify_gumroad_webhook_auth() is False
    client = app.test_client()
    r = client.post(
        "/webhooks/gumroad",
        json={"email": "attacker@example.com", "sale_id": "fake-sale-id"},
    )
    assert r.status_code == 401


def test_gumroad_webhook_unauthenticated_allowed_only_with_opt_in(monkeypatch):
    monkeypatch.delenv("GUMROAD_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("GUMROAD_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_GUMROAD_WEBHOOK", "1")
    with app.test_request_context("/webhooks/gumroad", method="POST"):
        assert _verify_gumroad_webhook_auth() is True

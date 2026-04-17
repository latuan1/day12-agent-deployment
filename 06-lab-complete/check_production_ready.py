from __future__ import annotations

import json

from app import app, settings


def assert_status(response, expected_status: int, label: str):
    if response.status_code != expected_status:
        raise SystemExit(f"{label} failed: expected {expected_status}, got {response.status_code}: {response.data!r}")


def main():
    client = app.test_client()

    health = client.get("/health")
    assert_status(health, 200, "/health")

    ready = client.get("/ready")
    assert_status(ready, 200, "/ready")

    protected = client.post("/ask", json={"question": "Hello"})
    assert_status(protected, 401, "auth required")

    headers = {"X-API-Key": settings.agent_api_key, "X-User-Id": "check-user"}

    previous_rate = settings.rate_limit_per_minute
    settings.rate_limit_per_minute = 1
    first_rate = client.post(
        "/ask",
        json={"question": "Rate limit test", "session_id": "rate-check"},
        headers=headers,
    )
    assert_status(first_rate, 200, "first rate-limited request")
    second_rate = client.post(
        "/ask",
        json={"question": "Rate limit test 2", "session_id": "rate-check-2"},
        headers=headers,
    )
    assert_status(second_rate, 429, "rate limit enforcement")
    settings.rate_limit_per_minute = previous_rate

    previous_budget = settings.monthly_budget_usd
    settings.monthly_budget_usd = 0.0
    budget_check = client.post(
        "/ask",
        json={"question": "Budget check", "session_id": "budget-check"},
        headers={"X-API-Key": settings.agent_api_key, "X-User-Id": "budget-user"},
    )
    assert_status(budget_check, 402, "cost guard enforcement")
    settings.monthly_budget_usd = previous_budget

    chat = client.post(
        "/api/chat",
        json={"message": "Hello there", "mode": "chatbot", "session_id": "ui-session"},
    )
    assert_status(chat, 200, "/api/chat")
    chat_body = json.loads(chat.data)
    session_id = chat_body.get("session_id")
    if not session_id:
        raise SystemExit("/api/chat did not return a session_id")

    session = client.get(f"/api/sessions/{session_id}")
    assert_status(session, 200, "session lookup")
    session_body = json.loads(session.data)
    if session_body.get("count", 0) < 2:
        raise SystemExit("conversation history was not persisted")

    print("Production readiness checks passed.")


if __name__ == "__main__":
    main()
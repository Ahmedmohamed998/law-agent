"""
The product loop, end to end: a conversation, a paid consultation, and the
model standing down.

    python -m product.tests.test_payment_loop

Needs the Node backend on :8001 and the AI service on :8000, with the AI
service pointed at the Node JWKS.

What this proves, and what it does not:

  * It DOES prove that a correctly-signed callback records the money exactly
    once, escalates the session, and that the model then refuses — plus that a
    forged signature changes nothing and a retry is a no-op.

  * It does NOT prove our HMAC field order matches Paymob's. The simulator
    builds the concatenation with the same constant the verifier uses, so both
    would be wrong together. Only a real sandbox callback settles that, and it
    is the one thing here that needs live credentials.
"""

import json
import subprocess
import sys
import uuid
from pathlib import Path

import httpx

AI = "http://127.0.0.1:8000"
NODE = "http://127.0.0.1:8001"
BACKEND = Path(__file__).resolve().parents[2] / "backend"
PASSWORD = "correct-horse-battery"
RUN = uuid.uuid4().hex[:8]

ok = 0
fail = 0


def check(desc: str, condition: bool, extra: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  ok:   {desc}")
    else:
        fail += 1
        print(f"  FAIL: {desc} {extra}")


def admin_key() -> str:
    root = Path(__file__).resolve().parents[2]
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ADMIN_API_KEY not found")


def simulate(consultation_id: str, *args: str) -> dict:
    """Fire a Paymob-shaped callback, signed with the local secret."""
    proc = subprocess.run(
        ["npx", "tsx", "src/tools/simulate-webhook.ts", consultation_id, *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        shell=True,
    )
    body = proc.stdout.strip().splitlines()
    for line in reversed(body):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"simulator gave no JSON:\n{proc.stdout}\n{proc.stderr}")


def main() -> int:
    KEY = admin_key()

    print("=== 1. a user has a conversation ===")
    user = httpx.post(
        f"{NODE}/auth/signup",
        json={"email": f"loop+{RUN}@lawagent-qa.com", "password": PASSWORD},
        timeout=15,
    ).json()
    H = {"Authorization": f"Bearer {user['access_token']}"}

    session = httpx.post(f"{AI}/v1/sessions", json={"lang": "ar"}, headers=H, timeout=20)
    check("session created on the AI service", session.status_code == 201, session.text[:160])
    session_id = session.json()["session_id"]

    print()
    print("=== 2. they buy a consultation ===")
    created = httpx.post(
        f"{NODE}/consultations",
        json={"ai_session_id": session_id, "amount_cents": 50000, "currency": "EGP"},
        headers=H,
        timeout=20,
    )
    check("consultation created", created.status_code == 201, created.text[:200])
    consultation_id = created.json()["consultation_id"]
    check("starts pending", created.json()["status"] == "pending")
    check("not yet escalated", created.json()["escalated"] is False)

    dupe = httpx.post(
        f"{NODE}/consultations",
        json={"ai_session_id": session_id, "amount_cents": 50000},
        headers=H,
        timeout=20,
    )
    check("cannot double-buy the same conversation", dupe.status_code == 409, dupe.text[:160])

    print()
    print("=== 3. a forged callback changes nothing ===")
    forged = simulate(consultation_id, "--bad-hmac")
    check("bad signature is rejected", forged.get("status") == "invalid_signature", str(forged))
    still = httpx.get(f"{NODE}/consultations/{consultation_id}", headers=H, timeout=15).json()
    check("consultation still pending after forgery", still["status"] == "pending", str(still))

    r = httpx.post(f"{AI}/v1/sessions/{session_id}/messages", headers=H, timeout=30,
                   json={"content": "test", "client_message_id": f"pre-{RUN}"})
    check("model still answers before payment", r.status_code != 409, f"got {r.status_code}")

    print()
    print("=== 4. the real callback closes the loop ===")
    paid = simulate(consultation_id)
    check("payment accepted", paid.get("status") == "paid", str(paid))

    after = httpx.get(f"{NODE}/consultations/{consultation_id}", headers=H, timeout=15).json()
    check("consultation marked paid", after["status"] == "paid", str(after))
    check("session escalated with the AI service", after["escalated"] is True, str(after))

    r = httpx.post(f"{AI}/v1/sessions/{session_id}/messages", headers=H, timeout=30,
                   json={"content": "بعد الدفع", "client_message_id": f"post-{RUN}"})
    check("model now refuses -> 409", r.status_code == 409, f"got {r.status_code} {r.text[:120]}")
    check("409 names the reason",
          r.json().get("error", {}).get("code") == "session_escalated", r.text[:160])

    print()
    print("=== 5. gateways retry; that must be a no-op ===")
    replay = simulate(consultation_id, "--txn", "999000111")
    check("first delivery of a new txn is processed",
          replay.get("status") in ("paid", "duplicate"), str(replay))
    again = simulate(consultation_id, "--txn", "999000111")
    check("same txn again is a duplicate", again.get("status") == "duplicate", str(again))

    print()
    print("=== 6. nothing is stuck ===")
    stuck = httpx.get(f"{NODE}/admin/billing/unescalated",
                      headers={"X-Admin-Key": KEY}, timeout=15)
    check("admin queue reachable", stuck.status_code == 200, stuck.text[:160])
    check("no paid-but-unescalated consultations",
          all(c["consultation_id"] != consultation_id for c in stuck.json()),
          str(stuck.json())[:200])

    unauth = httpx.get(f"{NODE}/admin/billing/unescalated", timeout=15)
    check("admin queue needs the key", unauth.status_code == 403, f"got {unauth.status_code}")

    print()
    print("=== 7. self-erasure removes both sides ===")
    erased = httpx.delete(f"{NODE}/auth/me", headers=H, timeout=20)
    check("self-erasure returns 202", erased.status_code == 202, erased.text[:160])
    check("erasure delivered to the AI service",
          erased.json().get("status") == "delivered", erased.text[:200])

    gone = httpx.get(f"{AI}/v1/sessions", headers=H, timeout=15)
    check("the AI service holds nothing for them",
          gone.status_code == 200 and gone.json() == [], gone.text[:160])

    print()
    print(f"{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

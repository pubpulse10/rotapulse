"""Staff Suite: the Hub's POST /internal/staff-suite adds or removes the 25%
coupon shared with HR-Pulse on every venue subscription the pub has."""

from types import SimpleNamespace

import stripe

from app import config
from app import db as db_module

H = {"Authorization": "Bearer test-secret"}


def _setup(app, venue, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    monkeypatch.setattr(config, "STRIPE_BUNDLE_COUPON_ID", "SUITE25")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_subscription SET stripe_subscription_id = 'sub_r' WHERE venue_id = ?",
                     (venue["id"],))
        conn.commit()
    state = {"discounts": [], "modified": []}
    monkeypatch.setattr(stripe.Subscription, "retrieve",
                        lambda sub_id, expand=None: SimpleNamespace(discounts=list(state["discounts"])))

    def modify(sub_id, discounts):
        state["modified"].append((sub_id, discounts))
        state["discounts"] = [SimpleNamespace(id="di_1", source=SimpleNamespace(coupon="SUITE25"))
                              for d in (discounts or []) if d.get("coupon")]
    monkeypatch.setattr(stripe.Subscription, "modify", modify)
    return state


def test_on_then_off(app, client, venue, monkeypatch):
    state = _setup(app, venue, monkeypatch)
    resp = client.post("/internal/staff-suite", headers=H, json={"pub_id": venue["pub_id"], "on": True})
    assert resp.status_code == 200 and resp.get_json() == {"ok": True, "changed": 1}
    assert state["modified"] == [("sub_r", [{"coupon": "SUITE25"}])]
    # Idempotent.
    assert client.post("/internal/staff-suite", headers=H,
                       json={"pub_id": venue["pub_id"], "on": True}).get_json()["changed"] == 0
    resp = client.post("/internal/staff-suite", headers=H, json={"pub_id": venue["pub_id"], "on": False})
    assert resp.get_json() == {"ok": True, "changed": 1}
    assert state["modified"][-1] == ("sub_r", "")


def test_needs_auth_and_a_coupon(app, client, venue, monkeypatch):
    _setup(app, venue, monkeypatch)
    assert client.post("/internal/staff-suite", json={"pub_id": venue["pub_id"], "on": True}).status_code == 401
    monkeypatch.setattr(config, "STRIPE_BUNDLE_COUPON_ID", None)
    assert client.post("/internal/staff-suite", headers=H,
                       json={"pub_id": venue["pub_id"], "on": True}).status_code == 503


def test_shared_module_is_byte_identical_to_hrpulse():
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "app" / "staff_suite.py"
    sibling = Path(__file__).resolve().parents[2] / "hrpulse" / "app" / "staff_suite.py"
    if sibling.exists():  # only on a machine with both checkouts
        assert here.read_bytes() == sibling.read_bytes()

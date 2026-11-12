import os
import tempfile
from datetime import datetime, timedelta

os.environ["CA_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db, engine, seed  # noqa: E402

conn, stats = seed.build(os.environ["CA_DB"], verbose=False)
conn.close()
from app import main  # noqa: E402

api = TestClient(main.app)
STUDENT, STRANGER, DROP_CARD, MOM = 1001, 1002, 1003, 1004


def test_engine_quality_on_synthetic_data():
    assert stats["tri_caught"] / stats["tri"] >= 0.75
    assert stats["honest_held"] / stats["honest"] <= 0.01


def test_ordinary_transfer_passes():
    r = api.post("/transfers", json={"sender_id": STUDENT, "receiver_id": MOM, "amount": 3000}).json()
    assert r["status"] == "done"


def test_scheme_is_held_and_safely_returned():
    before = api.get(f"/clients/{STRANGER}").json()["balance"]
    r = api.post("/transfers", json={"sender_id": STUDENT, "receiver_id": DROP_CARD, "amount": 150000}).json()
    assert r["status"] == "held"
    assert r["safe_return"]["to_id"] == STRANGER
    codes = {x["code"] for x in r["reasons"]}
    assert {"stranger_incoming", "not_to_sender", "amount_match"} <= codes
    # Деньги дропперу не ушли.
    assert api.get(f"/clients/{DROP_CARD}").json()["balance"] == 0

    ret = api.post(f"/cases/{r['case_id']}/return-to-sender").json()
    assert ret["status"] == "returned" and ret["amount"] == 150000
    assert api.get(f"/clients/{STRANGER}").json()["balance"] == before + 150000
    # Повторный возврат невозможен.
    assert api.post(f"/cases/{r['case_id']}/return-to-sender").status_code == 409


def test_return_to_same_sender_is_never_held():
    c = db.connect(":memory:")
    now = datetime(2026, 11, 12, 12, 0)
    for i in (1, 2):
        c.execute("INSERT INTO clients(id,name,account_opened_at) VALUES(?,?,?)",
                  (i, f"c{i}", (now - timedelta(days=500)).isoformat()))
    c.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at) VALUES(2,1,90000,?)",
              ((now - timedelta(minutes=30)).isoformat(),))
    assert engine.evaluate(c, 1, 2, 90000, now)["decision"] == "allow"

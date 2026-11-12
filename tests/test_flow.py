"""Сквозные тесты. Каждый тест получает свою копию базы, порядок запуска не важен."""
import base64
import os
import shutil
import tempfile
from datetime import datetime, timedelta

import pytest

_tmp = tempfile.mkdtemp()
os.environ["CA_DB"] = os.path.join(_tmp, "boot.db")
os.environ["CA_UPLOADS"] = os.path.join(_tmp, "uploads")
os.environ["CA_ANALYST_PIN"] = "4321"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, engine, main, seed  # noqa: E402

TEMPLATE = os.path.join(_tmp, "template.db")
_conn, STATS = seed.build(TEMPLATE, verbose=False)
_conn.close()

STUDENT, STRANGER, DROP_CARD, MOM, SELLER, BUYER, TRANSIT = 1001, 1002, 1003, 1004, 1005, 1006, 1007
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
B64 = lambda data: base64.b64encode(data).decode()  # noqa: E731


@pytest.fixture(autouse=True)
def fresh(tmp_path):
    shutil.copy(TEMPLATE, tmp_path / "t.db")
    main.conn = db.connect(str(tmp_path / "t.db"))
    main.UPLOADS = str(tmp_path / "uploads")
    main.SESSIONS.clear()
    main._pin_failures.clear()
    yield
    main.conn.close()


def client(cid):
    c = TestClient(main.app)
    assert c.post("/login", json={"role": "client", "client_id": cid}).status_code == 200
    return c


def analyst():
    c = TestClient(main.app)
    assert c.post("/login", json={"role": "analyst", "pin": "4321"}).status_code == 200
    return c


def send(c, to, amount):
    return c.post("/transfers", json={"receiver_id": to, "amount": amount})


def account(c, cid):
    return c.get(f"/clients/{cid}").json()


# ---------- Движок ----------

def test_engine_quality_on_synthetic_data():
    assert STATS["tri_caught"] / STATS["tri"] >= 0.75
    assert STATS["honest_held"] / STATS["honest"] <= 0.01


def test_return_to_same_sender_is_never_held():
    c = db.connect(":memory:")
    now = datetime(2026, 11, 12, 12, 0)
    for i in (1, 2):
        c.execute("INSERT INTO clients(id,name,account_opened_at) VALUES(?,?,?)",
                  (i, f"c{i}", (now - timedelta(days=500)).isoformat()))
    c.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at) VALUES(2,1,90000,?)",
              ((now - timedelta(minutes=30)).isoformat(),))
    assert engine.evaluate(c, 1, 2, 90000, now)["decision"] == "allow"


def test_splitting_the_amount_is_caught_on_the_second_part():
    st = client(STUDENT)
    # Покупатель — получатель со старым счётом, с которым у студентки не было переводов.
    first = send(st, BUYER, 80000).json()
    assert first["status"] == "done"
    second = send(st, BUYER, 70000).json()
    assert second["status"] == "held"
    assert "split_forward" in {r["code"] for r in second["reasons"]}


# ---------- Деньги ----------

def test_ordinary_transfer_passes():
    assert send(client(STUDENT), MOM, 3000).json()["status"] == "done"


def test_scheme_is_held_and_safely_returned_once():
    st, an = client(STUDENT), analyst()
    before = account(an, STRANGER)["balance"]
    r = send(st, DROP_CARD, 150000).json()
    assert r["status"] == "held" and r["safe_return"]["to_id"] == STRANGER
    assert {"stranger_incoming", "not_to_sender", "amount_match"} <= {x["code"] for x in r["reasons"]}
    assert account(an, DROP_CARD)["balance"] == 0          # дропперу ничего не ушло

    ret = st.post(f"/cases/{r['case_id']}/return-to-sender").json()
    assert ret["status"] == "returned" and ret["amount"] == 150000
    assert account(an, STRANGER)["balance"] == before + 150000
    assert st.post(f"/cases/{r['case_id']}/return-to-sender").status_code == 409
    assert account(st, STUDENT)["balance"] == 42000


def test_paused_transfer_reserves_money():
    st = client(STUDENT)
    assert send(st, DROP_CARD, 150000).json()["status"] == "held"
    acc = account(st, STUDENT)
    assert acc["held"] == 150000 and acc["available"] == 42000
    assert send(st, MOM, 100000).status_code == 400        # зарезервированное потратить нельзя
    assert send(st, MOM, 40000).json()["status"] == "done"


def test_repeated_attempt_reuses_the_case_and_cannot_return_twice():
    st, an = client(STUDENT), analyst()
    a = send(st, DROP_CARD, 150000).json()
    b = send(st, DROP_CARD, 140000).json()
    assert a["case_id"] == b["case_id"]
    assert account(st, STUDENT)["held"] == 140000          # первая попытка отменена, резерв один
    assert len([c for c in an.get("/analyst/cases").json() if c["client_id"] == STUDENT]) == 1
    assert st.post(f"/cases/{b['case_id']}/return-to-sender").status_code == 200
    assert st.post(f"/cases/{b['case_id']}/return-to-sender").status_code == 409
    assert account(st, STUDENT)["balance"] == 42000        # вернули ровно один раз


def test_return_is_refused_when_money_is_spent_and_nothing_changes():
    st = client(STUDENT)
    assert send(st, MOM, 30000).json()["status"] == "done"
    assert send(st, MOM, 20000).json()["status"] == "done"   # часть чужих денег потрачена
    r = send(st, DROP_CARD, 140000).json()
    assert r["status"] == "held"
    before = account(st, STUDENT)
    refused = st.post(f"/cases/{r['case_id']}/return-to-sender")
    assert refused.status_code == 400 and "142 000" in refused.json()["detail"]
    after = account(st, STUDENT)
    assert after["balance"] == before["balance"] == 142000 and after["held"] == 140000
    assert st.get(f"/cases/{r['case_id']}").json()["case_status"] == "open"


# ---------- Вход и права ----------

def test_anonymous_and_foreign_access_is_refused():
    anon = TestClient(main.app)
    assert anon.get(f"/clients/{STUDENT}").status_code == 401
    assert anon.get("/analyst/cases").status_code == 401
    assert send(anon, MOM, 1000).status_code == 401

    st, mom = client(STUDENT), client(MOM)
    assert mom.get(f"/clients/{STUDENT}").status_code == 403
    assert mom.get(f"/clients/{STUDENT}/case").status_code == 403
    assert mom.get("/analyst/cases").status_code == 401
    assert mom.post("/demo/reset").status_code == 401
    r = send(st, DROP_CARD, 150000).json()
    for path in ("return-to-sender", "insist"):
        assert mom.post(f"/cases/{r['case_id']}/{path}").status_code == 404
    assert mom.get(f"/cases/{r['case_id']}").status_code == 404
    # Отправитель берётся из сессии: подставить чужой счёт в запрос нельзя.
    forged = mom.post("/transfers", json={"sender_id": STUDENT, "receiver_id": STRANGER, "amount": 1000}).json()
    assert forged["status"] == "done"
    assert account(st, STUDENT)["balance"] == 192000 and account(mom, MOM)["balance"] == 149000


def test_analyst_code_is_checked_and_rate_limited():
    anon = TestClient(main.app)
    for _ in range(5):
        assert anon.post("/login", json={"role": "analyst", "pin": "0000"}).status_code == 401
    assert anon.post("/login", json={"role": "analyst", "pin": "4321"}).status_code == 429
    assert anon.post("/login", json={"role": "client", "client_id": 5}).status_code == 403


# ---------- Демо-данные ----------

def test_empty_database_is_filled_on_first_request(tmp_path):
    main.conn.close()
    main.conn = db.connect(str(tmp_path / "empty.db"))
    assert len(TestClient(main.app).get("/demo/clients").json()) == 7


def test_stale_demo_data_is_recreated():
    old = (datetime.now() - timedelta(days=4)).replace(microsecond=0).isoformat()
    main.conn.execute("UPDATE meta SET value=? WHERE key='seeded_at'", (old,))
    main.conn.execute("UPDATE transfers SET created_at=? WHERE scenario='demo_in'", (old,))
    main.conn.commit()
    TestClient(main.app).get("/demo/clients")
    assert send(client(STUDENT), DROP_CARD, 150000).json()["status"] == "held"


def test_analyst_can_reset_demo():
    st, an = client(STUDENT), analyst()
    send(st, MOM, 5000)
    assert an.post("/demo/reset").status_code == 200
    assert account(an, STUDENT)["balance"] == 192000
    assert st.get(f"/clients/{STUDENT}").status_code == 401     # клиентские сессии сброшены


# ---------- Апелляция и документы ----------

def test_uploads_are_restricted():
    sel, mom, an = client(SELLER), client(MOM), analyst()
    cid = sel.get(f"/clients/{SELLER}/case").json()["case_id"]
    up = lambda name, data, kind="chat", who=sel: who.post(  # noqa: E731
        f"/cases/{cid}/documents", json={"kind": kind, "filename": name, "content_base64": B64(data)})
    assert up("x.html", b"<script>alert(1)</script>").status_code == 400
    assert up("x.png", b"not really a png").status_code == 400
    assert up("x.png", PNG, who=mom).status_code == 404
    ok = up("../../чат.html", PNG).json()
    assert ok["filename"].endswith(".png") and "/" not in ok["filename"] and "html" not in ok["filename"]

    assert mom.get(f"/documents/{ok['id']}").status_code == 404
    got = an.get(f"/documents/{ok['id']}")
    assert got.content == PNG and got.headers["content-type"] == "image/png"
    assert "attachment" in got.headers["content-disposition"]
    assert got.headers["x-content-type-options"] == "nosniff"

    for _ in range(main.MAX_DOCS - 1):
        assert up("a.png", PNG, kind="other").status_code == 200
    assert up("a.png", PNG, kind="other").status_code == 400      # лимит файлов
    big = sel.post(f"/cases/{cid}/documents", content=b"x" * (main.MAX_BODY + 1),
                   headers={"content-type": "application/json"})
    assert big.status_code == 413


def test_blocked_client_appeals_and_analyst_decides():
    sel, an = client(SELLER), analyst()
    assert send(sel, MOM, 1000).status_code == 403
    case = sel.get(f"/clients/{SELLER}/case").json()
    assert case["status"] == "open" and "200 000 ₸" in case["summary"] and "40 мин" in case["summary"]
    cid = case["case_id"]
    text = "Покупатель оплатил товар и попросил вернуть деньги на другую карту."
    assert sel.post(f"/cases/{cid}/submit", json={"explanation": "не я"}).status_code == 400
    assert sel.post(f"/cases/{cid}/submit", json={"explanation": text}).status_code == 400   # нет переписки
    doc = {"kind": "chat", "filename": "chat.png", "content_base64": B64(PNG)}
    assert sel.post(f"/cases/{cid}/documents", json=doc).status_code == 200
    assert sel.post(f"/cases/{cid}/submit", json={"explanation": text}).json()["status"] == "under_review"
    assert sel.post(f"/cases/{cid}/documents", json=doc).status_code == 409

    by_client = {c["client_id"]: c for c in an.get("/analyst/cases").json()}
    assert by_client[SELLER]["role"]["verdict"] == "unwitting"
    assert by_client[TRANSIT]["role"]["verdict"] == "organizer" and by_client[TRANSIT]["role"]["episodes"] == 4
    card = an.get(f"/analyst/cases/{cid}").json()
    assert card["incoming"]["amount"] == 200000 and card["explanation"] and card["documents"]

    transit = by_client[TRANSIT]["case_id"]
    assert an.post(f"/analyst/cases/{transit}/decision", json={"decision": "reject"}).status_code == 400
    kept = an.post(f"/analyst/cases/{transit}/decision",
                   json={"decision": "reject", "comment": "Четыре пересылки от разных людей за пять дней."}).json()
    assert kept["status"] == "kept" and kept["client_status"] == "blocked"

    done = an.post(f"/analyst/cases/{cid}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "unblocked" and done["client_status"] == "active"
    assert an.post(f"/analyst/cases/{cid}/decision", json={"decision": "approve"}).status_code == 409
    assert sel.get(f"/clients/{SELLER}/case").json()["status"] == "unblocked"
    assert send(sel, MOM, 1000).json()["status"] == "done"


def test_client_sees_what_happened_to_a_paused_transfer():
    st, an = client(STUDENT), analyst()
    r = send(st, DROP_CARD, 150000).json()
    row = lambda: next(t for t in account(st, STUDENT)["feed"] if t["id"] == r["transfer_id"])  # noqa: E731
    assert row()["case_status"] == "open"
    queue = {c["case_id"]: c for c in an.get("/analyst/cases").json()}
    assert queue[r["case_id"]]["status_text"] == "Клиент ещё не выбрал действие"
    assert not queue[r["case_id"]]["needs_decision"]

    assert st.post(f"/cases/{r['case_id']}/insist").json()["status"] == "under_review"
    assert send(st, DROP_CARD, 150000).status_code == 409           # повторить в обход проверки нельзя
    out = an.post(f"/analyst/cases/{r['case_id']}/decision",
                  json={"decision": "reject", "comment": "Получатель похож на дропперский счёт."}).json()
    assert out["status"] == "cancelled"
    seen = row()
    assert seen["status"] == "cancelled" and seen["case_status"] == "cancelled"
    assert seen["decision_comment"] == "Получатель похож на дропперский счёт."
    acc = account(st, STUDENT)
    assert acc["held"] == 0 and acc["available"] == 192000          # резерв снят


def test_analyst_can_release_a_paused_transfer():
    st, an = client(STUDENT), analyst()
    r = send(st, DROP_CARD, 150000).json()
    st.post(f"/cases/{r['case_id']}/insist")
    assert an.post(f"/analyst/cases/{r['case_id']}/decision", json={"decision": "approve"}).json()["status"] == "released"
    assert account(st, STUDENT)["balance"] == 42000 and account(an, DROP_CARD)["balance"] == 150000

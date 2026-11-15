"""Сквозные тесты. Каждый тест получает свою копию базы, порядок запуска не важен."""
import base64
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

_tmp = tempfile.mkdtemp()
os.environ["CA_DB"] = os.path.join(_tmp, "boot.db")
os.environ["CA_UPLOADS"] = os.path.join(_tmp, "uploads")
os.environ["CA_ANALYST_PIN"] = "4321"
os.environ["CA_DEMO_HISTORY"] = "0"      # история за 30 дней проверяется отдельно, остальным тестам она не нужна
os.environ["CA_DOC_KEY_FILE"] = os.path.join(_tmp, "doc.key")

from fastapi.testclient import TestClient  # noqa: E402

from app import db, engine, main, seed  # noqa: E402

TEMPLATE = os.path.join(_tmp, "template.db")
_conn, STATS = seed.build(TEMPLATE, verbose=False)
_conn.close()

STUDENT, STRANGER, DROP_CARD, MOM, SELLER, VICTIM, TRANSIT = 1001, 1002, 1003, 1004, 1005, 1006, 1007
FAKE_INVEST, SAFE_ACCOUNT = 1008, 1009
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


def feed_item(c, cid, sender_id):
    return next(t for t in account(c, cid)["feed"] if t["direction"] == "in" and t["sender_id"] == sender_id)


def block_seller():
    """Гульнар жалуется на перевод Данияру; он уже переслал деньги, поэтому его блокируют."""
    victim = client(VICTIM)
    tid = next(t["id"] for t in account(victim, VICTIM)["feed"] if t["receiver_id"] == SELLER and t["amount"] == 200000)
    r = victim.post(f"/transfers/{tid}/complaint").json()
    assert r["outcome"] == "blocked"
    return victim, tid


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
    # Гульнар — получатель со старым счётом, с которым у студентки не было переводов.
    first = send(st, VICTIM, 80000).json()
    assert first["status"] == "done"
    second = send(st, VICTIM, 70000).json()
    assert second["status"] == "held"
    assert "split_forward" in {r["code"] for r in second["reasons"]}


# ---------- Деньги ----------

def test_ordinary_transfer_passes():
    assert send(client(STUDENT), MOM, 3000).json()["status"] == "done"


def test_scheme_is_held_and_safely_returned_once():
    st, an = client(STUDENT), analyst()
    before = account(an, STRANGER)["balance"]
    drop_before = account(an, DROP_CARD)["balance"]
    r = send(st, DROP_CARD, 150000).json()
    assert r["status"] == "held" and r["safe_return"]["to_id"] == STRANGER
    assert {"stranger_incoming", "not_to_sender", "amount_match"} <= {x["code"] for x in r["reasons"]}
    assert account(an, DROP_CARD)["balance"] == drop_before   # дропперу ничего не ушло

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
    assert len(TestClient(main.app).get("/demo/clients").json()) == 10


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
    block_seller()
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
    # На диске документ лежит зашифрованным и без имени клиента.
    stored = os.listdir(main.UPLOADS)
    assert stored == [f"{ok['id']}.enc"]
    raw = open(os.path.join(main.UPLOADS, stored[0]), "rb").read()
    assert PNG not in raw and b"PNG" not in raw
    with open(os.path.join(main.UPLOADS, stored[0]), "wb") as f:     # подмена файла на диске
        f.write(raw[:-5] + b"xxxxx")
    assert an.get(f"/documents/{ok['id']}").status_code == 409
    assert got.headers["x-content-type-options"] == "nosniff"

    for _ in range(main.MAX_DOCS - 1):
        assert up("a.png", PNG, kind="other").status_code == 200
    assert up("a.png", PNG, kind="other").status_code == 400      # лимит файлов
    big = sel.post(f"/cases/{cid}/documents", content=b"x" * (main.MAX_BODY + 1),
                   headers={"content-type": "application/json"})
    assert big.status_code == 413


def test_blocked_client_appeals_and_analyst_decides():
    sel, an = client(SELLER), analyst()
    assert send(sel, MOM, 1000).json()["status"] == "done"      # до жалобы Данияр ничего не знает
    block_seller()
    assert send(sel, MOM, 1000).status_code == 403
    case = sel.get(f"/clients/{SELLER}/case").json()
    assert case["status"] == "open" and "200 000 ₸" in case["summary"] and "40 мин" in case["summary"]
    cid = case["case_id"]
    text = "Покупатель оплатил товар и попросил вернуть деньги на другую карту."
    assert sel.post(f"/cases/{cid}/submit", json={"explanation": "не я"}).status_code == 400
    assert sel.post(f"/cases/{cid}/submit", json={"explanation": text}).status_code == 400   # нет переписки
    doc = {"kind": "chat", "filename": "chat.png", "content_base64": B64(PNG)}
    assert sel.post(f"/cases/{cid}/documents", json=doc).status_code == 200
    submitted = sel.post(f"/cases/{cid}/submit", json={"explanation": text}).json()
    assert submitted["status"] == "under_review" and submitted["due_at"] > submitted["submitted_at"]
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
    actions = [h["action"] for h in done["history"]]
    assert actions == ["complaint", "blocked", "document", "appeal", "decision", "cleared"]
    assert done["history"][-2]["details"]["role"] == "Похоже на невольного участника"
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
    drop_before = account(an, DROP_CARD)["balance"]
    r = send(st, DROP_CARD, 150000).json()
    st.post(f"/cases/{r['case_id']}/insist")
    assert an.post(f"/analyst/cases/{r['case_id']}/decision", json={"decision": "approve"}).json()["status"] == "released"
    assert account(st, STUDENT)["balance"] == 42000 and account(an, DROP_CARD)["balance"] == drop_before + 150000


# ---------- Предупреждение, жалоба, сроки ----------

def test_incoming_from_stranger_is_flagged_and_can_be_returned_from_the_feed():
    st, an = client(STUDENT), analyst()
    item = feed_item(st, STUDENT, STRANGER)
    assert item["warning"] and not item["returned"]
    assert not feed_item(st, STUDENT, MOM)["warning"]                 # мама — знакомый отправитель
    before = account(an, STRANGER)["balance"]
    assert st.post(f"/incoming/{item['id']}/return").json()["amount"] == 150000
    assert account(an, STRANGER)["balance"] == before + 150000
    assert not feed_item(st, STUDENT, STRANGER)["warning"]
    assert st.post(f"/incoming/{item['id']}/return").status_code == 409
    # Вернуть чужой входящий нельзя.
    assert client(MOM).post(f"/incoming/{item['id']}/return").status_code == 404


def test_return_from_feed_closes_a_paused_forward():
    st = client(STUDENT)
    r = send(st, DROP_CARD, 150000).json()
    item = feed_item(st, STUDENT, STRANGER)
    assert st.post(f"/incoming/{item['id']}/return").status_code == 200
    assert st.get(f"/cases/{r['case_id']}").json()["case_status"] == "returned"
    acc = account(st, STUDENT)
    assert acc["held"] == 0 and acc["balance"] == 42000


def test_known_sender_hides_the_warning_but_not_the_interception():
    st = client(STUDENT)
    item = feed_item(st, STUDENT, STRANGER)
    assert st.post(f"/incoming/{item['id']}/known").status_code == 200
    assert not feed_item(st, STUDENT, STRANGER)["warning"]
    assert send(st, DROP_CARD, 150000).json()["status"] == "held"


def test_complaint_blocks_the_forwarder_and_creates_a_case():
    an = analyst()
    victim, tid = block_seller()
    queue = {c["client_id"]: c for c in an.get("/analyst/cases").json()}
    case = queue[SELLER]
    assert case["from_complaint"] and case["role"]["verdict"] == "unwitting"
    card = an.get(f"/analyst/cases/{case['case_id']}").json()
    assert card["reasons"][0]["code"] == "complaint" and card["outgoing"]["amount"] == 195000
    assert [h["action"] for h in card["history"]] == ["complaint", "blocked"]
    assert victim.post(f"/transfers/{tid}/complaint").status_code == 409
    assert client(MOM).post(f"/transfers/{tid}/complaint").status_code == 404   # жалуется только отправитель


def test_complaint_when_money_is_still_there_warns_the_receiver():
    stranger, st = client(STRANGER), client(STUDENT)
    tid = feed_item(st, STUDENT, STRANGER)["id"]
    st.post(f"/incoming/{tid}/known")                                   # даже если получатель скрыл предупреждение
    r = stranger.post(f"/transfers/{tid}/complaint").json()
    assert r["outcome"] == "warned"
    item = feed_item(st, STUDENT, STRANGER)
    assert item["warning"] and item["complained"]
    assert account(st, STUDENT)["status"] == "active"


def test_complaint_after_return_says_so():
    stranger, st = client(STRANGER), client(STUDENT)
    tid = feed_item(st, STUDENT, STRANGER)["id"]
    st.post(f"/incoming/{tid}/return")
    assert stranger.post(f"/transfers/{tid}/complaint").json()["outcome"] == "already_returned"


def test_overdue_appeals_go_first():
    an = analyst()
    block_seller()
    sel = client(SELLER)
    cid = sel.get(f"/clients/{SELLER}/case").json()["case_id"]
    sel.post(f"/cases/{cid}/documents", json={"kind": "chat", "filename": "c.png", "content_base64": B64(PNG)})
    sel.post(f"/cases/{cid}/submit", json={"explanation": "Покупатель оплатил товар и попросил вернуть деньги."})
    queue = an.get("/analyst/cases").json()
    assert not queue[0]["overdue"]
    main.conn.execute("UPDATE cases SET due_at=? WHERE id=?", ((datetime.now() - timedelta(hours=1)).isoformat(), cid))
    main.conn.commit()
    queue = an.get("/analyst/cases").json()
    assert queue[0]["case_id"] == cid and queue[0]["overdue"]


def test_old_database_is_rebuilt(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE cases (id INTEGER PRIMARY KEY, client_id INTEGER)")
    old.commit()
    old.close()
    main.conn.close()
    main.conn = db.connect(str(path))
    assert len(TestClient(main.app).get("/demo/clients").json()) == 10


def test_quality_page_is_served_with_current_threshold():
    anon = TestClient(main.app)
    assert anon.get("/quality").status_code == 200
    q = anon.get("/quality.json").json()
    assert q["threshold"] == engine.HOLD_THRESHOLD
    cur = next(c for c in q["curve"] if c["threshold"] == q["threshold"])
    low, mean, high = cur["recall"]
    assert low <= mean <= high and mean >= 75


def test_kazakh_language_for_client_messages():
    st = client(STUDENT)
    st.cookies.set("ca_lang", "kk")
    r = send(st, DROP_CARD, 150000).json()
    texts = {x["code"]: x["text"] for x in r["reasons"]}
    assert texts["stranger_incoming"].startswith("Жақында")
    assert r["safe_return"]["to_name"] == "Бейтаныс жіберуші"
    assert send(st, MOM, 10_000_000).json()["detail"].startswith("Кідіртілген")
    names = {p["id"]: p["name"] for p in st.get("/demo/clients").json()}
    assert names[MOM] == "Анам"

    block_seller()
    sel = client(SELLER)
    sel.cookies.set("ca_lang", "kk")
    case = sel.get(f"/clients/{SELLER}/case").json()
    assert case["status_text"] == "Құжаттарыңызды күтеміз" and "40 мин" in case["summary"]
    assert case["summary"].startswith("Сіз бұрын")
    # Без cookie всё по-русски, а записи в базе и панель аналитика не зависят от языка клиента.
    assert client(SELLER).get(f"/clients/{SELLER}/case").json()["status_text"] == "Ждём ваши документы"
    card = analyst().get(f"/analyst/cases/{case['case_id']}").json()
    assert card["reasons"][0]["text"].startswith("Отправитель денег")


def test_complaint_follows_the_chain_to_the_card_that_holds_the_money():
    an = analyst()
    victim, tid = block_seller()
    queue = {c["client_id"]: c for c in an.get("/analyst/cases").json()}
    assert SELLER in queue and DROP_CARD in queue
    holder = an.get(f"/analyst/cases/{queue[DROP_CARD]['case_id']}").json()
    assert holder["outgoing"] is None and holder["hop"] == 2
    assert holder["reasons"][0]["code"] == "complaint_chain"
    assert holder["role"]["verdict"] == "organizer"
    assert [(x["hop"], x["role"]) for x in holder["chain"]] == [(0, "victim"), (1, "forwarded"), (2, "holder")]
    assert [x["current"] for x in holder["chain"][1:]] == [False, True]
    assert account(an, DROP_CARD)["status"] == "blocked"
    # Конечная карта видит понятную причину без пересылки.
    card = client(DROP_CARD).get(f"/clients/{DROP_CARD}/case").json()
    assert card["summary"].startswith("На ваш счёт поступило 195 000 ₸")
    # Цепочка короче лимита: на ней ничего больше не заблокировано.
    assert len([c for c in queue.values() if c["from_complaint"]]) == 2


def test_chain_does_not_block_accounts_that_already_returned_the_money():
    st, stranger = client(STUDENT), client(STRANGER)
    tid = feed_item(st, STUDENT, STRANGER)["id"]
    st.post(f"/incoming/{tid}/return")
    r = stranger.post(f"/transfers/{tid}/complaint").json()
    assert r["outcome"] == "already_returned"
    assert all(c["client_id"] != STUDENT for c in analyst().get("/analyst/cases").json())


# ---------- Ошибки второй ревизии ----------

def test_paused_transfer_of_a_blocked_client_cannot_be_released():
    st, an = client(STUDENT), analyst()
    r = send(st, DROP_CARD, 150000).json()
    st.post(f"/cases/{r['case_id']}/insist")
    main.conn.execute("UPDATE clients SET status='blocked' WHERE id=?", (STUDENT,))
    main.conn.commit()
    drop_before = account(an, DROP_CARD)["balance"]
    assert an.post(f"/analyst/cases/{r['case_id']}/decision", json={"decision": "approve"}).status_code == 409
    assert account(an, DROP_CARD)["balance"] == drop_before
    out = an.post(f"/analyst/cases/{r['case_id']}/decision",
                  json={"decision": "reject", "comment": "Счёт приостановлен, перевод отменён."})
    assert out.json()["status"] == "cancelled"


def test_blocked_client_cannot_move_money_by_returning():
    block_seller()
    card = client(DROP_CARD)
    inc = feed_item(card, DROP_CARD, SELLER)
    assert account(card, DROP_CARD)["balance"] >= inc["amount"]          # деньги на карте есть
    assert card.post(f"/incoming/{inc['id']}/return").status_code == 403


def test_one_approval_does_not_lift_another_pending_block():
    an = analyst()
    main.conn.execute("""INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at)
                         SELECT client_id,transfer_id,incoming_transfer_id,'block',score,reasons,created_at
                         FROM cases WHERE client_id=?""", (TRANSIT,))
    main.conn.commit()
    first, second = [c["case_id"] for c in an.get("/analyst/cases").json() if c["client_id"] == TRANSIT]
    assert an.post(f"/analyst/cases/{first}/decision", json={"decision": "approve"}).json()["client_status"] == "blocked"
    # Клиент видит ещё не решённую блокировку, а не ту, что уже сняли.
    assert client(TRANSIT).get(f"/clients/{TRANSIT}/case").json()["status"] == "open"
    assert an.post(f"/analyst/cases/{second}/decision", json={"decision": "approve"}).json()["client_status"] == "active"


def test_demo_balances_match_the_transfers():
    an = analyst()
    for cid in (STUDENT, STRANGER, DROP_CARD, MOM, SELLER, VICTIM, TRANSIT):
        assert account(an, cid)["balance"] >= 0
    assert account(an, STUDENT)["balance"] == 192000 and account(an, MOM)["balance"] == 150000
    assert account(an, DROP_CARD)["balance"] > 195000      # деньги из цепочки действительно лежат на карте


def test_times_are_kazakhstan_time_even_on_a_utc_server(monkeypatch):
    import time
    from app import clock
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        kz = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5)
        assert abs((clock.now() - kz).total_seconds()) < 5
    finally:
        monkeypatch.undo()
        time.tzset()


def test_empty_amount_gets_a_readable_error_and_complaints_are_limited():
    st = client(STUDENT)
    r = st.post("/transfers", json={"receiver_id": MOM, "amount": 0})
    assert r.status_code == 422 and r.json()["detail"].startswith("Проверьте введённые данные")
    for _ in range(main.COMPLAINT_LIMIT):
        tid = send(st, MOM, 1000).json()["transfer_id"]
        assert st.post(f"/transfers/{tid}/complaint").status_code == 200
    tid = send(st, MOM, 1000).json()["transfer_id"]
    assert st.post(f"/transfers/{tid}/complaint").status_code == 429


# ---------- Эффективность, связанные счета, выгрузка для Антифрод-центра ----------

HISTORY_DB = os.path.join(_tmp, "history.db")
_h = db.connect(HISTORY_DB)
seed.populate(_h, evaluate=True, with_history=True)
_h.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('history','1')")
_h.commit()
_h.close()


@pytest.fixture
def with_history(tmp_path):
    main.conn.close()
    shutil.copy(HISTORY_DB, tmp_path / "h.db")
    main.conn = db.connect(str(tmp_path / "h.db"))


def test_new_analyst_pages_are_closed_to_clients():
    st = client(STUDENT)
    for path in ("/analyst/metrics", "/analyst/linked"):
        assert st.get(path).status_code == 401
    assert st.post("/analyst/cases/1/report").status_code == 401
    assert st.post(f"/analyst/accounts/{DROP_CARD}/suspend").status_code == 401


def test_metrics_add_up(with_history):
    m = analyst().get("/analyst/metrics").json()
    assert m["simulated_history"] and m["stops"] > 20
    assert m["kept_count"] + m["released"] + m["pending_stops"] == m["stops"]
    assert m["saved_amount"] >= m["returned_amount"] > 0
    assert 50 < m["self_resolved_share"] < 90
    assert sum(d["stops"] for d in m["per_day"]) == m["stops"] and len(m["per_day"]) == 30


def test_metrics_follow_live_actions():
    an, st = analyst(), client(STUDENT)
    before = an.get("/analyst/metrics").json()
    r = send(st, DROP_CARD, 150000).json()
    st.post(f"/cases/{r['case_id']}/return-to-sender")
    after = an.get("/analyst/metrics").json()
    assert after["stops"] == before["stops"] + 1 and after["protected_clients"] == before["protected_clients"] + 1
    assert after["saved_amount"] == before["saved_amount"] + 150000
    assert not after["simulated_history"]


def test_linked_accounts_are_droppers_and_can_be_suspended(with_history):
    an = analyst()
    linked = an.get("/analyst/linked").json()
    assert len(linked) >= 5
    kinds = {main.conn.execute("SELECT true_kind FROM clients WHERE id=?", (a["client_id"],)).fetchone()[0] for a in linked}
    assert kinds == {"dropper"}                                    # на синтетике ни одного честного счёта
    target = next(a for a in linked if a["status"] == "active")
    case = an.post(f"/analyst/accounts/{target['client_id']}/suspend").json()
    assert case["client_status"] == "blocked" and case["reasons"][0]["code"] == "linked"
    assert case["linked"]["is_client"] and case["linked"]["sources"] == target["sources"]
    assert an.post(f"/analyst/accounts/{target['client_id']}/suspend").status_code == 409
    # Клиент видит понятную причину.
    view = an.get(f"/clients/{target['client_id']}/case").json()          # то, что увидит клиент
    assert view["summary"].startswith("На ваш счёт поступали деньги от нескольких клиентов")


def test_demo_drop_card_becomes_linked_after_the_complaint():
    an = analyst()
    assert all(a["client_id"] != DROP_CARD for a in an.get("/analyst/linked").json())
    block_seller()
    linked = {a["client_id"]: a for a in an.get("/analyst/linked").json()}
    assert linked[DROP_CARD]["sources"] == 2                        # Руслан и Данияр
    seller_case = next(c for c in an.get("/analyst/cases").json() if c["client_id"] == SELLER)
    card = an.get(f"/analyst/cases/{seller_case['case_id']}").json()
    assert card["linked"]["client_id"] == DROP_CARD and not card["linked"]["is_client"]
    assert {o["name"] for o in card["linked"]["other_cases"]} >= {"Руслан (счёту 12 дней)"}


def test_antifraud_report():
    an = analyst()
    block_seller()
    case_id = next(c["case_id"] for c in an.get("/analyst/cases").json() if c["client_id"] == SELLER)
    r = an.post(f"/analyst/cases/{case_id}/report")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rep = r.json()
    assert rep["client"]["id"] == SELLER and [o["amount_kzt"] for o in rep["operations"]] == [200000, 195000]
    assert rep["reasons"][0]["code"] == "complaint" and len(rep["complaint_chain"]) == 3
    assert set(rep["technical"]) >= {"ip", "geolocation", "phone_numbers"}
    history = an.get(f"/analyst/cases/{case_id}").json()["history"]
    assert history[-1]["action"] == "report"


# ---------- Списки Антифрод-центра и сигнал оператора о звонке ----------

def test_black_listed_recipient_is_refused():
    mom, an = client(MOM), analyst()
    assert mom.get(f"/recipients/{FAKE_INVEST}/check").json()["list"] == "black"
    r = send(mom, FAKE_INVEST, 50000)
    assert r.status_code == 403 and "подтверждённых мошенников" in r.json()["detail"]
    assert account(an, FAKE_INVEST)["balance"] == 0 and account(mom, MOM)["balance"] == 150000


def test_grey_listed_recipient_is_paused_and_can_be_withdrawn():
    mom = client(MOM)
    check = mom.get(f"/recipients/{DROP_CARD}/check").json()
    assert check["list"] == "grey" and check["text"]
    assert mom.get(f"/recipients/{STUDENT}/check").json()["list"] is None
    r = send(mom, DROP_CARD, 30000).json()
    assert r["status"] == "held" and r["kind"] == "grey" and r["safe_return"] is None
    assert "afc_grey" in {x["code"] for x in r["reasons"]}
    assert mom.post(f"/cases/{r['case_id']}/return-to-sender").status_code == 400
    assert account(mom, MOM)["held"] == 30000
    assert mom.post(f"/cases/{r['case_id']}/withdraw").json()["status"] == "withdrawn"
    acc = account(mom, MOM)
    assert acc["held"] == 0 and acc["balance"] == 150000


def test_call_signal_pauses_a_transfer_to_a_new_recipient_only():
    mom, an = client(MOM), analyst()
    assert send(mom, SAFE_ACCOUNT, 1000).json()["status"] == "done"      # без звонка обычный перевод проходит
    assert mom.post("/telecom/signal", json={"active": True}).json()["number"]
    assert account(mom, MOM)["call"] is not None
    assert send(mom, STUDENT, 5000).json()["status"] == "done"           # знакомому — можно и во время звонка
    r = send(mom, VICTIM, 100000).json()                                  # новый получатель во время звонка
    assert r["status"] == "held" and r["kind"] == "call"
    card = an.get(f"/analyst/cases/{r['case_id']}").json()
    assert card["incoming"] is None and "suspicious_call" in {x["code"] for x in card["reasons"]}
    assert [h["action"] for h in card["history"]] == ["intercepted", "call_signal", "guardian_notified"]
    # Сигнал гаснет через 15 минут или по команде.
    assert mom.post("/telecom/signal", json={"active": False}).json() is None
    assert send(mom, SAFE_ACCOUNT, 1000).json()["status"] == "done"


def test_call_signal_expires():
    mom = client(MOM)
    mom.post("/telecom/signal", json={"active": True})
    old = (main._now() - timedelta(minutes=engine.CALL_SIGNAL_MINUTES + 1)).isoformat()
    main.conn.execute("UPDATE call_signals SET started_at=?", (old,))
    main.conn.commit()
    assert account(mom, MOM)["call"] is None
    assert send(mom, VICTIM, 100000).json()["status"] == "pending"   # не остановлен; крупный первый перевод ждёт час


def test_signals_strengthen_the_scheme_reasons():
    st = client(STUDENT)
    st.post("/telecom/signal", json={"active": True})
    r = send(st, DROP_CARD, 150000).json()
    codes = {x["code"] for x in r["reasons"]}
    assert r["kind"] == "scheme" and {"afc_grey", "suspicious_call", "amount_match"} <= codes


def test_kazakh_texts_for_new_signals():
    mom = client(MOM)
    mom.cookies.set("ca_lang", "kk")
    assert send(mom, FAKE_INVEST, 50000).json()["detail"].startswith("Аударым мүмкін емес")
    assert mom.get(f"/recipients/{DROP_CARD}/check").json()["text"].startswith("Алушы Антифрод")


# ---------- Третья ревизия: обходы и защита сервера ----------

def test_small_bait_does_not_unlock_big_transfer_to_same_card():
    """Мошенник шлёт 1 000 ₸ и просит перевести на ту же карту больше: это не «возврат», проверка идёт."""
    drop, mom = client(DROP_CARD), client(MOM)
    assert send(drop, MOM, 1000).json()["status"] == "done"
    mom.post("/telecom/signal", json={"active": True})
    r = send(mom, DROP_CARD, 140000).json()
    assert r["status"] == "held"
    # Вернуть ровно присланное по-прежнему можно без остановки.
    mom.post("/telecom/signal", json={"active": False})
    assert send(mom, DROP_CARD, 1000).json()["status"] == "done"


def _appeal(cid, case_id):
    c = client(cid)
    c.post(f"/cases/{case_id}/documents", json={"kind": "chat", "filename": "a.png", "content_base64": B64(PNG)})
    assert c.post(f"/cases/{case_id}/submit", json={"explanation": "Мне написали, что деньги пришли по ошибке"}).status_code == 200
    return c


def test_kept_block_survives_approval_of_another_case():
    _, tid = block_seller()
    a = analyst()
    first = client(SELLER).get(f"/clients/{SELLER}/case").json()["case_id"]
    _appeal(SELLER, first)
    assert a.post(f"/analyst/cases/{first}/decision",
                  json={"decision": "reject", "comment": "Объяснение не подтверждено"}).status_code == 200
    main.conn.execute(
        """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at,status)
           VALUES(?,NULL,?,'block',40,'[{"code":"linked","points":40,"text":"x"}]',?,'under_review')""",
        (SELLER, tid, main._now().isoformat()))
    main.conn.commit()
    second = main.conn.execute("SELECT MAX(id) FROM cases").fetchone()[0]
    assert a.post(f"/analyst/cases/{second}/decision", json={"decision": "approve"}).status_code == 200
    assert account(client(SELLER), SELLER)["status"] == "blocked"


def test_paused_transfer_to_newly_blacklisted_receiver_cannot_be_released():
    mom = client(MOM)
    mom.post("/telecom/signal", json={"active": True})
    r = send(mom, SAFE_ACCOUNT, 100000).json()
    assert r["status"] == "held"
    assert mom.post(f"/cases/{r['case_id']}/insist").json()["status"] == "guardian"
    assert client(STUDENT).post(f"/guardian/requests/{r['case_id']}/confirm").status_code == 200
    main.conn.execute("INSERT INTO afc_list(client_id,list,added_at) VALUES(?,'black',?)",
                      (SAFE_ACCOUNT, main._now().isoformat()))
    main.conn.commit()
    a = analyst()
    assert a.post(f"/analyst/cases/{r['case_id']}/decision", json={"decision": "approve"}).status_code == 409
    assert a.post(f"/analyst/cases/{r['case_id']}/decision",
                  json={"decision": "reject", "comment": "Получатель в базе мошенников"}).status_code == 200


def test_no_return_to_blacklisted_sender():
    student = client(STUDENT)
    main.conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status) VALUES(?,?,?,?,'done')",
                      (FAKE_INVEST, STUDENT, 50000, main._now().isoformat()))
    main.conn.execute("UPDATE clients SET balance=balance+50000 WHERE id=?", (STUDENT,))
    main.conn.commit()
    tid = feed_item(student, STUDENT, FAKE_INVEST)["id"]
    assert student.post(f"/incoming/{tid}/return").status_code == 403


def test_sessions_expire_on_server():
    c = client(STUDENT)
    assert c.get("/me").status_code == 200
    for v in main.SESSIONS.values():
        v["expires"] = 0
    assert c.get("/me").status_code == 401


def test_pin_lockout_is_per_address():
    attacker = TestClient(main.app, client=("10.0.0.66", 5000))
    for _ in range(5):
        assert attacker.post("/login", json={"role": "analyst", "pin": "0000"}).status_code == 401
    assert attacker.post("/login", json={"role": "analyst", "pin": "4321"}).status_code == 429
    analyst()          # сотрудник с другого адреса входит


def test_security_headers_and_chunked_body():
    c = TestClient(main.app)
    h = c.get("/").headers
    assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]
    r = c.post("/login", content=iter([b'{"role":"client","client_id":1001}']),
               headers={"content-type": "application/json"})
    assert r.status_code == 411


# ---------- Справка, доверенное лицо, вербовка ----------

ERLAN = 1010


def _unblock_seller():
    block_seller()
    seller = client(SELLER)
    case = seller.get(f"/clients/{SELLER}/case").json()["case_id"]
    _appeal(SELLER, case)
    a = analyst()
    assert a.post(f"/analyst/cases/{case}/decision", json={"decision": "approve"}).json()["certificate"]["valid"]
    return seller, a, case


def test_certificate_is_issued_on_unblock_and_verifiable():
    seller, a, case = _unblock_seller()
    cert = seller.get(f"/clients/{SELLER}/case").json()["certificate"]
    assert cert["valid"] and cert["number"].startswith("ЧС-")
    page = seller.get(f"/clients/{SELLER}/certificate")
    assert page.status_code == 200 and cert["number"] in page.text and cert["code"] in page.text
    assert "юридической силы" in page.text
    # Другой клиент чужую справку не получит, а проверка по коду работает без входа.
    assert client(VICTIM).get(f"/clients/{SELLER}/certificate").status_code == 403
    check = TestClient(main.app).get(f"/verify/{cert['code'].lower()}")
    assert "действительна" in check.text and "маркетплейсе" not in check.text
    assert "не найдена" in TestClient(main.app).get("/verify/ZZZZ<b>").text
    # Счёт снова приостановили: справка отзывается.
    main._block_client(SELLER)
    main.conn.commit()
    assert "недействительна" in TestClient(main.app).get(f"/verify/{cert['code']}").text
    assert seller.get(f"/clients/{SELLER}/certificate").status_code == 404


def test_kept_block_gives_no_certificate():
    victim, _ = block_seller()
    case = client(SELLER).get(f"/clients/{SELLER}/case").json()["case_id"]
    _appeal(SELLER, case)
    r = analyst().post(f"/analyst/cases/{case}/decision", json={"decision": "reject", "comment": "Объяснение не подтверждено"})
    assert r.json()["certificate"] is None
    assert client(SELLER).get(f"/clients/{SELLER}/certificate").status_code == 404


def test_guardian_is_notified_and_can_cancel():
    mom = client(MOM)
    mom.post("/telecom/signal", json={"active": True})
    r = send(mom, SAFE_ACCOUNT, 100000).json()
    assert r["status"] == "held" and r["guardian"].startswith("Айдана")
    daughter = client(STUDENT)
    req = account(daughter, STUDENT)["guardian_requests"]
    assert len(req) == 1 and req[0]["ward"] == "Мама" and req[0]["amount"] == 100000
    # Подтвердить, пока мама сама не просила, нельзя; отменить можно сразу.
    assert daughter.post(f"/guardian/requests/{r['case_id']}/confirm").status_code == 409
    before = account(mom, MOM)["available"]
    assert daughter.post(f"/guardian/requests/{r['case_id']}/cancel").status_code == 200
    assert account(mom, MOM)["available"] == before + 100000
    assert account(daughter, STUDENT)["guardian_requests"] == []
    # Чужой человек отменить не может.
    r2 = send(mom, SAFE_ACCOUNT, 50000).json()
    assert client(VICTIM).post(f"/guardian/requests/{r2['case_id']}/cancel").status_code == 404
    m = analyst().get("/analyst/metrics").json()
    assert m["guardian_cancelled"] == 1


def test_insist_goes_to_guardian_before_analyst():
    mom = client(MOM)
    mom.post("/telecom/signal", json={"active": True})
    r = send(mom, SAFE_ACCOUNT, 100000).json()
    assert mom.post(f"/cases/{r['case_id']}/insist").json()["status"] == "guardian"
    a = analyst()
    assert not any(c["case_id"] == r["case_id"] for c in a.get("/analyst/cases").json() if c["needs_decision"])
    assert client(STUDENT).post(f"/guardian/requests/{r['case_id']}/confirm").status_code == 200
    card = a.get(f"/analyst/cases/{r['case_id']}").json()
    assert card["needs_decision"] and card["guardian"]["state"] == "confirmed"
    assert "guardian_confirmed" in [h["action"] for h in card["history"]]


def test_guardian_rules():
    mom = client(MOM)
    g = mom.get("/guardian").json()
    assert g["guardian"]["id"] == STUDENT
    # Сменить сразу нельзя; отключение — только через сутки.
    assert mom.post("/guardian", json={"guardian_id": VICTIM}).status_code == 409
    removing = mom.post("/guardian/remove").json()["guardian"]
    assert removing["remove_at"]
    r = (mom.post("/telecom/signal", json={"active": True}), send(mom, SAFE_ACCOUNT, 90000).json())[1]
    assert r["guardian"]                                  # пока сутки не прошли, дочь всё ещё узнаёт
    main.conn.execute("UPDATE guardians SET remove_at=? WHERE client_id=?",
                      ((main._now() - timedelta(minutes=1)).isoformat(), MOM))
    main.conn.commit()
    g = mom.get("/guardian").json()
    assert g["guardian"] is None and [c["id"] for c in g["candidates"]] == [STUDENT]
    # Получатель без давней истории переводов доверенным лицом стать не может.
    assert mom.post("/guardian", json={"guardian_id": SAFE_ACCOUNT}).status_code == 400
    assert mom.post("/guardian", json={"guardian_id": STUDENT}).json()["guardian"]["id"] == STUDENT


def test_recruitment_warning_and_return_all():
    erlan = client(ERLAN)
    acc = account(erlan, ERLAN)
    assert acc["recruit"]["senders"] == 3 and acc["recruit"]["amount"] == 143000
    before = acc["available"]
    r = erlan.post("/recruit/return-all").json()
    assert r["returned"] == 3
    acc = account(erlan, ERLAN)
    assert acc["recruit"] is None and acc["available"] == before - 143000


def test_recruitment_dismiss_is_shown_to_analyst():
    erlan = client(ERLAN)
    assert erlan.post("/recruit/dismiss").status_code == 200
    assert account(erlan, ERLAN)["recruit"] is None
    # Ерлан всё же пересылает всё одним переводом: сбор денег нескольких незнакомцев останавливается.
    r = send(erlan, DROP_CARD, 140000).json()
    assert r["status"] == "held"
    codes = [x["code"] for x in analyst().get(f"/analyst/cases/{r['case_id']}").json()["reasons"]]
    assert "pooled_forward" in codes
    card = analyst().get(f"/analyst/cases/{r['case_id']}").json()
    assert card["recruit_warned"]
    assert any("вербовк" in f["text"] for f in card["role"]["factors"])


def test_honest_seller_has_no_recruitment_warning():
    assert account(client(SELLER), SELLER)["recruit"] is None


# ---------- Проверка сообщения и период охлаждения ----------

SCAM_SAFE = "Служба безопасности банка. На ваше имя пытаются оформить кредит. Переведите деньги на безопасный счёт. Никому не сообщайте."
SCAM_MISTAKEN = "Я по ошибке перевела вам 150 000 тенге, верните срочно на карту 4400 0000 1234 5678"


def test_message_check_recognizes_schemes_and_keeps_no_text():
    mom = client(MOM)
    r = mom.post("/messages/check", json={"text": SCAM_SAFE}).json()
    assert r["verdict"] == "danger" and r["scheme"] == "safe_account" and len(r["advice"]) == 3
    assert "служба безопасности" in r["matched"]
    assert {f["code"] for f in r["flags"]} == {"secrecy"}
    for text, scheme in [("Гарантированный доход 3% в день, вложите от 50 000", "investment"),
                         ("Подработка: принимать переводы на свою карту и переводить дальше, 5% от суммы", "job"),
                         ("Банктің қауіпсіздік қызметі. Ақшаны қауіпсіз шотқа аударыңыз.", "safe_account")]:
        assert mom.post("/messages/check", json={"text": text}).json()["scheme"] == scheme
    honest = mom.post("/messages/check", json={"text": "Скинь 5000 за билеты в кино, встречаемся в 19:00"}).json()
    assert honest["verdict"] == "none" and honest["scheme"] is None
    assert mom.post("/messages/check", json={"text": "Срочно перейдите https://bonus.top"}).json()["verdict"] == "caution"
    assert mom.post("/messages/check", json={"text": "   "}).status_code == 400
    assert mom.post("/messages/check", json={"text": "x" * 4001}).status_code == 422
    # Текст сообщения не хранится: в таблице только схема и итог.
    cols = [r[1] for r in main.conn.execute("PRAGMA table_info(message_checks)")]
    assert cols == ["id", "client_id", "scheme", "verdict", "at"]


def test_mistaken_message_offers_return_of_foreign_money():
    st = client(STUDENT)
    r = st.post("/messages/check", json={"text": SCAM_MISTAKEN}).json()
    assert r["scheme"] == "mistaken" and r["return_incoming"]["amount"] == 150000
    assert st.post(f"/incoming/{r['return_incoming']['id']}/return").status_code == 200
    assert st.post("/messages/check", json={"text": SCAM_MISTAKEN}).json()["return_incoming"] is None


def test_scam_message_strengthens_next_transfer_to_new_receiver():
    mom = client(MOM)
    mom.post("/messages/check", json={"text": SCAM_SAFE})
    r = send(mom, SAFE_ACCOUNT, 50000).json()
    assert r["status"] == "held" and r["kind"] == "message"
    # Знакомому человеку перевод проходит.
    assert send(mom, STUDENT, 3000).json()["status"] == "done"


def test_big_first_transfer_waits_and_can_be_cancelled():
    mom = client(MOM)
    before = account(mom, MOM)["available"]
    r = send(mom, SAFE_ACCOUNT, 120000).json()
    assert r["status"] == "pending" and r["release_at"]
    acc = account(mom, MOM)
    assert acc["available"] == before - 120000 and acc["cooling"][0]["id"] == r["transfer_id"]
    # Получатель денег пока не видит.
    assert all(t["id"] != r["transfer_id"] for t in account(client(SAFE_ACCOUNT), SAFE_ACCOUNT)["feed"])
    assert mom.post(f"/transfers/{r['transfer_id']}/cancel").json()["status"] == "cancelled"
    assert account(mom, MOM)["available"] == before
    assert mom.post(f"/transfers/{r['transfer_id']}/cancel").status_code == 409
    assert client(STUDENT).post(f"/transfers/{r['transfer_id']}/cancel").status_code == 404


def test_cooling_transfer_goes_through_after_the_period():
    mom, safe = client(MOM), client(SAFE_ACCOUNT)
    before = account(safe, SAFE_ACCOUNT)["balance"]
    r = send(mom, SAFE_ACCOUNT, 120000).json()
    main.conn.execute("UPDATE transfers SET release_at=? WHERE id=?",
                      ((main._now() - timedelta(seconds=1)).isoformat(), r["transfer_id"]))
    main.conn.commit()
    assert account(safe, SAFE_ACCOUNT)["balance"] == before + 120000
    assert account(mom, MOM)["cooling"] == []


def test_cooling_rules():
    mom = client(MOM)
    main.conn.execute("UPDATE clients SET balance=balance+500000 WHERE id=?", (MOM,))
    main.conn.commit()
    assert send(mom, STUDENT, 100000).json()["status"] == "done"          # давно знакомый получатель
    assert send(mom, SAFE_ACCOUNT, 30000).json()["status"] == "done"      # небольшая сумма
    assert send(mom, SAFE_ACCOUNT, 80000).json()["status"] == "pending"   # части за сутки складываются


def test_cooling_stops_if_receiver_is_blacklisted_meanwhile():
    mom = client(MOM)
    before = account(mom, MOM)["available"]
    r = send(mom, SAFE_ACCOUNT, 120000).json()
    main.conn.execute("INSERT INTO afc_list(client_id,list,added_at) VALUES(?,'black',?)",
                      (SAFE_ACCOUNT, main._now().isoformat()))
    main.conn.execute("UPDATE transfers SET release_at=? WHERE id=?",
                      ((main._now() - timedelta(seconds=1)).isoformat(), r["transfer_id"]))
    main.conn.commit()
    assert account(mom, MOM)["available"] == before


def test_guardian_can_cancel_cooling_transfer():
    mom, daughter = client(MOM), client(STUDENT)
    r = send(mom, SAFE_ACCOUNT, 120000).json()
    g = account(daughter, STUDENT)["guardian_cooling"]
    assert len(g) == 1 and g[0]["sender"] == "Мама"
    assert client(VICTIM).post(f"/guardian/transfers/{r['transfer_id']}/cancel").status_code == 404
    assert daughter.post(f"/guardian/transfers/{r['transfer_id']}/cancel").status_code == 200
    m = analyst().get("/analyst/metrics").json()
    assert m["cooling"] == 1 and m["cooling_cancelled"] == 1 and m["cooling_saved"] == 120000


def test_returning_foreign_money_by_hand_is_not_delayed():
    st = client(STUDENT)
    assert send(st, STRANGER, 150000).json()["status"] == "done"

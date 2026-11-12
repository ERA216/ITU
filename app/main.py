"""API демо-банка.  Запуск:  uvicorn app.main:app --reload"""
import base64
import json
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import db, engine, seed

conn = db.connect()
STATIC = os.path.join(os.path.dirname(__file__), "static")
UPLOADS = os.environ.get("CA_UPLOADS", os.path.join(os.path.dirname(__file__), "..", "uploads"))
ANALYST_PIN = os.environ.get("CA_ANALYST_PIN", "2026")   # для показа; в реальной системе вход сотрудника банка
MAX_FILE = 5 * 1024 * 1024
MAX_BODY = 8 * 1024 * 1024      # файл 5 МБ в base64 занимает около 6,7 МБ
MAX_DOCS = 6                    # файлов на одну апелляцию
DEMO_TTL_HOURS = 48             # демо-данные старше этого пересоздаются: иначе «свежий» входящий устаревает
FILE_TYPES = [(b"\x89PNG\r\n\x1a\n", ".png", "image/png"), (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
              (b"%PDF-", ".pdf", "application/pdf")]

# Какие документы просим при блокировке из-за пересылки чужих денег.
DOC_KINDS = {
    "chat": {"title": "Переписка с тем, кто просил переслать деньги", "required": True},
    "deal": {"title": "Объявление, договор или чек, если деньги были оплатой товара", "required": False},
    "other": {"title": "Другое подтверждение", "required": False},
}
STATUS_TEXT = {
    "open": "Ждём ваши документы",
    "under_review": "На рассмотрении у сотрудника банка",
    "unblocked": "Ограничения сняты",
    "kept": "Ограничения оставлены",
    "returned": "Деньги возвращены отправителю",
    "released": "Перевод разрешён",
    "cancelled": "Перевод отменён",
}
# Те же статусы словами для сотрудника банка.
ANALYST_STATUS = {
    ("block", "open"): "Апелляция не подана",
    ("intercept", "open"): "Клиент ещё не выбрал действие",
    ("block", "under_review"): "Ждёт решения",
    ("intercept", "under_review"): "Ждёт решения",
}

LOCK = threading.Lock()
SESSIONS: dict[str, dict] = {}
_pin_failures: list[float] = []


def tx():
    """Один запрос — одна транзакция: при любой ошибке все изменения откатываются."""
    with LOCK:
        try:
            yield
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


app = FastAPI(title="Чистый счёт: защита от статуса дроппера", dependencies=[Depends(tx, scope="function")])


@app.middleware("http")
async def guard(request: Request, call_next):
    size = request.headers.get("content-length")
    if size and size.isdigit() and int(size) > MAX_BODY:
        return JSONResponse({"detail": "Запрос слишком большой"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


class LoginIn(BaseModel):
    role: str                     # client | analyst
    client_id: int | None = None
    pin: str = Field("", max_length=64)


class TransferIn(BaseModel):
    receiver_id: int
    amount: int = Field(gt=0, le=100_000_000)


class DocumentIn(BaseModel):
    kind: str
    filename: str = Field(max_length=255)
    content_base64: str = Field(max_length=7_200_000)


class AppealIn(BaseModel):
    explanation: str = Field(max_length=5000)


class DecisionIn(BaseModel):
    decision: str   # approve | reject
    comment: str = Field("", max_length=2000)


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₸"


# ---------- Демо-данные ----------

def ensure_demo(force: bool = False) -> bool:
    """Создаёт данные при первом запуске и пересоздаёт устаревшие."""
    row = conn.execute("SELECT value FROM meta WHERE key='seeded_at'").fetchone()
    stale = row is None or _now() - datetime.fromisoformat(row["value"]) > timedelta(hours=DEMO_TTL_HOURS)
    if not (force or stale):
        return False
    seed.populate(conn, evaluate=False, reset=True)
    shutil.rmtree(UPLOADS, ignore_errors=True)
    return True


ensure_demo()


# ---------- Вход и права ----------

def session(ca_client: str | None = Cookie(None), ca_analyst: str | None = Cookie(None)) -> dict:
    """Сессии клиента и сотрудника независимы: экран клиента и панель можно держать открытыми рядом."""
    s = {"client": SESSIONS.get(ca_client or ""), "analyst": SESSIONS.get(ca_analyst or "")}
    if not (s["client"] or s["analyst"]):
        raise HTTPException(401, "Войдите в систему")
    return s


def as_client(s: dict = Depends(session)) -> dict:
    if not s["client"]:
        raise HTTPException(401, "Войдите как клиент")
    return s["client"]


def as_analyst(s: dict = Depends(session)) -> dict:
    if not s["analyst"]:
        raise HTTPException(401, "Войдите как сотрудник банка")
    return s["analyst"]


def _client(cid: int):
    row = conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Клиент не найден")
    return row


def _own_client(s: dict, cid: int):
    """Клиент видит только свой счёт; сотрудник банка видит любой."""
    if not s["analyst"] and s["client"]["client_id"] != cid:
        raise HTTPException(403, "Это не ваш счёт")
    return _client(cid)


def _own_case(client: dict, case_id: int, kind: str):
    case = conn.execute("SELECT * FROM cases WHERE id=? AND kind=?", (case_id, kind)).fetchone()
    if not case or case["client_id"] != client["client_id"]:
        raise HTTPException(404, "Случай не найден")   # чужой случай неотличим от несуществующего
    return case


@app.get("/demo/clients")
def demo_clients():
    """Персонажи для показа: под ними входит экран клиента."""
    ensure_demo()
    rows = conn.execute("SELECT id, name FROM clients WHERE id >= 1001 ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.post("/login")
def login(body: LoginIn, response: Response):
    """Демо-вход. Клиент выбирает персонажа, сотрудник банка вводит код доступа."""
    if body.role == "analyst":
        now = time.time()
        _pin_failures[:] = [t for t in _pin_failures if now - t < 60]
        if len(_pin_failures) >= 5:
            raise HTTPException(429, "Слишком много попыток. Подождите минуту")
        if not secrets.compare_digest(body.pin.encode(), ANALYST_PIN.encode()):
            _pin_failures.append(now)
            raise HTTPException(401, "Неверный код доступа")
        s = {"role": "analyst", "name": "Сотрудник банка"}
    elif body.role == "client":
        c = _client(body.client_id or 0)
        if c["id"] < 1001:
            raise HTTPException(403, "Войти можно только под демо-персонажем")
        s = {"role": "client", "client_id": c["id"], "name": c["name"]}
    else:
        raise HTTPException(400, "Неизвестная роль")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = s
    response.set_cookie(f"ca_{body.role}", token, httponly=True, samesite="strict", max_age=8 * 3600)
    return s


@app.post("/logout")
def logout(response: Response, role: str = "client",
           ca_client: str | None = Cookie(None), ca_analyst: str | None = Cookie(None)):
    if role not in ("client", "analyst"):
        raise HTTPException(400, "Неизвестная роль")
    SESSIONS.pop((ca_analyst if role == "analyst" else ca_client) or "", None)
    response.delete_cookie(f"ca_{role}")
    return {"ok": True}


@app.get("/me")
def me(s: dict = Depends(session)):
    return s


# ---------- Экран клиента: счёт и переводы ----------

@app.get("/", include_in_schema=False)
def client_app():
    return FileResponse(os.path.join(STATIC, "index.html"))


def _held_sum(cid: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM transfers WHERE sender_id=? AND status='held'",
                        (cid,)).fetchone()["s"]


def _move(sender_id: int, receiver_id: int, amount: int):
    conn.execute("UPDATE clients SET balance = balance - ? WHERE id=?", (amount, sender_id))
    conn.execute("UPDATE clients SET balance = balance + ? WHERE id=?", (amount, receiver_id))


@app.get("/clients/{cid}")
def client_feed(cid: int, s: dict = Depends(session)):
    """Профиль и лента операций клиента."""
    c = _own_client(s, cid)
    rows = conn.execute(
        """SELECT t.id, t.amount, t.created_at, t.status, t.return_of,
                  t.sender_id, s.name AS sender, t.receiver_id, r.name AS receiver,
                  k.id AS case_id, k.status AS case_status, k.decision_comment
           FROM transfers t JOIN clients s ON s.id=t.sender_id JOIN clients r ON r.id=t.receiver_id
           LEFT JOIN cases k ON k.transfer_id=t.id AND k.kind='intercept'
           WHERE t.sender_id=? OR t.receiver_id=? ORDER BY t.created_at DESC, t.id DESC LIMIT 30""",
        (cid, cid)).fetchall()
    held = _held_sum(cid)
    return {"id": c["id"], "name": c["name"], "balance": c["balance"], "held": held,
            "available": c["balance"] - held, "status": c["status"],
            "feed": [dict(r) | {"direction": "in" if r["receiver_id"] == cid else "out"} for r in rows]}


def _intercept_view(case) -> dict:
    inc = conn.execute(
        """SELECT t.id, t.amount, t.sender_id, c.name FROM transfers t
           JOIN clients c ON c.id=t.sender_id WHERE t.id=?""", (case["incoming_transfer_id"],)).fetchone()
    tr = conn.execute(
        """SELECT t.amount, c.name FROM transfers t JOIN clients c ON c.id=t.receiver_id WHERE t.id=?""",
        (case["transfer_id"],)).fetchone()
    return {"case_id": case["id"], "transfer_id": case["transfer_id"], "status": "held",
            "case_status": case["status"], "score": case["score"], "reasons": json.loads(case["reasons"]),
            "amount": tr["amount"], "receiver": tr["name"],
            "safe_return": {"to_id": inc["sender_id"], "to_name": inc["name"], "amount": inc["amount"]}}


@app.post("/transfers")
def create_transfer(body: TransferIn, s: dict = Depends(as_client)):
    """Создаёт перевод от имени вошедшего клиента. Если движок видит схему, перевод останавливается."""
    sender_id = s["client_id"]
    sender = _client(sender_id)
    _client(body.receiver_id)
    if sender_id == body.receiver_id:
        raise HTTPException(400, "Нельзя перевести самому себе")
    if sender["status"] == "blocked":
        raise HTTPException(403, "Операции по счёту приостановлены")
    now = _now()
    res = engine.evaluate(conn, sender_id, body.receiver_id, body.amount, now)
    # На один «чужой» входящий существует не больше одного незакрытого случая.
    prev = None
    if res["decision"] == "hold":
        prev = conn.execute(
            """SELECT * FROM cases WHERE client_id=? AND incoming_transfer_id=? AND kind='intercept'
               AND status IN ('open','under_review')""", (sender_id, res["incoming_id"])).fetchone()
        if prev and prev["status"] == "under_review":
            raise HTTPException(409, "Перевод этих денег уже проверяет сотрудник банка. Дождитесь решения")
        if prev:   # новая попытка заменяет прежнюю, её резерв освобождается
            conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (prev["transfer_id"],))
    if sender["balance"] - _held_sum(sender_id) < body.amount:
        raise HTTPException(400, "Недостаточно средств с учётом переводов на паузе")

    if res["decision"] != "hold":
        cur = conn.execute(
            "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status) VALUES(?,?,?,?,'done')",
            (sender_id, body.receiver_id, body.amount, now.isoformat()))
        _move(sender_id, body.receiver_id, body.amount)
        return {"transfer_id": cur.lastrowid, "status": "done", "score": res["score"], "reasons": res["reasons"]}

    cur = conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status) VALUES(?,?,?,?,'held')",
        (sender_id, body.receiver_id, body.amount, now.isoformat()))
    reasons = json.dumps(res["reasons"], ensure_ascii=False)
    if prev:
        conn.execute("UPDATE cases SET transfer_id=?, score=?, reasons=? WHERE id=?",
                     (cur.lastrowid, res["score"], reasons, prev["id"]))
        case_id = prev["id"]
    else:
        case_id = conn.execute(
            """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (sender_id, cur.lastrowid, res["incoming_id"], "intercept", res["score"], reasons,
             now.isoformat())).lastrowid
    return _intercept_view(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())


@app.get("/cases/{case_id}")
def intercept_case(case_id: int, s: dict = Depends(as_client)):
    """Остановленный перевод: клиент может вернуться к выбору позже."""
    return _intercept_view(_own_case(s, case_id, "intercept"))


@app.post("/cases/{case_id}/return-to-sender")
def return_to_sender(case_id: int, s: dict = Depends(as_client)):
    """Безопасный возврат: деньги уходят только на счёт, с которого пришли, и только один раз."""
    case = _own_case(s, case_id, "intercept")
    if case["status"] != "open":
        raise HTTPException(409, "По этому случаю решение уже принято")
    inc = conn.execute("SELECT * FROM transfers WHERE id=?", (case["incoming_transfer_id"],)).fetchone()
    if conn.execute("SELECT 1 FROM transfers WHERE return_of=? AND status='done'", (inc["id"],)).fetchone():
        raise HTTPException(409, "Эти деньги уже возвращены отправителю")
    conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (case["transfer_id"],))
    available = _client(case["client_id"])["balance"] - _held_sum(case["client_id"])
    if available < inc["amount"]:
        raise HTTPException(400, f"На счёте доступно {_money(available)}, а вернуть нужно {_money(inc['amount'])}. "
                                 "Пополните счёт или обратитесь в банк")
    cur = conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,return_of) VALUES(?,?,?,?,?,?)",
        (case["client_id"], inc["sender_id"], inc["amount"], _now().isoformat(), "done", inc["id"]))
    _move(case["client_id"], inc["sender_id"], inc["amount"])
    conn.execute("UPDATE cases SET status='returned' WHERE id=?", (case_id,))
    return {"status": "returned", "return_transfer_id": cur.lastrowid, "amount": inc["amount"]}


@app.post("/cases/{case_id}/insist")
def insist(case_id: int, s: dict = Depends(as_client)):
    """Клиент настаивает на переводе: деньги остаются на паузе, случай уходит аналитику."""
    case = _own_case(s, case_id, "intercept")
    if case["status"] != "open":
        raise HTTPException(409, "По этому случаю решение уже принято")
    conn.execute("UPDATE cases SET status='under_review' WHERE id=?", (case_id,))
    return {"status": "under_review"}


# ---------- Апелляция: клиент заблокирован и объясняет, что произошло ----------

def _minutes_text(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60} мин" if minutes < 1440 else f"{minutes // 1440} дн"


def _documents(case_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, kind, filename, size, uploaded_at FROM documents WHERE case_id=? ORDER BY id",
                        (case_id,)).fetchall()
    return [dict(d) | {"title": DOC_KINDS.get(d["kind"], {}).get("title", d["kind"])} for d in rows]


def _case_view(case) -> dict:
    inc = conn.execute("SELECT amount, created_at FROM transfers WHERE id=?",
                       (case["incoming_transfer_id"],)).fetchone()
    out = conn.execute("SELECT amount, created_at FROM transfers WHERE id=?", (case["transfer_id"],)).fetchone()
    gap = int((datetime.fromisoformat(out["created_at"]) - datetime.fromisoformat(inc["created_at"])).total_seconds() // 60)
    docs = _documents(case["id"])
    have = {d["kind"] for d in docs}
    return {
        "case_id": case["id"],
        "status": case["status"],
        "status_text": STATUS_TEXT.get(case["status"], case["status"]),
        "summary": (f"Вы получили {_money(inc['amount'])} от человека, с которым раньше не было переводов, "
                    f"и через {_minutes_text(gap)} переслали {_money(out['amount'])} третьему лицу. "
                    "Так обычно выводят украденные деньги, поэтому банк приостановил операции "
                    "до выяснения."),
        "reasons": json.loads(case["reasons"]),
        "explanation": case["explanation"],
        "decision_comment": case["decision_comment"],
        "required": [{"kind": k, **v, "uploaded": k in have} for k, v in DOC_KINDS.items()],
        "documents": docs,
        "max_documents": MAX_DOCS,
        "can_submit": case["status"] == "open",
    }


@app.get("/clients/{cid}/case")
def client_case(cid: int, s: dict = Depends(session)):
    """Последний случай блокировки клиента: причина простым языком, документы, статус."""
    _own_client(s, cid)
    case = conn.execute("SELECT * FROM cases WHERE client_id=? AND kind='block' ORDER BY id DESC LIMIT 1",
                        (cid,)).fetchone()
    if not case:
        raise HTTPException(404, "Блокировок нет")
    return _case_view(case)


@app.post("/cases/{case_id}/documents")
def upload_document(case_id: int, body: DocumentIn, s: dict = Depends(as_client)):
    """Клиент прикладывает документ к апелляции: только PNG, JPEG или PDF до 5 МБ."""
    case = _own_case(s, case_id, "block")
    if case["status"] != "open":
        raise HTTPException(409, "Документы уже отправлены на рассмотрение")
    if body.kind not in DOC_KINDS:
        raise HTTPException(400, "Неизвестный тип документа")
    if conn.execute("SELECT COUNT(*) AS n FROM documents WHERE case_id=?", (case_id,)).fetchone()["n"] >= MAX_DOCS:
        raise HTTPException(400, f"К апелляции можно приложить не больше {MAX_DOCS} файлов")
    try:
        data = base64.b64decode(body.content_base64, validate=True)
    except Exception:
        raise HTTPException(400, "Файл повреждён")
    if not data or len(data) > MAX_FILE:
        raise HTTPException(400, "Файл пустой или больше 5 МБ")
    kind = next(((ext, media) for sig, ext, media in FILE_TYPES if data.startswith(sig)), None)
    if not kind:
        raise HTTPException(400, "Принимаются только изображения PNG, JPEG и файлы PDF")
    # Имя и расширение задаёт сервер по содержимому файла, а не по тому, что прислал клиент.
    stem = re.sub(r"[^\w\- ]", "_", os.path.splitext(os.path.basename(body.filename))[0])[:60] or "file"
    name = stem + kind[0]
    cur = conn.execute("INSERT INTO documents(case_id,kind,filename,size,media_type,uploaded_at) VALUES(?,?,?,?,?,?)",
                       (case_id, body.kind, name, len(data), kind[1], _now().isoformat()))
    os.makedirs(UPLOADS, exist_ok=True)
    path = os.path.join(UPLOADS, f"{cur.lastrowid}{kind[0]}")
    with open(path, "wb") as f:
        f.write(data)
    conn.execute("UPDATE documents SET path=? WHERE id=?", (path, cur.lastrowid))
    return {"id": cur.lastrowid, "kind": body.kind, "filename": name, "size": len(data)}


@app.post("/cases/{case_id}/submit")
def submit_appeal(case_id: int, body: AppealIn, s: dict = Depends(as_client)):
    """Клиент отправляет апелляцию: объяснение и обязательные документы."""
    case = _own_case(s, case_id, "block")
    if case["status"] != "open":
        raise HTTPException(409, "Апелляция уже отправлена")
    text = body.explanation.strip()
    if len(text) < 20:
        raise HTTPException(400, "Опишите, что произошло, хотя бы в двух предложениях")
    have = {r["kind"] for r in conn.execute("SELECT kind FROM documents WHERE case_id=?", (case_id,))}
    missing = [v["title"] for k, v in DOC_KINDS.items() if v["required"] and k not in have]
    if missing:
        raise HTTPException(400, "Не хватает документа: " + "; ".join(missing))
    conn.execute("UPDATE cases SET status='under_review', explanation=? WHERE id=?", (text[:2000], case_id))
    return _case_view(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())


@app.get("/documents/{doc_id}")
def get_document(doc_id: int, s: dict = Depends(session)):
    """Документ видят только его владелец и сотрудник банка. Отдаётся как вложение."""
    doc = conn.execute(
        "SELECT d.*, k.client_id FROM documents d JOIN cases k ON k.id=d.case_id WHERE d.id=?", (doc_id,)).fetchone()
    if not doc or (not s["analyst"] and doc["client_id"] != s["client"]["client_id"]):
        raise HTTPException(404, "Документ не найден")
    if not doc["path"] or not os.path.exists(doc["path"]):
        raise HTTPException(404, "Документ не найден")
    return FileResponse(doc["path"], filename=doc["filename"], media_type=doc["media_type"])


# ---------- Панель аналитика ----------

@app.get("/analyst", include_in_schema=False)
def analyst_app():
    return FileResponse(os.path.join(STATIC, "analyst.html"))


def _transfer_view(tid: int) -> dict:
    t = conn.execute(
        """SELECT t.id, t.amount, t.created_at, t.status, s.name AS sender, r.name AS receiver,
                  r.account_opened_at AS receiver_opened
           FROM transfers t JOIN clients s ON s.id=t.sender_id JOIN clients r ON r.id=t.receiver_id
           WHERE t.id=?""", (tid,)).fetchone()
    return dict(t)


def _pending(case) -> bool:
    return case["status"] == "under_review" or (case["kind"] == "block" and case["status"] == "open")


def _analyst_row(case) -> dict:
    client = _client(case["client_id"])
    return {
        "case_id": case["id"], "kind": case["kind"], "status": case["status"],
        "status_text": ANALYST_STATUS.get((case["kind"], case["status"]),
                                          STATUS_TEXT.get(case["status"], case["status"])),
        "client_id": client["id"], "client": client["name"], "score": case["score"],
        "created_at": case["created_at"],
        "role": engine.assess_role(conn, case["client_id"], case, _now()),
        "needs_decision": _pending(case),
    }


@app.get("/analyst/cases")
def analyst_queue(s: dict = Depends(as_analyst)):
    """Очередь: сначала случаи, которые ждут решения."""
    rows = [_analyst_row(c) for c in conn.execute("SELECT * FROM cases ORDER BY created_at DESC, id DESC").fetchall()]
    order = {"under_review": 0, "open": 1}
    return sorted(rows, key=lambda r: (not r["needs_decision"], order.get(r["status"], 2)))


def _analyst_case(case_id: int) -> dict:
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Случай не найден")
    client = _client(case["client_id"])
    return _analyst_row(case) | {
        "account_opened_at": client["account_opened_at"],
        "client_status": client["status"],
        "incoming": _transfer_view(case["incoming_transfer_id"]),
        "outgoing": _transfer_view(case["transfer_id"]),
        "reasons": json.loads(case["reasons"]),
        "explanation": case["explanation"],
        "documents": _documents(case_id),
        "decision_comment": case["decision_comment"],
        "decided_at": case["decided_at"],
    }


@app.get("/analyst/cases/{case_id}")
def analyst_case(case_id: int, s: dict = Depends(as_analyst)):
    """Карточка случая: цепочка переводов, признаки, подсказка о роли клиента, апелляция."""
    return _analyst_case(case_id)


@app.post("/analyst/cases/{case_id}/decision")
def decide(case_id: int, body: DecisionIn, s: dict = Depends(as_analyst)):
    """Решение аналитика. Блокировка: снять или оставить. Перевод на паузе: разрешить или отменить."""
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Случай не найден")
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "Решение должно быть approve или reject")
    if not _pending(case):
        raise HTTPException(409, "По этому случаю решение уже принято")
    comment = body.comment.strip()[:1000]
    if body.decision == "reject" and len(comment) < 10:
        raise HTTPException(400, "При отказе напишите клиенту причину")
    if case["kind"] == "block":
        status = "unblocked" if body.decision == "approve" else "kept"
        if status == "unblocked":
            conn.execute("UPDATE clients SET status='active' WHERE id=?", (case["client_id"],))
    else:
        tr = conn.execute("SELECT * FROM transfers WHERE id=?", (case["transfer_id"],)).fetchone()
        if body.decision == "approve":
            # Деньги под этот перевод были зарезервированы, пока он стоял на паузе.
            conn.execute("UPDATE transfers SET status='done' WHERE id=?", (tr["id"],))
            _move(tr["sender_id"], tr["receiver_id"], tr["amount"])
            status = "released"
        else:
            conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (tr["id"],))
            status = "cancelled"
    conn.execute("UPDATE cases SET status=?, decision_comment=?, decided_at=? WHERE id=?",
                 (status, comment or None, _now().isoformat(), case_id))
    return _analyst_case(case_id)


@app.post("/demo/reset")
def demo_reset(s: dict = Depends(as_analyst)):
    """Возвращает демо-данные в исходное состояние: даты отсчитываются от текущего момента."""
    ensure_demo(force=True)
    for token in [t for t, v in SESSIONS.items() if v["role"] == "client"]:
        SESSIONS.pop(token)
    return {"ok": True}

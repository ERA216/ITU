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
from html import escape
from urllib.parse import quote

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response as RawResponse
from pydantic import BaseModel, Field

from . import clock, db, engine, i18n, messages, metrics, seed, vault
from .i18n import t

conn = db.connect()
STATIC = os.path.join(os.path.dirname(__file__), "static")
UPLOADS = os.environ.get("CA_UPLOADS", os.path.join(os.path.dirname(__file__), "..", "uploads"))
ANALYST_PIN = os.environ.get("CA_ANALYST_PIN", "")      # для показа; в реальной системе вход сотрудника банка
if not ANALYST_PIN:
    # На открытом хостинге код из README знает любой: там без заданного кода сервер придумывает свой.
    ANALYST_PIN = secrets.token_hex(4) if os.environ.get("CA_PUBLIC") == "1" else "2026"
    print(f"[Чистый счёт] Код доступа аналитика: {ANALYST_PIN}  (задайте свой в CA_ANALYST_PIN)", flush=True)
SESSION_HOURS = 8
MAX_SESSIONS = 5000
PIN_TRIES_PER_IP = 5            # неверных кодов в минуту с одного адреса
PIN_TRIES_TOTAL = 30            # и со всех адресов вместе: от подбора с многих адресов
MAX_FILE = 5 * 1024 * 1024
MAX_BODY = 8 * 1024 * 1024      # файл 5 МБ в base64 занимает около 6,7 МБ
MAX_DOCS = 6                    # файлов на одну апелляцию
DEMO_HISTORY = os.environ.get("CA_DEMO_HISTORY", "1") == "1"   # история за 30 дней для панели эффективности
DEMO_TTL_HOURS = 48             # демо-данные старше этого пересоздаются: иначе «свежий» входящий устаревает
APPEAL_SLA_DAYS = 3             # за сколько дней банк обещает ответить на апелляцию
WARN_MIN_AMOUNT = 20_000        # входящие от незнакомцев от этой суммы получают предупреждение
COMPLAINT_DAYS = 30             # на перевод можно пожаловаться в течение стольких дней
COMPLAINT_LIMIT = 3             # жалоб от одного клиента за COMPLAINT_DAYS: защита от блокировок по ложным жалобам
CHAIN_HOPS = 3                  # на сколько звеньев жалоба проходит по цепочке переводов
GUARDIAN_KNOWN_DAYS = 30        # доверенное лицо: человек, с которым переводы идут дольше этого срока
GUARDIAN_REMOVE_HOURS = 24      # отключение доверенного лица вступает в силу через сутки
COOLING_MIN = 100_000           # крупный перевод новому получателю уходит не сразу
COOLING_MINUTES = int(os.environ.get("CA_COOLING_MINUTES", "60"))
COOLING_KNOWN_HOURS = 24        # получатель «знакомый», если переводы с ним были раньше этого срока
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
    "withdrawn": "Клиент отменил перевод",
    "guardian_cancelled": "Перевод отменило доверенное лицо",
}
EVENT_TEXT = {
    "blocked": "Операции по счёту приостановлены",
    "complaint": "Поступила жалоба отправителя",
    "intercepted": "Перевод остановлен системой",
    "returned": "Клиент вернул деньги отправителю",
    "insisted": "Клиент настаивает на переводе",
    "document": "Клиент приложил документ",
    "appeal": "Клиент подал апелляцию",
    "decision": "Решение сотрудника банка",
    "withdrawn": "Клиент отменил перевод",
    "call_signal": "Сигнал оператора: клиенту звонят с подозрительного номера",
    "report": "Сведения выгружены для Антифрод-центра",
    "guardian_notified": "Доверенному лицу сообщили об остановке перевода",
    "guardian_asked": "Клиент настаивает: перевод ждёт подтверждения доверенного лица",
    "guardian_confirmed": "Доверенное лицо подтвердило перевод, решение за сотрудником",
    "guardian_cancelled": "Доверенное лицо отменило перевод",
    "cleared": "Выдана справка о снятии ограничений",
    "recruit_dismissed": "Клиент ответил на предупреждение о вербовке: знает отправителей",
    "refunded": "Деньги возвращены пострадавшему",
    "escalated": "Сотрудник приостановил счёт полностью",
    "self_report": "Клиент сам сообщил банку, что его втянули в схему",
}
UNRESOLVED = ("open", "under_review", "kept")      # блокировка действует, пока случай в одном из этих статусов
SELF_REPORT_KINDS = {
    "card": "Передал карту, её данные или доступ к приложению другому человеку",
    "transit": "Принимал чужие деньги и пересылал их по чьей-то просьбе",
    "offer": "Предложили такую «подработку», но клиент ещё ничего не делал",
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
_pin_failures: list[tuple[float, str]] = []
# Защита страниц: не встраивать в чужие сайты, скрипты и запросы только со своего адреса.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'self'"),
}


def tx():
    """Один запрос — одна транзакция: при любой ошибке все изменения откатываются."""
    with LOCK:
        try:
            _settle_due()       # переводы, у которых закончился период охлаждения, уходят получателю
            yield
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


app = FastAPI(title="Чистый счёт: защита от статуса дроппера", dependencies=[Depends(tx, scope="function")])


@app.exception_handler(RequestValidationError)
async def bad_input(request: Request, exc: RequestValidationError):
    """Вместо технического списка ошибок — фраза, которую можно показать клиенту."""
    return JSONResponse({"detail": t("bad_input")}, status_code=422)


@app.middleware("http")
async def guard(request: Request, call_next):
    size = request.headers.get("content-length")
    lang = request.cookies.get("ca_lang", "ru")
    i18n.lang.set(lang if lang in i18n.LANGS else "ru")
    if size and size.isdigit() and int(size) > MAX_BODY:
        return JSONResponse({"detail": t("too_big")}, status_code=413)
    if "transfer-encoding" in request.headers:
        # Тело без указанной длины обошло бы проверку размера выше: такие запросы не принимаем.
        return JSONResponse({"detail": t("too_big")}, status_code=411)
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
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
    return clock.now()


def _money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₸"


def log(actor: str, action: str, case_id: int | None = None, transfer_id: int | None = None, **details):
    """Журнал действий: кто, что и когда сделал по случаю. Пишется в той же транзакции, что и само действие."""
    conn.execute("INSERT INTO events(at,actor,action,case_id,transfer_id,details) VALUES(?,?,?,?,?,?)",
                 (_now().isoformat(), actor, action, case_id, transfer_id,
                  json.dumps(details, ensure_ascii=False) if details else None))


# ---------- Демо-данные ----------

def ensure_demo(force: bool = False) -> bool:
    """Создаёт данные при первом запуске и пересоздаёт устаревшие."""
    row = conn.execute("SELECT value FROM meta WHERE key='seeded_at'").fetchone()
    stale = row is None or _now() - datetime.fromisoformat(row["value"]) > timedelta(hours=DEMO_TTL_HOURS)
    if not (force or stale):
        return False
    seed.populate(conn, evaluate=DEMO_HISTORY, with_history=DEMO_HISTORY, reset=True)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('history',?)", ("1" if DEMO_HISTORY else "0",))
    shutil.rmtree(UPLOADS, ignore_errors=True)
    return True


ensure_demo()


# ---------- Вход и права ----------

def session(ca_client: str | None = Cookie(None), ca_analyst: str | None = Cookie(None)) -> dict:
    """Сессии клиента и сотрудника независимы: экран клиента и панель можно держать открытыми рядом."""
    now = time.time()
    s = {role: v if v and v["expires"] > now else None
         for role, v in (("client", SESSIONS.get(ca_client or "")), ("analyst", SESSIONS.get(ca_analyst or "")))}
    if not (s["client"] or s["analyst"]):
        raise HTTPException(401, t("login"))
    return s


def as_client(s: dict = Depends(session)) -> dict:
    if not s["client"]:
        raise HTTPException(401, t("login_client"))
    return s["client"]


def as_analyst(s: dict = Depends(session)) -> dict:
    if not s["analyst"]:
        raise HTTPException(401, "Войдите как сотрудник банка")
    return s["analyst"]


def _client(cid: int):
    row = conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, t("client_not_found"))
    return row


def _own_client(s: dict, cid: int):
    """Клиент видит только свой счёт; сотрудник банка видит любой."""
    if not s["analyst"] and s["client"]["client_id"] != cid:
        raise HTTPException(403, t("not_yours"))
    return _client(cid)


def _own_case(client: dict, case_id: int, kind: str):
    case = conn.execute("SELECT * FROM cases WHERE id=? AND kind=?", (case_id, kind)).fetchone()
    if not case or case["client_id"] != client["client_id"]:
        raise HTTPException(404, t("case_not_found"))   # чужой случай неотличим от несуществующего
    return case


@app.get("/demo/clients")
def demo_clients():
    """Персонажи для показа: под ними входит экран клиента."""
    ensure_demo()
    rows = conn.execute("SELECT id, name FROM clients WHERE id >= 1001 ORDER BY id").fetchall()
    return [{"id": r["id"], "name": i18n.name(r["id"], r["name"])} for r in rows]


@app.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    """Демо-вход. Клиент выбирает персонажа, сотрудник банка вводит код доступа."""
    now = time.time()
    if body.role == "analyst":
        ip = request.client.host if request.client else "?"
        _pin_failures[:] = [f for f in _pin_failures if now - f[0] < 60]
        # Лимит на адрес: чужие ошибки не запирают вход сотруднику. Общий лимит выше: от подбора с многих адресов.
        if sum(f[1] == ip for f in _pin_failures) >= PIN_TRIES_PER_IP or len(_pin_failures) >= PIN_TRIES_TOTAL:
            raise HTTPException(429, "Слишком много попыток. Подождите минуту")
        if not secrets.compare_digest(body.pin.encode(), ANALYST_PIN.encode()):
            _pin_failures.append((now, ip))
            raise HTTPException(401, "Неверный код доступа")
        s = {"role": "analyst", "name": "Сотрудник банка"}
    elif body.role == "client":
        c = _client(body.client_id or 0)
        if c["id"] < 1001:
            raise HTTPException(403, "Войти можно только под демо-персонажем")
        s = {"role": "client", "client_id": c["id"], "name": c["name"]}
    else:
        raise HTTPException(400, "Неизвестная роль")
    for old in [k for k, v in SESSIONS.items() if v["expires"] <= now]:
        SESSIONS.pop(old)
    while len(SESSIONS) >= MAX_SESSIONS:            # память сервера не растёт от бесконечных входов
        SESSIONS.pop(next(iter(SESSIONS)))
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = s | {"expires": now + SESSION_HOURS * 3600}
    response.set_cookie(f"ca_{body.role}", token, httponly=True, samesite="strict", max_age=SESSION_HOURS * 3600,
                        secure=os.environ.get("CA_HTTPS") == "1")   # на хостинге с https куки не уходят по http
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


def _refresh_status(cid: int) -> str:
    """Статус счёта по нерешённым блокировкам: полная закрывает счёт, соразмерная замораживает спорную сумму.

    Справка о прежнем снятии ограничений перестаёт действовать, как только появляется новое ограничение.
    """
    scopes = {r["scope"] for r in conn.execute(
        f"""SELECT scope FROM cases WHERE client_id=? AND kind='block'
            AND status IN ({",".join("?" * len(UNRESOLVED))})""", (cid, *UNRESOLVED))}
    status = "blocked" if "full" in scopes else "limited" if scopes else "active"
    conn.execute("UPDATE clients SET status=? WHERE id=?", (status, cid))
    if status != "active":
        conn.execute("UPDATE clearances SET revoked_at=? WHERE client_id=? AND revoked_at IS NULL",
                     (_now().isoformat(), cid))
    return status


def _held_sum(cid: int) -> int:
    # Деньги под перевод на паузе или в периоде охлаждения зарезервированы: потратить их ещё раз нельзя.
    return conn.execute("""SELECT COALESCE(SUM(amount),0) AS s FROM transfers
                           WHERE sender_id=? AND status IN ('held','pending')""", (cid,)).fetchone()["s"]


def _frozen_sum(cid: int, except_case: int | None = None) -> int:
    """Спорные деньги, замороженные соразмерной блокировкой: остальным клиент пользуется."""
    return conn.execute(
        f"""SELECT COALESCE(SUM(frozen),0) AS s FROM cases WHERE client_id=? AND kind='block' AND scope='partial'
            AND status IN ({",".join("?" * len(UNRESOLVED))}) AND id IS NOT ?""",
        (cid, *UNRESOLVED, except_case)).fetchone()["s"]


def _available(cid: int) -> int:
    return _client(cid)["balance"] - _held_sum(cid) - _frozen_sum(cid)


def _limited_since(cid: int) -> str | None:
    """С какого момента получатель считается «новым» для клиента с соразмерной блокировкой:
    со спорного поступления (или с начала случая, если поступления нет)."""
    rows = conn.execute(
        f"""SELECT k.created_at, i.created_at AS inc_at FROM cases k LEFT JOIN transfers i ON i.id=k.incoming_transfer_id
            WHERE k.client_id=? AND k.kind='block' AND k.scope='partial'
            AND k.status IN ({",".join("?" * len(UNRESOLVED))})""", (cid, *UNRESOLVED)).fetchall()
    return min((r["inc_at"] or r["created_at"] for r in rows), default=None)


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
                  k.id AS case_id, k.status AS case_status, k.decision_comment, k.guardian_state, t.release_at
           FROM transfers t JOIN clients s ON s.id=t.sender_id JOIN clients r ON r.id=t.receiver_id
           LEFT JOIN cases k ON k.transfer_id=t.id AND k.kind='intercept'
           WHERE t.sender_id=? OR (t.receiver_id=? AND t.status='done')   -- получатель не видит остановленные
           ORDER BY t.created_at DESC, t.id DESC LIMIT 30""",
        (cid, cid)).fetchall()
    held, frozen = _held_sum(cid), _frozen_sum(cid)
    feed = []
    for r in rows:
        item = dict(r) | {"direction": "in" if r["receiver_id"] == cid else "out",
                          "sender": i18n.name(r["sender_id"], r["sender"]),
                          "receiver": i18n.name(r["receiver_id"], r["receiver"])}
        if item["direction"] == "in":
            item |= _incoming_flags(cid, r)
            # Возврат по жалобе приходит от счёта из цепочки, а не от того, кому клиент переводил.
            item["refund"] = bool(r["return_of"]) and _incoming_row(r["return_of"])["receiver_id"] != r["sender_id"]
        else:
            item["can_complain"] = _can_complain(r)
            item["complained"] = bool(conn.execute("SELECT 1 FROM complaints WHERE transfer_id=?",
                                                   (r["id"],)).fetchone())
        feed.append(item)
    return {"id": c["id"], "name": c["name"], "balance": c["balance"], "held": held, "frozen": frozen,
            "available": max(0, c["balance"] - held - frozen), "status": c["status"], "feed": feed,
            "call": _call_info(cid),
            "complaints": _complaints_view(cid),
            "self_report": _self_report_view(cid),
            "recruit": _recruit_view(cid) if c["status"] != "blocked" else None,
            "guardian_requests": _guardian_requests(cid),
            "cooling": _cooling_list("t.sender_id=?", (cid,)),
            "now": _now().isoformat(),          # для отсчёта времени на экране: время банка, а не браузера
            "guardian_cooling": _cooling_list(
                """t.sender_id IN (SELECT client_id FROM guardians WHERE guardian_id=?
                                   AND (remove_at IS NULL OR remove_at > ?))""", (cid, _now().isoformat()))}


def _incoming_row(tid: int):
    return conn.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()


def _returned_sum(inc_id: int) -> int:
    """Сколько денег этого перевода уже вернули его отправителю: получатель сам или счета дальше по цепочке."""
    return conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM transfers WHERE return_of=? AND status='done'",
                        (inc_id,)).fetchone()["s"]


def _returned(inc_id: int) -> bool:
    inc = _incoming_row(inc_id)
    return bool(inc) and _returned_sum(inc_id) >= inc["amount"]


def _incoming_flags(cid: int, r) -> dict:
    """Нужно ли предупредить клиента, что пришли, возможно, чужие деньги."""
    inc = _incoming_row(r["id"])
    complained = bool(conn.execute("SELECT 1 FROM complaints WHERE transfer_id=?", (inc["id"],)).fetchone())
    flags = {"warning": False, "complained": complained, "returned": _returned(inc["id"])}
    if (inc["status"] != "done" or inc["return_of"] or flags["returned"]
            or engine.find_forward(conn, cid, inc)):
        return flags
    fresh = _now() - datetime.fromisoformat(inc["created_at"]) <= timedelta(hours=engine.LOOKBACK_HOURS)
    stranger = not engine._had_contact(conn, cid, inc["sender_id"], inc["created_at"], inc["id"])
    known = bool(conn.execute("SELECT 1 FROM incoming_marks WHERE transfer_id=?", (inc["id"],)).fetchone())
    flags["warning"] = complained or (fresh and stranger and not known and inc["amount"] >= WARN_MIN_AMOUNT)
    return flags


def _can_complain(r) -> bool:
    if r["status"] != "done" or r["return_of"]:
        return False
    return _now() - datetime.fromisoformat(r["created_at"]) <= timedelta(days=COMPLAINT_DAYS)


def _intercept_view(case) -> dict:
    inc = conn.execute(
        """SELECT t.id, t.amount, t.sender_id, c.name FROM transfers t
           JOIN clients c ON c.id=t.sender_id WHERE t.id=?""", (case["incoming_transfer_id"],)).fetchone() \
        if case["incoming_transfer_id"] else None
    codes = [r.get("code") for r in json.loads(case["reasons"])]
    tr = conn.execute(
        """SELECT t.amount, t.receiver_id, c.name FROM transfers t JOIN clients c ON c.id=t.receiver_id WHERE t.id=?""",
        (case["transfer_id"],)).fetchone()
    return {"case_id": case["id"], "transfer_id": case["transfer_id"], "status": "held",
            "case_status": case["status"], "score": case["score"],
            "reasons": i18n.reasons(json.loads(case["reasons"])),
            "amount": tr["amount"], "receiver": i18n.name(tr["receiver_id"], tr["name"]),
            # Что показать клиенту в окне остановки: схема с чужими деньгами, звонок или подозрительный получатель.
            "kind": "scheme" if inc else ("call" if "suspicious_call" in codes else
                                          "message" if "scam_message" in codes else "grey"),
            "guardian": _guardian_name(case["guardian_id"]), "guardian_state": case["guardian_state"],
            "safe_return": {"to_id": inc["sender_id"], "to_name": i18n.name(inc["sender_id"], inc["name"]),
                            "amount": inc["amount"]} if inc else None}


@app.post("/transfers")
def create_transfer(body: TransferIn, s: dict = Depends(as_client)):
    """Создаёт перевод от имени вошедшего клиента. Если движок видит схему, перевод останавливается."""
    sender_id = s["client_id"]
    sender = _client(sender_id)
    _client(body.receiver_id)
    if sender_id == body.receiver_id:
        raise HTTPException(400, t("self_transfer"))
    if sender["status"] == "blocked":
        raise HTTPException(403, t("account_blocked"))
    if engine.afc_status(conn, body.receiver_id) == "black":
        raise HTTPException(403, t("afc_black"))
    if sender["status"] == "limited" and not engine._had_contact(conn, sender_id, body.receiver_id,
                                                                 _limited_since(sender_id)):
        # Соразмерная блокировка: свои деньги доступны, но только для тех, кому клиент переводил и раньше.
        raise HTTPException(403, t("limited_new_receiver"))
    now = _now()
    res = engine.evaluate(conn, sender_id, body.receiver_id, body.amount, now)
    # На один «чужой» входящий существует не больше одного незакрытого случая.
    prev = None
    if res["decision"] == "hold" and res["incoming_id"] is not None:
        prev = conn.execute(
            """SELECT * FROM cases WHERE client_id=? AND incoming_transfer_id=? AND kind='intercept'
               AND status IN ('open','under_review')""", (sender_id, res["incoming_id"])).fetchone()
        if prev and prev["status"] == "under_review":
            raise HTTPException(409, t("under_review_dup"))
        if prev:   # новая попытка заменяет прежнюю, её резерв освобождается
            conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (prev["transfer_id"],))
    if _available(sender_id) < body.amount:
        raise HTTPException(400, t("no_funds_frozen" if _frozen_sum(sender_id) else "no_funds"))

    if res["decision"] != "hold":
        if not res.get("return_to_sender") and _needs_cooling(sender_id, body.receiver_id, body.amount, now):
            release = (now + timedelta(minutes=COOLING_MINUTES)).isoformat()
            cur = conn.execute("""INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,release_at)
                                  VALUES(?,?,?,?,'pending',?)""",
                               (sender_id, body.receiver_id, body.amount, now.isoformat(), release))
            log("system", "cooling_started", None, cur.lastrowid, release_at=release)
            return {"transfer_id": cur.lastrowid, "status": "pending", "release_at": release,
                    "amount": body.amount, "score": res["score"], "reasons": res["reasons"]}
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
    log("system", "intercepted", case_id, cur.lastrowid, score=res["score"], amount=body.amount)
    if any(r["code"] == "suspicious_call" for r in res["reasons"]):
        log("system", "call_signal", case_id, cur.lastrowid)
    g = _guardian_of(sender_id)
    if g and not prev:
        conn.execute("UPDATE cases SET guardian_id=?, guardian_state='notified' WHERE id=?", (g["guardian_id"], case_id))
        log("system", "guardian_notified", case_id, cur.lastrowid, guardian=g["guardian_id"])
    return _intercept_view(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())


@app.get("/cases/{case_id}")
def intercept_case(case_id: int, s: dict = Depends(as_client)):
    """Остановленный перевод: клиент может вернуться к выбору позже."""
    return _intercept_view(_own_case(s, case_id, "intercept"))


def _return_incoming(client: dict, inc) -> dict:
    """Безопасный возврат: деньги уходят только на счёт, с которого пришли, и только один раз."""
    cid = client["client_id"]
    if _client(cid)["status"] == "blocked":
        # Пока счёт приостановлен, деньгами распоряжается только банк: иначе их можно увести в обход проверки.
        raise HTTPException(403, t("account_blocked"))
    if _returned(inc["id"]):
        raise HTTPException(409, t("already_returned"))
    if engine.afc_status(conn, inc["sender_id"]) == "black":
        # Деньги пришли со счёта подтверждённого мошенника: возврат отдал бы их ему же. Разбирается банк.
        raise HTTPException(403, t("afc_black_return"))
    # Остановленные пересылки этих денег больше не нужны: их резерв освобождается.
    cases = conn.execute(
        """SELECT * FROM cases WHERE client_id=? AND incoming_transfer_id=? AND kind='intercept'
           AND status IN ('open','under_review')""", (cid, inc["id"])).fetchall()
    for case in cases:
        conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (case["transfer_id"],))
        conn.execute("UPDATE cases SET status='returned' WHERE id=?", (case["id"],))
    amount = inc["amount"] - _returned_sum(inc["id"])      # часть могли уже вернуть пострадавшему по жалобе
    available = max(0, _available(cid))
    if available < amount:
        raise HTTPException(400, t("return_no_funds", available=_money(available), amount=_money(amount)))
    cur = conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,return_of) VALUES(?,?,?,?,?,?)",
        (cid, inc["sender_id"], amount, _now().isoformat(), "done", inc["id"]))
    _move(cid, inc["sender_id"], amount)
    for case in cases or [None]:
        log(f"client:{cid}", "returned", case["id"] if case else None, inc["id"], amount=amount)
    return {"status": "returned", "return_transfer_id": cur.lastrowid, "amount": amount}


@app.post("/cases/{case_id}/return-to-sender")
def return_to_sender(case_id: int, s: dict = Depends(as_client)):
    """Возврат из окна остановленного перевода."""
    case = _own_case(s, case_id, "intercept")
    if case["status"] != "open":
        raise HTTPException(409, t("decided"))
    if not case["incoming_transfer_id"]:
        raise HTTPException(400, t("no_return"))
    return _return_incoming(s, _incoming_row(case["incoming_transfer_id"]))


@app.post("/cases/{case_id}/withdraw")
def withdraw(case_id: int, s: dict = Depends(as_client)):
    """Клиент сам отменяет остановленный перевод: деньги остаются у него."""
    case = _own_case(s, case_id, "intercept")
    if case["status"] not in ("open", "under_review"):
        raise HTTPException(409, t("decided"))
    conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (case["transfer_id"],))
    conn.execute("UPDATE cases SET status='withdrawn' WHERE id=?", (case_id,))
    log(f"client:{s['client_id']}", "withdrawn", case_id, case["transfer_id"])
    return {"status": "withdrawn"}


@app.get("/recipients/{rid}/check")
def check_recipient(rid: int, s: dict = Depends(as_client)):
    """Проверка получателя по базе Антифрод-центра до перевода (в демо база — имитация)."""
    _client(rid)
    status = engine.afc_status(conn, rid)
    return {"list": status, "text": t(f"afc_{status}_check") if status else None}


class CallSignalIn(BaseModel):
    active: bool


@app.post("/telecom/signal")
def telecom_signal(body: CallSignalIn, s: dict = Depends(as_client)):
    """Сигнал оператора связи о подозрительном звонке клиенту (как в пилоте «Телеком Антифрод»).

    В банке сигнал приходит от оператора по защищённому каналу; в демо его включает сам клиент кнопкой.
    """
    cid = s["client_id"]
    if body.active:
        conn.execute("INSERT OR REPLACE INTO call_signals(client_id,number,started_at) VALUES(?,?,?)",
                     (cid, "+7 708 *** 41 19", _now().isoformat()))
    else:
        conn.execute("DELETE FROM call_signals WHERE client_id=?", (cid,))
    return _call_info(cid)


def _call_info(cid: int) -> dict | None:
    if not engine.call_active(conn, cid, _now()):
        return None
    row = conn.execute("SELECT number, started_at FROM call_signals WHERE client_id=?", (cid,)).fetchone()
    return {"number": row["number"], "started_at": row["started_at"]}


def _own_incoming(client: dict, tid: int):
    inc = _incoming_row(tid)
    if not inc or inc["receiver_id"] != client["client_id"]:
        raise HTTPException(404, t("transfer_not_found"))
    if inc["status"] != "done" or inc["return_of"]:
        raise HTTPException(400, t("cannot_return"))
    return inc


@app.post("/incoming/{tid}/return")
def return_incoming(tid: int, s: dict = Depends(as_client)):
    """Возврат прямо из списка операций, до того как мошенник попросит переслать деньги."""
    return _return_incoming(s, _own_incoming(s, tid))


@app.post("/incoming/{tid}/known")
def mark_known(tid: int, s: dict = Depends(as_client)):
    """Клиент знает отправителя: предупреждение у этого перевода скрывается. Перехват при пересылке остаётся."""
    inc = _own_incoming(s, tid)
    conn.execute("INSERT OR IGNORE INTO incoming_marks(transfer_id,created_at) VALUES(?,?)",
                 (inc["id"], _now().isoformat()))
    return {"ok": True}


def _trace_complaint(tr, complaint_id: int, now: datetime, complainant: int) -> list[int]:
    """Идёт по цепочке от перевода пострадавшего и приостанавливает счета, через которые прошли его деньги.

    Звено, которое переслало деньги дальше, получает случай с этой пересылкой. Счёт, на котором деньги
    остались (начиная со второго звена), тоже приостанавливается: это обычно конечная карта дроппера.
    Первый получатель, который ещё ничего не переслал, не блокируется, а получает предупреждение.
    """
    created, seen, frontier = [], {tr["sender_id"]}, [tr]
    for hop in range(1, CHAIN_HOPS + 1):
        nxt = []
        for inc in frontier:
            holder = inc["receiver_id"]
            if holder in seen or _returned(inc["id"]):
                continue
            seen.add(holder)
            fwd = engine.find_forward(conn, holder, inc)
            if not fwd and hop == 1:
                continue
            reason = "complaint" if hop == 1 else "complaint_chain"
            reasons = [{"code": reason, "points": 30, "text": i18n.MSG["reason." + reason][0]}]
            if fwd:
                first = fwd[0]
                res = engine.evaluate(conn, holder, first["receiver_id"], first["amount"],
                                      datetime.fromisoformat(first["created_at"]))
                reasons += res["reasons"]
                score, out_id = min(100, res["score"] + 30), first["id"]
                sent = sum(r["amount"] for r in fwd)
                text = f"деньги переслали дальше ({_money(sent)})"
                nxt.extend(fwd)
            else:
                score, out_id, sent = 30, None, 0
                text = f"на счёте остались деньги из цепочки ({_money(inc['amount'])})"
            # Сколько денег из цепочки осталось на этом счёте: их банк замораживает в любом случае.
            frozen = max(0, inc["amount"] - sent)
            case_id = conn.execute(
                """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at,
                                     complaint_id,hop,frozen) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (holder, out_id, inc["id"], "block", score, json.dumps(reasons, ensure_ascii=False),
                 now.isoformat(), complaint_id, hop, frozen)).lastrowid
            # Соразмерная блокировка: если по правилам клиент похож на невольного участника, замораживается
            # только спорная сумма. Остальных (и всех, в ком система не уверена) счёт закрывается целиком.
            case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            role = engine.assess_role(conn, holder, case, now)
            partial = role["verdict"] == "unwitting"
            if partial:
                conn.execute("UPDATE cases SET scope='partial' WHERE id=?", (case_id,))
                text = (f"Заморожено {_money(frozen)}: {text}. Остальными деньгами клиент пользуется, "
                        "но только для знакомых получателей")
            else:
                text = "Операции приостановлены: " + text
            _refresh_status(holder)
            log(f"client:{complainant}", "complaint", case_id, tr["id"], amount=tr["amount"])
            log("system", "blocked", case_id, out_id or inc["id"], text=text, hop=hop,
                scope="partial" if partial else "full", role=role["label"])
            created.append(case_id)
        frontier = nxt
    return created


@app.post("/transfers/{tid}/complaint")
def complain(tid: int, s: dict = Depends(as_client)):
    """Отправитель сообщает, что перевёл деньги мошенникам. Банк проверяет, куда ушли деньги дальше."""
    tr = conn.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
    if not tr or tr["sender_id"] != s["client_id"]:
        raise HTTPException(404, t("transfer_not_found"))
    if not _can_complain(tr):
        raise HTTPException(400, t("complaint_old", days=COMPLAINT_DAYS))
    if conn.execute("SELECT 1 FROM complaints WHERE transfer_id=?", (tid,)).fetchone():
        raise HTTPException(409, t("complaint_dup"))
    since = (_now() - timedelta(days=COMPLAINT_DAYS)).isoformat()
    if conn.execute("SELECT COUNT(*) AS n FROM complaints WHERE complainant_id=? AND created_at >= ?",
                    (s["client_id"], since)).fetchone()["n"] >= COMPLAINT_LIMIT:
        raise HTTPException(429, t("complaint_limit", n=COMPLAINT_LIMIT))
    now = _now()
    complaint_id = conn.execute(
        "INSERT INTO complaints(transfer_id,complainant_id,outcome,created_at) VALUES(?,?,?,?)",
        (tid, s["client_id"], "pending", now.isoformat())).lastrowid
    if _returned(tid):
        outcome, text, cases = "already_returned", t("complaint_already_returned"), []
    else:
        cases = _trace_complaint(tr, complaint_id, now, s["client_id"])
        outcome = "blocked" if cases else "warned"
        text = t("complaint_blocked", n=len(cases)) if cases else t("complaint_warned")
    conn.execute("UPDATE complaints SET outcome=?, case_id=? WHERE id=?",
                 (outcome, cases[0] if cases else None, complaint_id))
    if not cases:
        log(f"client:{s['client_id']}", "complaint", None, tid, amount=tr["amount"], outcome=outcome)
    return {"outcome": outcome, "text": text}


# ---------- Что стало с жалобой: пострадавший видит, где его деньги ----------

def _complaint_state(p) -> dict:
    """Сколько денег пострадавшего заморожено в цепочке и сколько ему уже вернули."""
    tr = _incoming_row(p["transfer_id"])
    cases = conn.execute("SELECT * FROM cases WHERE complaint_id=?", (p["id"],)).fetchall()
    returned = min(tr["amount"], _returned_sum(tr["id"]))
    frozen = min(tr["amount"] - returned, sum(k["frozen"] for k in cases if k["status"] in UNRESOLVED))
    if returned >= tr["amount"]:
        stage = "returned"
    elif any(k["status"] in ("open", "under_review") for k in cases):
        stage = "checking"
    elif frozen:
        stage = "frozen"
    elif p["outcome"] == "warned":
        stage = "warned"
    else:
        stage = "closed"
    return {"amount": tr["amount"], "returned": returned, "frozen": frozen, "stage": stage, "transfer": tr}


def _complaints_view(cid: int) -> list[dict]:
    out = []
    for p in conn.execute("SELECT * FROM complaints WHERE complainant_id=? ORDER BY id DESC LIMIT 5", (cid,)):
        st = _complaint_state(p)
        who = _client(st["transfer"]["receiver_id"])
        vals = {"m": _money(st["amount"]), "r": _money(st["returned"]), "f": _money(st["frozen"]),
                "left": _money(st["amount"] - st["returned"])}
        text = t("complaint.stage." + st["stage"], **vals)
        if st["returned"] and st["stage"] != "returned":
            text = t("complaint.part", **vals) + " " + text
        out.append({"id": p["id"], "created_at": p["created_at"], "amount": st["amount"],
                    "receiver": i18n.name(who["id"], who["name"]), "stage": st["stage"],
                    "returned": st["returned"], "frozen": st["frozen"], "text": text})
    return out


def _refundable(case) -> int:
    """Сколько денег из цепочки можно вернуть пострадавшему со счёта этого случая прямо сейчас."""
    if case["kind"] != "block" or not case["complaint_id"] or case["status"] not in UNRESOLVED:
        return 0
    p = conn.execute("SELECT * FROM complaints WHERE id=?", (case["complaint_id"],)).fetchone()
    owed = _incoming_row(p["transfer_id"])["amount"] - _returned_sum(p["transfer_id"])
    cid = case["client_id"]
    on_account = _client(cid)["balance"] - _held_sum(cid) - _frozen_sum(cid, except_case=case["id"])
    return max(0, min(case["frozen"], owed, on_account))


def _refund(case, actor: str) -> dict:
    """Возвращает пострадавшему деньги из цепочки, которые остались на счёте. Больше, чем он перевёл, не уйдёт."""
    amount = _refundable(case)
    if not amount:
        raise HTTPException(409, t("refund_none"))
    p = conn.execute("SELECT * FROM complaints WHERE id=?", (case["complaint_id"],)).fetchone()
    cur = conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,return_of) VALUES(?,?,?,?,?,?)",
        (case["client_id"], p["complainant_id"], amount, _now().isoformat(), "done", p["transfer_id"]))
    _move(case["client_id"], p["complainant_id"], amount)
    conn.execute("UPDATE cases SET frozen = frozen - ? WHERE id=?", (amount, case["id"]))
    log(actor, "refunded", case["id"], cur.lastrowid, amount=amount,
        text=f"Пострадавшему возвращено {_money(amount)}" + (": клиент вернул сам" if actor != "analyst" else ""))
    return {"status": "refunded", "amount": amount}


@app.post("/cases/{case_id}/refund")
def refund_by_client(case_id: int, s: dict = Depends(as_client)):
    """Клиент сам возвращает пострадавшему замороженные деньги, не дожидаясь решения банка."""
    return _refund(_own_case(s, case_id, "block"), f"client:{s['client_id']}")


@app.post("/analyst/cases/{case_id}/refund")
def refund_by_analyst(case_id: int, s: dict = Depends(as_analyst)):
    """Сотрудник возвращает пострадавшему деньги, замороженные на счёте из цепочки.

    В банке для этого нужно основание: согласие владельца счёта либо решение суда или органа
    уголовного преследования. В демо это одна кнопка.
    """
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Случай не найден")
    _refund(case, "analyst")
    return _analyst_case(case_id)


@app.post("/cases/{case_id}/insist")
def insist(case_id: int, s: dict = Depends(as_client)):
    """Клиент настаивает на переводе: деньги остаются на паузе, случай уходит аналитику."""
    case = _own_case(s, case_id, "intercept")
    if case["status"] != "open":
        raise HTTPException(409, t("decided"))
    if case["guardian_id"] and case["guardian_state"] in ("notified", "asked"):
        # Перевод уходит сотруднику только после подтверждения доверенного лица.
        if case["guardian_state"] == "notified":
            conn.execute("UPDATE cases SET guardian_state='asked' WHERE id=?", (case_id,))
            log(f"client:{s['client_id']}", "guardian_asked", case_id, case["transfer_id"])
        name = _guardian_name(case["guardian_id"])
        return {"status": "guardian", "guardian": name, "text": t("guardian_wait", name=name)}
    conn.execute("UPDATE cases SET status='under_review' WHERE id=?", (case_id,))
    log(f"client:{s['client_id']}", "insisted", case_id, case["transfer_id"])
    return {"status": "under_review"}


# ---------- Апелляция: клиент заблокирован и объясняет, что произошло ----------

def _minutes_text(minutes: int) -> str:
    if minutes < 60:
        return t("min", m=minutes)
    return t("hour_min", h=minutes // 60, m=minutes % 60) if minutes < 1440 else t("days", d=minutes // 1440)


def _doc_title(kind: str, for_client: bool) -> str:
    if for_client and f"doc.{kind}" in i18n.MSG:
        return t(f"doc.{kind}")
    return DOC_KINDS.get(kind, {}).get("title", kind)


def _documents(case_id: int, for_client: bool = False) -> list[dict]:
    rows = conn.execute("SELECT id, kind, filename, size, uploaded_at FROM documents WHERE case_id=? ORDER BY id",
                        (case_id,)).fetchall()
    return [dict(d) | {"title": _doc_title(d["kind"], for_client)} for d in rows]


def _case_view(case) -> dict:
    inc = conn.execute("SELECT amount, created_at FROM transfers WHERE id=?",
                       (case["incoming_transfer_id"],)).fetchone()
    out = conn.execute("SELECT amount, created_at FROM transfers WHERE id=?", (case["transfer_id"],)).fetchone()
    first = json.loads(case["reasons"])[0].get("code")
    if first == "self_report":
        summary = t("summary_self")
    elif out:
        gap = int((datetime.fromisoformat(out["created_at"]) - datetime.fromisoformat(inc["created_at"])).total_seconds() // 60)
        summary = t("summary", inc=_money(inc["amount"]), gap=_minutes_text(gap), out=_money(out["amount"]))
    elif first == "linked":
        summary = t("summary_linked")
    else:
        summary = t("summary_holder", inc=_money(inc["amount"]))
    partial = case["scope"] == "partial"
    summary += " " + (t("summary_partial", m=_money(case["frozen"])) if partial else t("summary_full"))
    docs = _documents(case["id"], for_client=True)
    have = {d["kind"] for d in docs}
    refundable = _refundable(case) if case["status"] in ("open", "under_review") else 0
    return {
        "case_id": case["id"],
        "status": case["status"],
        "status_text": t("status." + case["status"]),
        "scope": case["scope"],
        "frozen": case["frozen"] if partial else 0,
        # Клиент может сам вернуть пострадавшему замороженные деньги, не дожидаясь решения банка.
        "refundable": refundable,
        "summary": summary,
        "reasons": i18n.reasons(json.loads(case["reasons"])),
        "explanation": case["explanation"],
        "decision_comment": case["decision_comment"],
        "submitted_at": case["submitted_at"],
        "due_at": case["due_at"],
        "required": [{"kind": k, "title": _doc_title(k, True), "required": v["required"], "uploaded": k in have}
                     for k, v in DOC_KINDS.items()],
        "documents": docs,
        "max_documents": MAX_DOCS,
        "can_submit": case["status"] == "open",
        "certificate": _clearance_view(case["id"]),
    }


@app.get("/clients/{cid}/case")
def client_case(cid: int, s: dict = Depends(session)):
    """Последний случай блокировки клиента: причина простым языком, документы, статус."""
    _own_client(s, cid)
    # Сначала нерешённая блокировка: снятие одной не должно скрывать другую.
    case = conn.execute(
        """SELECT * FROM cases WHERE client_id=? AND kind='block'
           ORDER BY status IN ('open','under_review') DESC, id DESC LIMIT 1""", (cid,)).fetchone()
    if not case:
        raise HTTPException(404, t("no_block"))
    return _case_view(case)


@app.post("/cases/{case_id}/documents")
def upload_document(case_id: int, body: DocumentIn, s: dict = Depends(as_client)):
    """Клиент прикладывает документ к апелляции: только PNG, JPEG или PDF до 5 МБ."""
    case = _own_case(s, case_id, "block")
    if case["status"] != "open":
        raise HTTPException(409, t("docs_sent"))
    if body.kind not in DOC_KINDS:
        raise HTTPException(400, t("doc_kind"))
    if conn.execute("SELECT COUNT(*) AS n FROM documents WHERE case_id=?", (case_id,)).fetchone()["n"] >= MAX_DOCS:
        raise HTTPException(400, t("doc_count", n=MAX_DOCS))
    try:
        data = base64.b64decode(body.content_base64, validate=True)
    except Exception:
        raise HTTPException(400, t("doc_broken"))
    if not data or len(data) > MAX_FILE:
        raise HTTPException(400, t("doc_size"))
    kind = next(((ext, media) for sig, ext, media in FILE_TYPES if data.startswith(sig)), None)
    if not kind:
        raise HTTPException(400, t("doc_type"))
    # Имя и расширение задаёт сервер по содержимому файла, а не по тому, что прислал клиент.
    stem = re.sub(r"[^\w\- ]", "_", os.path.splitext(os.path.basename(body.filename))[0])[:60] or "file"
    name = stem + kind[0]
    cur = conn.execute("INSERT INTO documents(case_id,kind,filename,size,media_type,uploaded_at) VALUES(?,?,?,?,?,?)",
                       (case_id, body.kind, name, len(data), kind[1], _now().isoformat()))
    os.makedirs(UPLOADS, exist_ok=True)
    # На диске только зашифрованный файл под номером, без имени и расширения клиента.
    path = os.path.join(UPLOADS, f"{cur.lastrowid}.enc")
    with open(path, "wb") as f:
        f.write(vault.encrypt(data))
    conn.execute("UPDATE documents SET path=? WHERE id=?", (path, cur.lastrowid))
    log(f"client:{s['client_id']}", "document", case_id, None, filename=name, title=DOC_KINDS[body.kind]["title"])
    return {"id": cur.lastrowid, "kind": body.kind, "filename": name, "size": len(data)}


@app.post("/cases/{case_id}/submit")
def submit_appeal(case_id: int, body: AppealIn, s: dict = Depends(as_client)):
    """Клиент отправляет апелляцию: объяснение и обязательные документы."""
    case = _own_case(s, case_id, "block")
    if case["status"] != "open":
        raise HTTPException(409, t("appeal_sent"))
    text = body.explanation.strip()
    if len(text) < 20:
        raise HTTPException(400, t("appeal_short"))
    have = {r["kind"] for r in conn.execute("SELECT kind FROM documents WHERE case_id=?", (case_id,))}
    missing = [_doc_title(k, True) for k, v in DOC_KINDS.items() if v["required"] and k not in have]
    if missing:
        raise HTTPException(400, t("appeal_missing", docs="; ".join(missing)))
    now = _now()
    conn.execute("UPDATE cases SET status='under_review', explanation=?, submitted_at=?, due_at=? WHERE id=?",
                 (text[:2000], now.isoformat(), (now + timedelta(days=APPEAL_SLA_DAYS)).isoformat(), case_id))
    log(f"client:{s['client_id']}", "appeal", case_id, None)
    return _case_view(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())


@app.get("/documents/{doc_id}")
def get_document(doc_id: int, s: dict = Depends(session)):
    """Документ видят только его владелец и сотрудник банка. Отдаётся как вложение."""
    doc = conn.execute(
        "SELECT d.*, k.client_id FROM documents d JOIN cases k ON k.id=d.case_id WHERE d.id=?", (doc_id,)).fetchone()
    if not doc or (not s["analyst"] and doc["client_id"] != s["client"]["client_id"]):
        raise HTTPException(404, t("doc_not_found"))
    if not doc["path"] or not os.path.exists(doc["path"]):
        raise HTTPException(404, t("doc_not_found"))
    try:
        with open(doc["path"], "rb") as f:
            data = vault.decrypt(f.read())
    except vault.InvalidToken:
        # Файл подменён или зашифрован другим ключом: отдавать нечего, но и падать не нужно.
        raise HTTPException(409, t("doc_broken"))
    return RawResponse(data, media_type=doc["media_type"], headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(doc['filename'])}",
        "Cache-Control": "no-store"})


# ---------- Доверенное лицо: родственник узнаёт об остановленном переводе ----------

def _guardian_of(cid: int):
    """Действующее доверенное лицо клиента. Отключение вступает в силу только через сутки."""
    row = conn.execute("SELECT * FROM guardians WHERE client_id=?", (cid,)).fetchone()
    if row and row["remove_at"] and row["remove_at"] <= _now().isoformat():
        conn.execute("DELETE FROM guardians WHERE client_id=?", (cid,))
        return None
    return row


def _guardian_name(gid: int | None) -> str | None:
    if not gid:
        return None
    row = conn.execute("SELECT name FROM clients WHERE id=?", (gid,)).fetchone()
    return i18n.name(gid, row["name"]) if row else None


def _guardian_candidates(cid: int) -> list[dict]:
    """Люди, с которыми клиент давно обменивается переводами: новый «знакомый» мошенника сюда не попадёт."""
    before = (_now() - timedelta(days=GUARDIAN_KNOWN_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT DISTINCT c.id, c.name FROM transfers t
           JOIN clients c ON c.id = CASE WHEN t.sender_id=? THEN t.receiver_id ELSE t.sender_id END
           WHERE t.status='done' AND (t.sender_id=? OR t.receiver_id=?) AND t.created_at <= ?
             AND c.status='active' AND c.id NOT IN (SELECT client_id FROM afc_list)
           ORDER BY c.id""", (cid, cid, cid, before)).fetchall()
    return [{"id": r["id"], "name": i18n.name(r["id"], r["name"])} for r in rows if r["id"] != cid]


def _guardian_requests(gid: int) -> list[dict]:
    """Остановленные переводы тех, для кого клиент — доверенное лицо."""
    rows = conn.execute(
        """SELECT * FROM cases WHERE guardian_id=? AND kind='intercept' AND status IN ('open','under_review')
           ORDER BY created_at DESC""", (gid,)).fetchall()
    out = []
    for k in rows:
        ward = _client(k["client_id"])
        view = _intercept_view(k)
        out.append({"case_id": k["id"], "ward": i18n.name(ward["id"], ward["name"]), "amount": view["amount"],
                    "receiver": view["receiver"], "reasons": view["reasons"], "kind": view["kind"],
                    "state": k["guardian_state"], "status": k["status"], "created_at": k["created_at"]})
    return out


class GuardianIn(BaseModel):
    guardian_id: int


@app.get("/guardian")
def guardian_settings(s: dict = Depends(as_client)):
    cid = s["client_id"]
    g = _guardian_of(cid)
    return {"guardian": {"id": g["guardian_id"], "name": _guardian_name(g["guardian_id"]),
                         "remove_at": g["remove_at"]} if g else None,
            "candidates": [] if g else _guardian_candidates(cid)}


@app.post("/guardian")
def set_guardian(body: GuardianIn, s: dict = Depends(as_client)):
    cid = s["client_id"]
    if _guardian_of(cid):
        raise HTTPException(409, t("guardian_exists"))
    if body.guardian_id not in {c["id"] for c in _guardian_candidates(cid)}:
        raise HTTPException(400, t("guardian_not_candidate"))
    conn.execute("INSERT OR REPLACE INTO guardians(client_id,guardian_id,added_at,remove_at) VALUES(?,?,?,NULL)",
                 (cid, body.guardian_id, _now().isoformat()))
    log(f"client:{cid}", "guardian_set", None, None, guardian=body.guardian_id)
    return guardian_settings(s)


@app.post("/guardian/remove")
def remove_guardian(s: dict = Depends(as_client)):
    """Отключение через сутки: если мошенник уговорит «убрать контроль», родственник успеет вмешаться."""
    cid = s["client_id"]
    g = _guardian_of(cid)
    if not g:
        raise HTTPException(404, t("guardian_none"))
    if not g["remove_at"]:
        conn.execute("UPDATE guardians SET remove_at=? WHERE client_id=?",
                     ((_now() + timedelta(hours=GUARDIAN_REMOVE_HOURS)).isoformat(), cid))
        log(f"client:{cid}", "guardian_remove", None, None, guardian=g["guardian_id"])
    return guardian_settings(s)


@app.post("/guardian/keep")
def keep_guardian(s: dict = Depends(as_client)):
    cid = s["client_id"]
    if not _guardian_of(cid):
        raise HTTPException(404, t("guardian_none"))
    conn.execute("UPDATE guardians SET remove_at=NULL WHERE client_id=?", (cid,))
    return guardian_settings(s)


def _guarded_case(s: dict, case_id: int):
    case = conn.execute("SELECT * FROM cases WHERE id=? AND kind='intercept'", (case_id,)).fetchone()
    if not case or case["guardian_id"] != s["client_id"] or case["status"] not in ("open", "under_review"):
        raise HTTPException(404, t("guardian_case"))
    return case


@app.post("/guardian/requests/{case_id}/cancel")
def guardian_cancel(case_id: int, s: dict = Depends(as_client)):
    """Доверенное лицо отменяет перевод: деньги остаются у того, кого он защищает."""
    case = _guarded_case(s, case_id)
    conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (case["transfer_id"],))
    conn.execute("UPDATE cases SET status='guardian_cancelled', guardian_state='cancelled', decided_at=? WHERE id=?",
                 (_now().isoformat(), case_id))
    log(f"client:{s['client_id']}", "guardian_cancelled", case_id, case["transfer_id"])
    return {"status": "guardian_cancelled"}


@app.post("/guardian/requests/{case_id}/confirm")
def guardian_confirm(case_id: int, s: dict = Depends(as_client)):
    """Подтверждение доверенного лица не проводит перевод само: дальше решает сотрудник банка."""
    case = _guarded_case(s, case_id)
    if case["status"] != "open" or case["guardian_state"] != "asked":
        raise HTTPException(409, t("guardian_not_asked"))
    conn.execute("UPDATE cases SET status='under_review', guardian_state='confirmed' WHERE id=?", (case_id,))
    log(f"client:{s['client_id']}", "guardian_confirmed", case_id, case["transfer_id"])
    return {"status": "under_review"}


# ---------- Справка о снятии ограничений ----------

def _clearance_code() -> str:
    while True:
        code = secrets.token_hex(4).upper()
        if not conn.execute("SELECT 1 FROM clearances WHERE code=?", (code,)).fetchone():
            return code


def _issue_clearance(case):
    """Ограничения сняты: клиенту справка, в Антифрод-центр — сведения о снятии (в демо — запись в истории)."""
    now = _now()
    cid = conn.execute("INSERT INTO clearances(case_id,client_id,code,issued_at) VALUES(?,?,?,?)",
                       (case["id"], case["client_id"], _clearance_code(), now.isoformat())).lastrowid
    log("system", "cleared", case["id"], None,
        text=f"Выдана справка № {_clearance_number(cid, now.isoformat())}; в Антифрод-центр переданы сведения "
             "о снятии ограничений и выводе банка (имитация)")


def _clearance_number(cid: int, issued_at: str) -> str:
    return f"ЧС-{issued_at[:4]}-{cid:05d}"


def _clearance_view(case_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM clearances WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    if not row:
        return None
    return {"number": _clearance_number(row["id"], row["issued_at"]), "issued_at": row["issued_at"],
            "valid": row["revoked_at"] is None, "revoked_at": row["revoked_at"], "code": row["code"]}


def _page(title: str, body: str) -> RawResponse:
    html_doc = f"""<!DOCTYPE html><html lang="{i18n.lang.get()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>
<style>body{{margin:0;background:#f5f5fa;color:#1d1d1e;font:16px/1.5 "Roboto Flex",Roboto,"Segoe UI",system-ui,-apple-system,sans-serif}}
main{{max-width:720px;margin:24px auto;background:#fff;border-radius:16px;padding:28px 24px}}
h1{{font-size:22px;margin:0 0 4px;color:#1d1d1e}} .muted{{color:#7f8085;font-size:14px}}
.ok{{background:#e6f7f0;color:#00875c;border-radius:12px;padding:12px 14px;font-weight:600}}
.bad{{background:#fdeeee;color:#c62828;border-radius:12px;padding:12px 14px;font-weight:600}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:16px 0}} dt{{color:#7f8085}} dd{{margin:0}}
.demo{{border:1px dashed #d8d8dd;color:#7f8085;border-radius:10px;padding:8px 12px;font-size:13px;margin-top:20px}}
button{{font:inherit;font-weight:500;border:0;border-radius:12px;padding:12px 16px;background:#00ab75;color:#fff;cursor:pointer}}
@media print{{body{{background:#fff}} main{{margin:0}} button{{display:none}}}}</style></head>
<body><main>{body}</main></body></html>"""
    return RawResponse(html_doc, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/clients/{cid}/certificate", include_in_schema=False)
def certificate(cid: int, request: Request, s: dict = Depends(session)):
    """Справка для клиента: её можно распечатать или показать в другом банке. Подлинность проверяется по коду."""
    c = _own_client(s, cid)
    row = conn.execute("""SELECT * FROM clearances WHERE client_id=? AND revoked_at IS NULL
                          ORDER BY id DESC LIMIT 1""", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, t("no_certificate"))
    case = conn.execute("SELECT * FROM cases WHERE id=?", (row["case_id"],)).fetchone()
    blocked_at = conn.execute("SELECT MIN(at) AS at FROM events WHERE case_id=? AND action='blocked'",
                              (case["id"],)).fetchone()["at"] or case["created_at"]
    number = _clearance_number(row["id"], row["issued_at"])
    verify = str(request.base_url) + f"verify/{row['code']}"
    d = lambda x: ".".join(reversed(x[:10].split("-")))  # noqa: E731   2026-11-13 -> 13.11.2026
    body = f"""<h1>{escape(t("cert.title"))}</h1><div class="muted">№ {escape(number)} · {d(row["issued_at"])}</div>
<dl><dt>{escape(t("cert.client"))}</dt><dd>{escape(i18n.name(c["id"], c["name"]))}</dd>
<dt>{escape(t("cert.blocked"))}</dt><dd>{d(blocked_at)}</dd>
<dt>{escape(t("cert.cleared"))}</dt><dd>{d(case["decided_at"] or row["issued_at"])}</dd></dl>
<p>{escape(t("cert.text"))}</p><p>{escape(t("cert.afc"))}</p>
<p class="muted">{escape(t("cert.verify"))}<br><b>{escape(verify)}</b><br>{escape(t("cert.code"))}: <b>{row["code"]}</b></p>
<button onclick="print()">{escape(t("cert.print"))}</button>
<div class="demo">{escape(t("cert.demo"))}</div>"""
    return _page(t("cert.title"), body)


@app.get("/verify/{code}", include_in_schema=False)
def verify_certificate(code: str):
    """Проверка справки без входа: другой банк или работодатель вводит код с документа."""
    code = code.strip().upper()
    ok = bool(re.fullmatch(r"[0-9A-F]{8}", code))
    row = conn.execute("SELECT * FROM clearances WHERE code=?", (code,)).fetchone() if ok else None
    report = conn.execute("SELECT * FROM self_reports WHERE code=?", (code,)).fetchone() if ok and not row else None
    if report:
        # Подтверждение обращения: кто и когда сам сообщил банку. Подробностей страница не показывает.
        c = _client(report["client_id"])
        body = f"""<h1>{escape(t("sr.check"))}</h1><p class="ok">{escape(t("sr.valid"))}</p>
<dl><dt>{escape(t("cert.number"))}</dt><dd>{escape(_self_report_number(report))}</dd>
<dt>{escape(t("sr.at"))}</dt><dd>{escape(report["at"][:16].replace("T", " "))}</dd>
<dt>{escape(t("cert.client"))}</dt><dd>{escape(i18n.name(c["id"], c["name"]).split(" (")[0])}</dd></dl>
<div class="demo">{escape(t("cert.demo"))}</div>"""
        return _page(t("sr.check"), body)
    if not row:
        return _page(t("cert.check"), f'<h1>{escape(t("cert.check"))}</h1><p class="bad">{escape(t("cert.unknown"))}</p>'
                                      f'<div class="demo">{escape(t("cert.demo"))}</div>')
    c = _client(row["client_id"])
    short = i18n.name(c["id"], c["name"]).split(" (")[0]          # без лишних подробностей о клиенте
    number = _clearance_number(row["id"], row["issued_at"])
    state = (f'<p class="ok">{escape(t("cert.valid"))}</p>' if row["revoked_at"] is None else
             f'<p class="bad">{escape(t("cert.revoked", d=row["revoked_at"][:10]))}</p>')
    body = f"""<h1>{escape(t("cert.check"))}</h1>{state}
<dl><dt>{escape(t("cert.number"))}</dt><dd>{escape(number)}</dd><dt>{escape(t("cert.issued"))}</dt><dd>{row["issued_at"][:10]}</dd>
<dt>{escape(t("cert.client"))}</dt><dd>{escape(short)}</dd></dl><div class="demo">{escape(t("cert.demo"))}</div>"""
    return _page(t("cert.check"), body)


# ---------- Период охлаждения: крупный первый перевод новому получателю уходит через час ----------

def _needs_cooling(sender_id: int, receiver_id: int, amount: int, now: datetime) -> bool:
    """Новый получатель и крупная сумма. Части за сутки складываются: раздробить сумму не поможет."""
    since = (now - timedelta(hours=COOLING_KNOWN_HOURS)).isoformat()
    if engine._had_contact(conn, sender_id, receiver_id, since):
        return False
    recent = conn.execute("""SELECT COALESCE(SUM(amount),0) AS s FROM transfers WHERE sender_id=? AND receiver_id=?
                             AND status IN ('done','pending') AND created_at >= ?""",
                          (sender_id, receiver_id, since)).fetchone()["s"]
    return recent + amount >= COOLING_MIN


def _settle_due():
    """Переводы, у которых закончился период охлаждения. Если за это время счёт отправителя или получателя
    приостановили или получатель попал в чёрный список, перевод не уходит, деньги остаются у клиента."""
    now = _now().isoformat()
    for tr in conn.execute("SELECT * FROM transfers WHERE status='pending' AND release_at <= ?", (now,)).fetchall():
        sender, receiver = _client(tr["sender_id"]), _client(tr["receiver_id"])
        # Отложенный перевод всегда идёт новому получателю: при любом ограничении по счёту он не уходит.
        stop = ("sender_blocked" if sender["status"] != "active" else
                "receiver_blocked" if receiver["status"] == "blocked" else
                "receiver_black" if engine.afc_status(conn, receiver["id"]) == "black" else None)
        if stop:
            conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (tr["id"],))
            log("system", "cooling_stopped", None, tr["id"], reason=stop)
        else:
            conn.execute("UPDATE transfers SET status='done' WHERE id=?", (tr["id"],))
            _move(tr["sender_id"], tr["receiver_id"], tr["amount"])
            log("system", "cooling_done", None, tr["id"])


def _cooling_list(where: str, args: tuple) -> list[dict]:
    rows = conn.execute(
        f"""SELECT t.id, t.amount, t.created_at, t.release_at, t.sender_id, s.name AS sender, t.receiver_id, r.name AS receiver
            FROM transfers t JOIN clients s ON s.id=t.sender_id JOIN clients r ON r.id=t.receiver_id
            WHERE t.status='pending' AND {where} ORDER BY t.release_at""", args).fetchall()
    return [dict(r) | {"sender": i18n.name(r["sender_id"], r["sender"]),
                       "receiver": i18n.name(r["receiver_id"], r["receiver"])} for r in rows]


def _cancel_pending(tr, actor: str):
    conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (tr["id"],))
    log(actor, "cooling_cancelled", None, tr["id"])
    return {"status": "cancelled", "amount": tr["amount"]}


@app.post("/transfers/{tid}/cancel")
def cancel_pending(tid: int, s: dict = Depends(as_client)):
    """Клиент передумал, пока перевод в периоде охлаждения: деньги остаются у него."""
    tr = conn.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
    if not tr or tr["sender_id"] != s["client_id"]:
        raise HTTPException(404, t("transfer_not_found"))
    if tr["status"] != "pending":
        raise HTTPException(409, t("cooling_none"))
    return _cancel_pending(tr, f"client:{s['client_id']}")


@app.post("/guardian/transfers/{tid}/cancel")
def guardian_cancel_pending(tid: int, s: dict = Depends(as_client)):
    """Доверенное лицо отменяет крупный перевод близкого человека, пока тот не ушёл."""
    tr = conn.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
    g = _guardian_of(tr["sender_id"]) if tr else None
    if not tr or not g or g["guardian_id"] != s["client_id"]:
        raise HTTPException(404, t("transfer_not_found"))
    if tr["status"] != "pending":
        raise HTTPException(409, t("cooling_none"))
    return _cancel_pending(tr, f"client:{s['client_id']}")


# ---------- Проверка подозрительного сообщения ----------

class MessageIn(BaseModel):
    text: str = Field(max_length=4000)


@app.post("/messages/check")
def check_message(body: MessageIn, s: dict = Depends(as_client)):
    """Клиент вставляет текст из мессенджера. Текст не сохраняется: в базу пишется только найденная схема."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, t("msg.empty"))
    cid, now = s["client_id"], _now()
    r = messages.check(text)
    conn.execute("INSERT INTO message_checks(client_id,scheme,verdict,at) VALUES(?,?,?,?)",
                 (cid, r["scheme"], r["verdict"], now.isoformat()))
    key = r["scheme"] or r["verdict"]
    out = {"verdict": r["verdict"], "scheme": r["scheme"], "title": t(f"msg.{key}.title"), "text": t(f"msg.{key}.text"),
           "advice": [t(f"msg.{key}.a{i}") for i in (1, 2, 3)] if r["scheme"] else [],
           "matched": r["matched"], "flags": [{"code": f, "text": t(f"msg.flag.{f}")} for f in r["flags"]],
           "return_incoming": None}
    if r["scheme"] == "mistaken" and _client(cid)["status"] != "blocked":
        # Если чужие деньги уже на счёте, сразу предлагаем вернуть их отправителю.
        inc = max(engine._stranger_incomings(conn, cid, now), key=lambda i: i["created_at"], default=None)
        if inc:
            who = _client(inc["sender_id"])
            out["return_incoming"] = {"id": inc["id"], "amount": inc["amount"], "sender": i18n.name(who["id"], who["name"])}
    return out


# ---------- Предупреждение о вербовке в дропперы ----------

def _recruit_view(cid: int) -> dict | None:
    sig = engine.recruitment_signal(conn, cid, _now())
    if not sig:
        return None
    mark = conn.execute("SELECT at FROM recruit_marks WHERE client_id=?", (cid,)).fetchone()
    if mark and mark["at"] >= sig["last_at"]:
        return None                    # клиент уже ответил; новый незнакомец покажет предупреждение снова
    return {"senders": sig["senders"], "amount": sig["amount"], "incoming_ids": sig["incoming_ids"]}


@app.post("/recruit/return-all")
def recruit_return_all(s: dict = Depends(as_client)):
    """Вернуть всем отправителям сразу: так клиент выходит из схемы, не став звеном цепочки."""
    cid = s["client_id"]
    sig = _recruit_view(cid)
    if not sig:
        raise HTTPException(409, t("recruit_none"))
    available = max(0, _available(cid))
    if available < sig["amount"]:
        raise HTTPException(400, t("return_no_funds", available=_money(available), amount=_money(sig["amount"])))
    for tid in sig["incoming_ids"]:
        _return_incoming(s, _incoming_row(tid))
    return {"returned": len(sig["incoming_ids"]), "amount": sig["amount"]}


@app.post("/recruit/dismiss")
def recruit_dismiss(s: dict = Depends(as_client)):
    """«Это мои покупатели или знакомые». Ответ сохраняется: его увидит сотрудник, если случай дойдёт до него."""
    cid = s["client_id"]
    sig = _recruit_view(cid)
    if not sig:
        raise HTTPException(409, t("recruit_none"))
    conn.execute("INSERT OR REPLACE INTO recruit_marks(client_id,action,at) VALUES(?,'dismissed',?)",
                 (cid, _now().isoformat()))
    log(f"client:{cid}", "recruit_dismissed", None, None, senders=sig["senders"], amount=sig["amount"])
    return {"ok": True}


# ---------- «Меня втянули в схему»: клиент сам сообщает банку ----------

class SelfReportIn(BaseModel):
    kind: str                       # card | transit | offer
    details: str = Field("", max_length=3000)


def _self_report_number(row) -> str:
    return f"СО-{row['at'][:4]}-{row['id']:05d}"


def _self_report_view(cid: int) -> dict | None:
    row = conn.execute("SELECT * FROM self_reports WHERE client_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "kind": row["kind"], "at": row["at"], "number": _self_report_number(row),
            "code": row["code"]}


@app.post("/self-report")
def self_report(body: SelfReportIn, s: dict = Depends(as_client)):
    """Клиент сам рассказывает банку, что его втянули в схему, пока на него никто не пожаловался.

    Передал карту или доступ: счёт закрывается целиком, потому что им управляет чужой человек.
    Принимал и пересылал: замораживаются только чужие деньги на счёте. Только предложили: ограничений нет.
    Обращение сразу попадает сотруднику банка, клиент получает подтверждение с датой и кодом проверки.
    """
    cid, now = s["client_id"], _now()
    if body.kind not in SELF_REPORT_KINDS:
        raise HTTPException(400, t("bad_input"))
    if _client(cid)["status"] != "active":
        raise HTTPException(409, t("sr.restricted"))
    details = body.details.strip()[:2000]
    if body.kind != "offer" and len(details) < 20:
        raise HTTPException(400, t("sr.short"))
    while True:
        code = secrets.token_hex(4).upper()
        if not conn.execute("SELECT 1 FROM clearances WHERE code=? UNION SELECT 1 FROM self_reports WHERE code=?",
                            (code, code)).fetchone():
            break
    case_id = None
    if body.kind != "offer":
        # Чужие деньги, которые ещё на счёте, и последняя пересылка: по ней сотрудник увидит карту сборщика.
        foreign = engine._stranger_incomings(conn, cid, now)
        episode = (engine.forward_episodes(conn, cid, now) or [None])[-1]
        inc_id = episode["incoming_id"] if episode else max((i["id"] for i in foreign), default=None)
        on_account = sum(i["amount"] for i in foreign if not engine.find_forward(conn, cid, i))
        frozen = max(0, min(on_account, _available(cid)))
        scope = "full" if body.kind == "card" else "partial"
        reasons = [{"code": "self_report", "points": 0, "text": "Клиент сам сообщил банку: " + SELF_REPORT_KINDS[body.kind][0].lower() + SELF_REPORT_KINDS[body.kind][1:]}]
        case_id = conn.execute(
            """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,status,explanation,
                                 submitted_at,due_at,created_at,scope,frozen)
               VALUES(?,?,?,'block',0,?,'under_review',?,?,?,?,?,?)""",
            (cid, episode["outgoing_id"] if episode else None, inc_id, json.dumps(reasons, ensure_ascii=False),
             details, now.isoformat(), (now + timedelta(days=APPEAL_SLA_DAYS)).isoformat(), now.isoformat(),
             scope, frozen)).lastrowid
        _refresh_status(cid)
        text = ("Клиент сам сообщил, что передал карту или доступ: счёт приостановлен, карта и доступ "
                "к приложению подлежат перевыпуску (в демо — запись в истории)" if scope == "full" else
                f"Клиент сам сообщил, что принимал и пересылал чужие деньги: заморожено {_money(frozen)}, "
                "остальным клиент пользуется, но только для знакомых получателей")
        log(f"client:{cid}", "self_report", case_id, inc_id, text=text, kind=body.kind, scope=scope)
    rid = conn.execute("INSERT INTO self_reports(client_id,kind,details,case_id,code,at) VALUES(?,?,?,?,?,?)",
                       (cid, body.kind, details or None, case_id, code, now.isoformat())).lastrowid
    if case_id is None:
        log(f"client:{cid}", "self_report", None, None, kind=body.kind)
    return {"report": _self_report_view(cid), "text": t("sr.done." + body.kind), "report_id": rid}


@app.get("/clients/{cid}/self-report", include_in_schema=False)
def self_report_page(cid: int, request: Request, s: dict = Depends(session)):
    """Подтверждение обращения: дата, время и что именно клиент сообщил. Можно приложить к заявлению в полицию."""
    c = _own_client(s, cid)
    row = conn.execute("SELECT * FROM self_reports WHERE client_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, t("sr.none"))
    verify = str(request.base_url) + f"verify/{row['code']}"
    body = f"""<h1>{escape(t("sr.title"))}</h1>
<div class="muted">№ {escape(_self_report_number(row))} · {escape(row["at"][:16].replace("T", " "))}</div>
<dl><dt>{escape(t("cert.client"))}</dt><dd>{escape(i18n.name(c["id"], c["name"]))}</dd>
<dt>{escape(t("sr.what"))}</dt><dd>{escape(t("sr.kind." + row["kind"]))}</dd>
<dt>{escape(t("sr.before"))}</dt><dd>{escape(t("sr.before_yes"))}</dd></dl>
{f'<p>{escape(t("sr.words"))} «{escape(row["details"])}»</p>' if row["details"] else ''}
<p>{escape(t("sr.text"))}</p><p>{escape(t("sr.law"))}</p>
<p class="muted">{escape(t("sr.verify"))}<br><b>{escape(verify)}</b><br>{escape(t("cert.code"))}: <b>{row["code"]}</b></p>
<button onclick="print()">{escape(t("cert.print"))}</button>
<div class="demo">{escape(t("cert.demo"))}</div>"""
    return _page(t("sr.title"), body)


# ---------- Панель аналитика ----------

@app.get("/analyst", include_in_schema=False)
def analyst_app():
    return FileResponse(os.path.join(STATIC, "analyst.html"))


def _transfer_view(tid: int | None) -> dict | None:
    if tid is None:
        return None
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
        "status_text": "Ждёт подтверждения доверенного лица"
                       if case["status"] == "open" and case["guardian_state"] == "asked" else
                       ANALYST_STATUS.get((case["kind"], case["status"]), STATUS_TEXT.get(case["status"], case["status"])),
        "client_id": client["id"], "client": client["name"], "score": case["score"],
        "created_at": case["created_at"],
        "role": engine.assess_role(conn, case["client_id"], case, _now()),
        "needs_decision": _pending(case),
        "due_at": case["due_at"],
        "overdue": bool(_pending(case) and case["due_at"] and case["due_at"] < _now().isoformat()),
        "from_complaint": case["complaint_id"] is not None,
        "hop": case["hop"],
        "scope": case["scope"] if case["kind"] == "block" else None,
        "frozen": case["frozen"],
        "self_reported": json.loads(case["reasons"])[0].get("code") == "self_report",
    }


@app.get("/analyst/cases")
def analyst_queue(s: dict = Depends(as_analyst)):
    """Очередь: сначала случаи, которые ждут решения."""
    rows = [_analyst_row(c) for c in conn.execute("SELECT * FROM cases ORDER BY created_at DESC, id DESC").fetchall()]
    order = {"under_review": 0, "open": 1}
    # Сначала просроченные, затем ждущие решения по ближайшему сроку.
    return sorted(rows, key=lambda r: (not r["needs_decision"], not r["overdue"], order.get(r["status"], 2),
                                       r["due_at"] or "9999"))


def _chain(case) -> list[dict]:
    """Все счета, приостановленные по той же жалобе: сотрудник видит цепочку целиком."""
    if case["complaint_id"] is None:
        return []
    rows = conn.execute(
        """SELECT k.id, k.hop, k.status, k.transfer_id, k.scope, k.frozen, c.name, i.amount FROM cases k
           JOIN clients c ON c.id=k.client_id JOIN transfers i ON i.id=k.incoming_transfer_id
           WHERE k.complaint_id=? ORDER BY k.hop, k.id""", (case["complaint_id"],)).fetchall()
    victim = conn.execute(
        """SELECT c.name, t.amount FROM complaints p JOIN transfers t ON t.id=p.transfer_id
           JOIN clients c ON c.id=p.complainant_id WHERE p.id=?""", (case["complaint_id"],)).fetchone()
    return [{"case_id": None, "hop": 0, "name": victim["name"], "amount": victim["amount"], "role": "victim",
             "status_text": "Пожаловался на мошенничество"}] + [
        {"case_id": r["id"], "hop": r["hop"], "name": r["name"], "amount": r["amount"],
         "role": "forwarded" if r["transfer_id"] else "holder", "current": r["id"] == case["id"],
         "scope": r["scope"], "frozen": r["frozen"] if r["status"] in UNRESOLVED else 0,
         "status_text": ANALYST_STATUS.get(("block", r["status"]), STATUS_TEXT.get(r["status"], r["status"]))}
        for r in rows]


def _history(case_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM events WHERE case_id=? ORDER BY at, id", (case_id,)).fetchall()
    out = []
    for e in rows:
        details = json.loads(e["details"]) if e["details"] else {}
        who = {"system": "Система", "analyst": "Сотрудник банка"}.get(e["actor"], "Клиент")
        out.append({"at": e["at"], "who": who, "action": e["action"],
                    "text": details.get("text") or EVENT_TEXT.get(e["action"], e["action"]), "details": details})
    return out


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
        "submitted_at": case["submitted_at"],
        "history": _history(case_id),
        "chain": _chain(case),
        "linked": _linked_for(case),
        "guardian": {"name": _guardian_name(case["guardian_id"]), "state": case["guardian_state"]}
                    if case["guardian_id"] else None,
        "certificate": _clearance_view(case_id),
        "recruit_warned": bool(conn.execute(
            "SELECT 1 FROM recruit_marks WHERE client_id=? AND at <= ?", (case["client_id"], case["created_at"])).fetchone()),
        "refundable": _refundable(case),
        "complaint": _analyst_complaint(case),
        "self_report": _analyst_self_report(case),
        "can_escalate": case["kind"] == "block" and case["scope"] == "partial" and case["status"] in UNRESOLVED,
    }


def _analyst_complaint(case) -> dict | None:
    if case["complaint_id"] is None:
        return None
    st = _complaint_state(conn.execute("SELECT * FROM complaints WHERE id=?", (case["complaint_id"],)).fetchone())
    return {k: st[k] for k in ("amount", "returned", "frozen", "stage")}


def _analyst_self_report(case) -> dict | None:
    row = conn.execute("SELECT * FROM self_reports WHERE case_id=?", (case["id"],)).fetchone()
    if not row:
        return None
    return {"kind": row["kind"], "kind_text": SELF_REPORT_KINDS[row["kind"]], "details": row["details"],
            "number": _self_report_number(row), "at": row["at"], "code": row["code"]}


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
    else:
        tr = conn.execute("SELECT * FROM transfers WHERE id=?", (case["transfer_id"],)).fetchone()
        if body.decision == "approve":
            if _client(tr["sender_id"])["status"] != "active":
                raise HTTPException(409, "Счёт клиента приостановлен: перевод можно только отменить. "
                                         "Сначала разберите блокировку")
            if engine.afc_status(conn, tr["receiver_id"]) == "black" or _client(tr["receiver_id"])["status"] == "blocked":
                # Пока перевод стоял на паузе, получатель мог попасть в базу мошенников или под блокировку.
                raise HTTPException(409, "Получатель в чёрном списке Антифрод-центра или его счёт приостановлен: "
                                         "перевод можно только отменить")
            # Деньги под этот перевод были зарезервированы, пока он стоял на паузе.
            conn.execute("UPDATE transfers SET status='done' WHERE id=?", (tr["id"],))
            _move(tr["sender_id"], tr["receiver_id"], tr["amount"])
            status = "released"
        else:
            conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (tr["id"],))
            status = "cancelled"
    conn.execute("UPDATE cases SET status=?, decision_comment=?, decided_at=? WHERE id=?",
                 (status, comment or None, _now().isoformat(), case_id))
    # Блокировка, которую сотрудник уже оставил в силе, тоже держит счёт: одобрение другой её не снимает.
    cleared = case["kind"] == "block" and _refresh_status(case["client_id"]) == "active" and status == "unblocked"
    # Вместе с решением сохраняется подсказка, которую видел сотрудник: её потом не пересчитать задним числом.
    role = engine.assess_role(conn, case["client_id"], case, _now())
    log("analyst", "decision", case_id, case["transfer_id"], text=f"Решение: {STATUS_TEXT[status]}",
        status=status, comment=comment or None, role=role["label"], role_score=role["score"])
    if cleared:
        _issue_clearance(case)
    return _analyst_case(case_id)


@app.get("/quality", include_in_schema=False)
def quality_page():
    """Страница качества движка: цифры по нескольким наборам синтетических данных (python -m app.quality)."""
    return FileResponse(os.path.join(STATIC, "quality.html"))


@app.get("/quality.json", include_in_schema=False)
def quality_data():
    path = os.path.join(STATIC, "quality.json")
    if not os.path.exists(path):
        raise HTTPException(404, "Запустите python -m app.quality")
    return FileResponse(path, media_type="application/json")


# ---------- Эффективность, связи между случаями, выгрузка для Антифрод-центра ----------

@app.get("/analyst/stats", include_in_schema=False)
def stats_page():
    return FileResponse(os.path.join(STATIC, "stats.html"))


@app.get("/analyst/metrics")
def analyst_metrics(s: dict = Depends(as_analyst)):
    """Показатели за 30 дней: деньги, нагрузка на сотрудников, скорость апелляций, ошибки системы."""
    row = conn.execute("SELECT value FROM meta WHERE key='history'").fetchone()
    return metrics.compute(conn, _now()) | {"simulated_history": bool(row and row["value"] == "1")}


@app.get("/analyst/linked")
def analyst_linked(s: dict = Depends(as_analyst)):
    """Счета, связанные с несколькими независимыми случаями."""
    return metrics.linked_accounts(conn)


def _linked_for(case) -> dict | None:
    """Куда шли деньги в этом случае и встречался ли этот счёт в других случаях."""
    dest = conn.execute("SELECT receiver_id FROM transfers WHERE id=?", (case["transfer_id"],)).fetchone() \
        if case["transfer_id"] else {"receiver_id": case["client_id"]}
    found = next((a for a in metrics.linked_accounts(conn) if a["client_id"] == dest["receiver_id"]), None)
    if not found:
        return None
    others = [c for c in found["cases"] if c != case["id"]]
    rows = conn.execute(f"""SELECT k.id, k.kind, k.status, c.name FROM cases k JOIN clients c ON c.id=k.client_id
                            WHERE k.id IN ({",".join("?" * len(others))}) ORDER BY k.created_at DESC""",
                        others).fetchall() if others else []
    return found | {"is_client": dest["receiver_id"] == case["client_id"],
                    "other_cases": [dict(r) | {"kind_text": "Блокировка" if r["kind"] == "block" else "Перевод на паузе"}
                                    for r in rows]}


@app.post("/analyst/accounts/{cid}/suspend")
def suspend_account(cid: int, s: dict = Depends(as_analyst)):
    """Приостановить счёт, который всплывает в нескольких случаях: создаётся случай, его видит и клиент."""
    found = next((a for a in metrics.linked_accounts(conn) if a["client_id"] == cid), None)
    if not found:
        raise HTTPException(404, "Счёт не связан с несколькими случаями")
    if conn.execute("""SELECT 1 FROM cases WHERE client_id=? AND kind='block'
                       AND status IN ('open','under_review')""", (cid,)).fetchone():
        raise HTTPException(409, "По этому счёту уже есть нерешённая блокировка")
    inc = conn.execute("SELECT id FROM transfers WHERE receiver_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                       (cid,)).fetchone()
    reasons = [{"code": "linked", "points": 40,
                "text": f"Счёт получал деньги от {found['sources']} разных клиентов в подозрительных случаях"}]
    case_id = conn.execute(
        """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at)
           VALUES(?,NULL,?,'block',40,?,?)""",
        (cid, inc["id"], json.dumps(reasons, ensure_ascii=False), _now().isoformat())).lastrowid
    _refresh_status(cid)
    log("analyst", "blocked", case_id, inc["id"],
        text=f"Операции приостановлены сотрудником: счёт связан с {len(found['cases'])} случаями")
    return _analyst_case(case_id)


@app.post("/analyst/cases/{case_id}/escalate")
def escalate(case_id: int, s: dict = Depends(as_analyst)):
    """Сотрудник не согласен с подсказкой системы: соразмерная блокировка становится полной."""
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Случай не найден")
    if case["kind"] != "block" or case["scope"] != "partial" or case["status"] not in UNRESOLVED:
        raise HTTPException(409, "По этому случаю счёт уже приостановлен целиком или ограничения сняты")
    conn.execute("UPDATE cases SET scope='full' WHERE id=?", (case_id,))
    _refresh_status(case["client_id"])
    log("analyst", "escalated", case_id, None)
    return _analyst_case(case_id)


@app.post("/analyst/cases/{case_id}/report")
def antifraud_report(case_id: int, s: dict = Depends(as_analyst)):
    """Сведения по случаю для передачи в Антифрод-центр Нацбанка.

    Набор полей взят из требований к банкам: клиент, платёжные реквизиты, IP-адрес, геолокация, номера
    телефонов. Формат условный: точную схему обмена задаёт Антифрод-центр. Технических данных в демо нет.
    """
    card = _analyst_case(case_id)
    client = _client(card["client_id"])
    ops = [x for x in (card["incoming"], card["outgoing"]) if x]
    report = {
        "format": "Сведения о подозрительной операции (прототип, формат условный)",
        "generated_at": _now().isoformat(),
        "generated_by": s["name"],
        "case": {"id": card["case_id"], "kind": card["kind"], "created_at": card["created_at"],
                 "status": card["status"], "status_text": card["status_text"], "score": card["score"],
                 "decision_comment": card["decision_comment"], "decided_at": card["decided_at"]},
        "client": {"id": client["id"], "name": client["name"], "account_opened_at": client["account_opened_at"],
                   "status": client["status"], "role_hint": card["role"]["label"]},
        "operations": [{"id": o["id"], "created_at": o["created_at"], "amount_kzt": o["amount"], "status": o["status"],
                        "sender": o["sender"], "receiver": o["receiver"]} for o in ops],
        "reasons": [{"code": r.get("code"), "points": r.get("points"), "text": r.get("text")} for r in card["reasons"]],
        "complaint_chain": [{k: x.get(k) for k in ("hop", "name", "amount", "role", "status_text")} for x in card["chain"]],
        "linked_account": ({k: card["linked"][k] for k in ("client_id", "name", "sources", "amount")}
                           if card["linked"] else None),
        "client_explanation": card["explanation"],
        "documents": [{"filename": d["filename"], "title": d["title"], "size": d["size"]} for d in card["documents"]],
        "technical": {"ip": None, "geolocation": None, "phone_numbers": None, "device_id": None,
                      "note": "В демо-данных нет. В банке берутся из журналов мобильного приложения."},
        "history": [{"at": h["at"], "who": h["who"], "text": h["text"]} for h in card["history"]],
    }
    log("analyst", "report", case_id, None)
    body = json.dumps(report, ensure_ascii=False, indent=2)
    return RawResponse(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="antifraud-case-{case_id}.json"', "Cache-Control": "no-store"})


@app.post("/demo/reset")
def demo_reset(s: dict = Depends(as_analyst)):
    """Возвращает демо-данные в исходное состояние: даты отсчитываются от текущего момента."""
    ensure_demo(force=True)
    for token in [t for t, v in SESSIONS.items() if v["role"] == "client"]:
        SESSIONS.pop(token)
    return {"ok": True}

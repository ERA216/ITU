"""API демо-банка.  Запуск:  uvicorn app.main:app --reload"""
import base64
import json
import os
import re
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, engine

app = FastAPI(title="Чистый счёт: защита от статуса дроппера")
conn = db.connect()
STATIC = os.path.join(os.path.dirname(__file__), "static")
UPLOADS = os.environ.get("CA_UPLOADS", os.path.join(os.path.dirname(__file__), "..", "uploads"))
MAX_FILE = 5 * 1024 * 1024

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
}


class TransferIn(BaseModel):
    sender_id: int
    receiver_id: int
    amount: int


class DocumentIn(BaseModel):
    kind: str
    filename: str
    content_base64: str


class AppealIn(BaseModel):
    explanation: str


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _client(cid: int):
    row = conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Клиент не найден")
    return row


def _move(sender_id: int, receiver_id: int, amount: int):
    conn.execute("UPDATE clients SET balance = balance - ? WHERE id=?", (amount, sender_id))
    conn.execute("UPDATE clients SET balance = balance + ? WHERE id=?", (amount, receiver_id))


@app.get("/", include_in_schema=False)
def client_app():
    """Экраны клиента."""
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/demo/clients")
def demo_clients():
    """Персонажи для показа: между ними переключается демо-экран."""
    rows = conn.execute("SELECT id, name FROM clients WHERE id >= 1001 ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.get("/clients/{cid}")
def client_feed(cid: int):
    """Профиль и лента операций клиента."""
    c = _client(cid)
    rows = conn.execute(
        """SELECT t.id, t.amount, t.created_at, t.status, t.return_of,
                  t.sender_id, s.name AS sender, t.receiver_id, r.name AS receiver
           FROM transfers t JOIN clients s ON s.id=t.sender_id JOIN clients r ON r.id=t.receiver_id
           WHERE t.sender_id=? OR t.receiver_id=? ORDER BY t.created_at DESC LIMIT 30""",
        (cid, cid)).fetchall()
    return {"id": c["id"], "name": c["name"], "balance": c["balance"], "status": c["status"],
            "feed": [dict(r) | {"direction": "in" if r["receiver_id"] == cid else "out"} for r in rows]}


@app.post("/transfers")
def create_transfer(body: TransferIn):
    """Создаёт перевод. Если движок видит схему, перевод останавливается и создаётся случай."""
    sender = _client(body.sender_id)
    _client(body.receiver_id)
    if body.sender_id == body.receiver_id or body.amount <= 0:
        raise HTTPException(400, "Некорректный перевод")
    if sender["status"] == "blocked":
        raise HTTPException(403, "Операции по счёту приостановлены")
    if sender["balance"] < body.amount:
        raise HTTPException(400, "Недостаточно средств")

    now = _now()
    res = engine.evaluate(conn, body.sender_id, body.receiver_id, body.amount, now)
    status = "held" if res["decision"] == "hold" else "done"
    cur = conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status) VALUES(?,?,?,?,?)",
        (body.sender_id, body.receiver_id, body.amount, now.isoformat(), status))
    tid = cur.lastrowid
    out = {"transfer_id": tid, "status": status, "score": res["score"], "reasons": res["reasons"]}
    if status == "done":
        _move(body.sender_id, body.receiver_id, body.amount)
    else:
        inc = conn.execute(
            """SELECT t.id, t.amount, t.sender_id, c.name FROM transfers t
               JOIN clients c ON c.id=t.sender_id WHERE t.id=?""", (res["incoming_id"],)).fetchone()
        cur = conn.execute(
            """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (body.sender_id, tid, inc["id"], "intercept", res["score"],
             json.dumps(res["reasons"], ensure_ascii=False), now.isoformat()))
        out |= {"case_id": cur.lastrowid,
                "safe_return": {"to_id": inc["sender_id"], "to_name": inc["name"], "amount": inc["amount"]}}
    conn.commit()
    return out


@app.post("/cases/{case_id}/return-to-sender")
def return_to_sender(case_id: int):
    """Безопасный возврат: деньги уходят только на счёт, с которого пришли."""
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case or case["kind"] != "intercept":
        raise HTTPException(404, "Случай не найден")
    if case["status"] != "open":
        raise HTTPException(409, "По этому случаю решение уже принято")
    inc = conn.execute("SELECT * FROM transfers WHERE id=?", (case["incoming_transfer_id"],)).fetchone()
    now = _now().isoformat()
    conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (case["transfer_id"],))
    cur = conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,return_of) VALUES(?,?,?,?,?,?)",
        (case["client_id"], inc["sender_id"], inc["amount"], now, "done", inc["id"]))
    _move(case["client_id"], inc["sender_id"], inc["amount"])
    conn.execute("UPDATE cases SET status='returned' WHERE id=?", (case_id,))
    conn.commit()
    return {"status": "returned", "return_transfer_id": cur.lastrowid, "amount": inc["amount"]}


@app.post("/cases/{case_id}/insist")
def insist(case_id: int):
    """Клиент настаивает на переводе: деньги остаются на паузе, случай уходит аналитику."""
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case or case["status"] != "open":
        raise HTTPException(404, "Открытый случай не найден")
    conn.execute("UPDATE cases SET status='under_review' WHERE id=?", (case_id,))
    conn.commit()
    return {"status": "under_review"}


@app.get("/cases")
def list_cases():
    rows = conn.execute(
        """SELECT k.*, c.name AS client FROM cases k JOIN clients c ON c.id=k.client_id
           ORDER BY k.created_at DESC""").fetchall()
    return [dict(r) | {"reasons": json.loads(r["reasons"])} for r in rows]


# ---------- Апелляция: клиент заблокирован и объясняет, что произошло ----------

def _block_case(case_id: int):
    case = conn.execute("SELECT * FROM cases WHERE id=? AND kind='block'", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Случай не найден")
    return case


def _money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₸"


def _minutes_text(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60} мин" if minutes < 1440 else f"{minutes // 1440} дн"


def _case_view(case) -> dict:
    inc = conn.execute(
        """SELECT t.amount, t.created_at, c.name FROM transfers t JOIN clients c ON c.id=t.sender_id
           WHERE t.id=?""", (case["incoming_transfer_id"],)).fetchone()
    out = conn.execute(
        """SELECT t.amount, t.created_at, c.name, c.account_opened_at FROM transfers t
           JOIN clients c ON c.id=t.receiver_id WHERE t.id=?""", (case["transfer_id"],)).fetchone()
    gap = int((datetime.fromisoformat(out["created_at"]) - datetime.fromisoformat(inc["created_at"])).total_seconds() // 60)
    docs = conn.execute("SELECT id, kind, filename, size, uploaded_at FROM documents WHERE case_id=? ORDER BY id",
                        (case["id"],)).fetchall()
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
        "required": [{"kind": k, **v, "uploaded": k in have} for k, v in DOC_KINDS.items()],
        "documents": [dict(d) for d in docs],
        "can_submit": case["status"] == "open",
    }


@app.get("/clients/{cid}/case")
def client_case(cid: int):
    """Последний случай блокировки клиента: причина простым языком, документы, статус."""
    _client(cid)
    case = conn.execute("SELECT * FROM cases WHERE client_id=? AND kind='block' ORDER BY id DESC LIMIT 1",
                        (cid,)).fetchone()
    if not case:
        raise HTTPException(404, "Блокировок нет")
    return _case_view(case)


@app.post("/cases/{case_id}/documents")
def upload_document(case_id: int, body: DocumentIn):
    """Клиент прикладывает документ к апелляции."""
    case = _block_case(case_id)
    if case["status"] != "open":
        raise HTTPException(409, "Документы уже отправлены на рассмотрение")
    if body.kind not in DOC_KINDS:
        raise HTTPException(400, "Неизвестный тип документа")
    try:
        data = base64.b64decode(body.content_base64, validate=True)
    except Exception:
        raise HTTPException(400, "Файл повреждён")
    if not data or len(data) > MAX_FILE:
        raise HTTPException(400, "Файл пустой или больше 5 МБ")
    name = re.sub(r"[^\w.\- ]", "_", os.path.basename(body.filename))[:80] or "file"
    now = _now().isoformat()
    cur = conn.execute("INSERT INTO documents(case_id,kind,filename,size,uploaded_at) VALUES(?,?,?,?,?)",
                       (case_id, body.kind, name, len(data), now))
    os.makedirs(UPLOADS, exist_ok=True)
    path = os.path.join(UPLOADS, f"{cur.lastrowid}_{name}")
    with open(path, "wb") as f:
        f.write(data)
    conn.execute("UPDATE documents SET path=? WHERE id=?", (path, cur.lastrowid))
    conn.commit()
    return {"id": cur.lastrowid, "kind": body.kind, "filename": name, "size": len(data)}


@app.post("/cases/{case_id}/submit")
def submit_appeal(case_id: int, body: AppealIn):
    """Клиент отправляет апелляцию: объяснение и обязательные документы."""
    case = _block_case(case_id)
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
    conn.commit()
    return _case_view(_block_case(case_id))

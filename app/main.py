"""API демо-банка.  Запуск:  uvicorn app.main:app --reload"""
import json
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import db, engine

app = FastAPI(title="Чистый счёт: защита от статуса дроппера")
conn = db.connect()


class TransferIn(BaseModel):
    sender_id: int
    receiver_id: int
    amount: int


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

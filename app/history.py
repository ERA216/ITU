"""История случаев за 30 дней для панели эффективности.

Какие переводы остановить, решает настоящий движок: здесь используются его решения по синтетическим
переводам. Что дальше сделали клиенты и сотрудники банка (вернул деньги, настоял, снял блокировку),
смоделировано по вероятностям ниже. Страница /analyst/stats об этом прямо пишет.
"""
import json
import random
from datetime import datetime, timedelta

HISTORY_DAYS = 30
P_RETURN = 0.70            # клиент, которому прислали чужие деньги, сам возвращает их отправителю
P_APPEAL = 0.85            # заблокированный после жалобы клиент подаёт апелляцию
P_UNBLOCK = 0.90           # сотрудник снимает блокировку с невольного участника
EXPLANATIONS = [
    "Мне написали, что перевели деньги по ошибке, и попросили вернуть на карту родственника.",
    "Покупатель оплатил товар, потом попросил вернуть деньги на другую карту, сказал, что передумал.",
    "Позвонили, сказали, что ошиблись номером, и продиктовали карту для возврата.",
]


def simulate(conn, stats: dict, now: datetime, rng: random.Random) -> None:
    since = now - timedelta(days=HISTORY_DAYS)
    for h in stats["held"]:
        if h["t"] >= since and h["res"]["incoming_id"]:
            _intercept(conn, h, now, rng)
    for p in stats["passed"]:
        if p["t"] >= since and p["scen"].startswith("tri_"):
            _missed_scheme(conn, p, now, rng)
    _apply_statuses(conn)


def _ev(conn, at: datetime, actor: str, action: str, case_id: int, transfer_id: int | None, **details):
    conn.execute("INSERT INTO events(at,actor,action,case_id,transfer_id,details) VALUES(?,?,?,?,?,?)",
                 (at.isoformat(), actor, action, case_id, transfer_id,
                  json.dumps(details, ensure_ascii=False) if details else None))


def _later(t: datetime, rng: random.Random, lo_hours: float, hi_hours: float) -> datetime:
    return (t + timedelta(hours=rng.uniform(lo_hours, hi_hours))).replace(microsecond=0)


def _intercept(conn, h: dict, now: datetime, rng: random.Random):
    """Перевод, который движок остановил: что стало с ним дальше."""
    res, t, client = h["res"], h["t"], h["sender"]
    case_id = conn.execute(
        """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (client, h["id"], res["incoming_id"], "intercept", res["score"],
         json.dumps(res["reasons"], ensure_ascii=False), t.isoformat())).lastrowid
    _ev(conn, t, "system", "intercepted", case_id, h["id"], score=res["score"], amount=h["amount"])
    scheme = h["scen"].startswith("tri_")
    if scheme and rng.random() < P_RETURN:
        at = _later(t, rng, 0.05, 0.5)
        inc = conn.execute("SELECT * FROM transfers WHERE id=?", (res["incoming_id"],)).fetchone()
        conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (h["id"],))
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,return_of,scenario) "
                     "VALUES(?,?,?,?,'done',?,'history')",
                     (client, inc["sender_id"], inc["amount"], at.isoformat(), inc["id"]))
        conn.execute("UPDATE cases SET status='returned' WHERE id=?", (case_id,))
        _ev(conn, at, f"client:{client}", "returned", case_id, inc["id"], amount=inc["amount"])
        return
    # Клиент настоял на переводе: решает сотрудник банка.
    insisted = _later(t, rng, 0.02, 0.3)
    _ev(conn, insisted, f"client:{client}", "insisted", case_id, h["id"])
    decided = _later(insisted, rng, 0.5, 10)
    if decided > now:
        conn.execute("UPDATE transfers SET status='held' WHERE id=?", (h["id"],))
        conn.execute("UPDATE cases SET status='under_review' WHERE id=?", (case_id,))
        return
    status = "cancelled" if scheme else "released"
    conn.execute("UPDATE transfers SET status=? WHERE id=?", ("cancelled" if scheme else "done", h["id"]))
    comment = "Получатель похож на дропперский счёт." if scheme else None
    conn.execute("UPDATE cases SET status=?, decision_comment=?, decided_at=? WHERE id=?",
                 (status, comment, decided.isoformat(), case_id))
    _ev(conn, decided, "analyst", "decision", case_id, h["id"],
        text="Решение: " + ("Перевод отменён" if scheme else "Перевод разрешён"), status=status, comment=comment)


def _missed_scheme(conn, p: dict, now: datetime, rng: random.Random):
    """Схема, которую движок пропустил: пострадавший жалуется, счета в цепочке приостанавливаются."""
    victim_tr = conn.execute(
        """SELECT * FROM transfers WHERE receiver_id=? AND scenario='tri_in' AND created_at < ?
           ORDER BY created_at DESC LIMIT 1""", (p["sender"], p["t"].isoformat())).fetchone()
    if not victim_tr:
        return
    complained = _later(p["t"], rng, 20, 70)
    if complained > now - timedelta(hours=1):
        return
    if conn.execute("SELECT 1 FROM complaints WHERE transfer_id=?", (victim_tr["id"],)).fetchone():
        return                     # раздробленная сумма: жалоба одна на обе части
    complaint_id = conn.execute(
        "INSERT INTO complaints(transfer_id,complainant_id,outcome,created_at) VALUES(?,?,?,?)",
        (victim_tr["id"], victim_tr["sender_id"], "blocked", complained.isoformat())).lastrowid
    complaint_reason = {"code": "complaint", "points": 30,
                        "text": "Отправитель денег сообщил банку, что стал жертвой мошенников"}
    chain_reason = {"code": "complaint_chain", "points": 30,
                    "text": "Деньги пришли по цепочке переводов от человека, который пожаловался на мошенничество"}

    # Звено 1: тот, кто переслал деньги. Почти всегда невольный участник.
    fwd_case = conn.execute(
        """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at,complaint_id,hop)
           VALUES(?,?,?,'block',?,?,?,?,1)""",
        (p["sender"], p["id"], victim_tr["id"], min(100, p["res"]["score"] + 30),
         json.dumps([complaint_reason] + p["res"]["reasons"], ensure_ascii=False), complained.isoformat(),
         complaint_id)).lastrowid
    conn.execute("UPDATE complaints SET case_id=? WHERE id=?", (fwd_case, complaint_id))
    _ev(conn, complained, f"client:{victim_tr['sender_id']}", "complaint", fwd_case, victim_tr["id"],
        amount=victim_tr["amount"])
    _ev(conn, complained, "system", "blocked", fwd_case, p["id"],
        text=f"Операции приостановлены: деньги переслали дальше ({p['amount']:,} ₸)".replace(",", " "), hop=1)
    if rng.random() < P_APPEAL:
        submitted = _later(complained, rng, 3, 30)
        if submitted <= now:
            conn.execute("UPDATE cases SET status='under_review', explanation=?, submitted_at=?, due_at=? WHERE id=?",
                         (rng.choice(EXPLANATIONS), submitted.isoformat(),
                          (submitted + timedelta(days=3)).isoformat(), fwd_case))
            _ev(conn, submitted, f"client:{p['sender']}", "appeal", fwd_case, None)
            decided = _later(submitted, rng, 4, 90)
            if decided <= now:
                status = "unblocked" if rng.random() < P_UNBLOCK else "kept"
                comment = None if status == "unblocked" else "Объяснение не подтверждено перепиской."
                conn.execute("UPDATE cases SET status=?, decision_comment=?, decided_at=? WHERE id=?",
                             (status, comment, decided.isoformat(), fwd_case))
                _ev(conn, decided, "analyst", "decision", fwd_case, p["id"],
                    text="Решение: " + ("Ограничения сняты" if status == "unblocked" else "Ограничения оставлены"),
                    status=status, comment=comment)
                if status == "unblocked":
                    cl = conn.execute("INSERT INTO clearances(case_id,client_id,code,issued_at) VALUES(?,?,?,?)",
                                      (fwd_case, p["sender"], f"{rng.getrandbits(32):08X}", decided.isoformat())).lastrowid
                    _ev(conn, decided, "system", "cleared", fwd_case, None,
                        text=f"Выдана справка № ЧС-{decided.year}-{cl:05d}; в Антифрод-центр переданы сведения "
                             "о снятии ограничений и выводе банка (имитация)")

    # Звено 2: карта, на которой остались деньги. Апелляций от таких счетов не бывает.
    holder_case = conn.execute(
        """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at,complaint_id,hop)
           VALUES(?,NULL,?,'block',30,?,?,?,2)""",
        (p["receiver"], p["id"], json.dumps([chain_reason], ensure_ascii=False), complained.isoformat(),
         complaint_id)).lastrowid
    _ev(conn, complained, f"client:{victim_tr['sender_id']}", "complaint", holder_case, victim_tr["id"],
        amount=victim_tr["amount"])
    _ev(conn, complained, "system", "blocked", holder_case, p["id"],
        text=f"Операции приостановлены: на счёте остались деньги из цепочки ({p['amount']:,} ₸)".replace(",", " "),
        hop=2)


def _apply_statuses(conn):
    """Счёт приостановлен, пока по нему есть нерешённая или оставленная в силе блокировка."""
    conn.execute("""UPDATE clients SET status='blocked' WHERE id IN (
                      SELECT client_id FROM cases WHERE kind='block' AND status IN ('open','under_review','kept'))""")

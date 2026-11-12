"""Движок перехвата схемы «ошибочный перевод».

Ищет связку: входящий перевод от незнакомца -> исходящий третьему лицу
на похожую сумму. Каждый признак даёт баллы и текст причины, поэтому
решение можно объяснить и клиенту, и аналитику.
"""
from datetime import datetime, timedelta

HOLD_THRESHOLD = 75
LOOKBACK_HOURS = 72
YOUNG_ACCOUNT_DAYS = 30


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _had_contact(conn, a: int, b: int, before: str, exclude_id: int | None = None) -> bool:
    row = conn.execute(
        """SELECT 1 FROM transfers
           WHERE status='done' AND created_at < ? AND id IS NOT ?
             AND ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))
           LIMIT 1""",
        (before, exclude_id, a, b, b, a),
    ).fetchone()
    return row is not None


def _stranger_incomings(conn, client_id: int, now: datetime):
    """Недавние входящие от тех, с кем раньше не было переводов, ещё не возвращённые."""
    since = (now - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    rows = conn.execute(
        """SELECT t.* FROM transfers t
           WHERE t.receiver_id=? AND t.status='done' AND t.return_of IS NULL
             AND t.created_at >= ? AND t.created_at <= ?
             AND NOT EXISTS (SELECT 1 FROM transfers r
                             WHERE r.return_of = t.id AND r.status='done')""",
        (client_id, since, now.isoformat()),
    ).fetchall()
    return [r for r in rows
            if not _had_contact(conn, client_id, r["sender_id"], r["created_at"], r["id"])]


def evaluate(conn, sender_id: int, receiver_id: int, amount: int, now: datetime) -> dict:
    """Оценивает исходящий перевод до его проведения."""
    reasons: list[dict] = []
    score = 0

    def add(code: str, points: int, text: str):
        nonlocal score
        score += points
        reasons.append({"code": code, "points": points, "text": text})

    incomings = _stranger_incomings(conn, sender_id, now)

    # Возврат тому же отправителю безопасен по определению.
    if any(i["sender_id"] == receiver_id for i in incomings):
        return {"score": 0, "decision": "allow", "reasons": [], "incoming_id": None}

    incoming = None
    if incomings:
        # Берём входящий, наиболее близкий по сумме к исходящему.
        incoming = min(incomings, key=lambda i: abs(i["amount"] - amount) / i["amount"])
        add("stranger_incoming", 25,
            "Недавно пришли деньги от человека, с которым раньше не было переводов")
        add("not_to_sender", 15,
            "Деньги уходят не тому, кто их прислал, а третьему лицу")
        if 0.8 * incoming["amount"] <= amount <= 1.02 * incoming["amount"]:
            add("amount_match", 30, "Сумма почти совпадает с полученной от незнакомца")
        gap = now - _dt(incoming["created_at"])
        if gap <= timedelta(hours=2):
            add("fast_forward", 15, "Перевод отправляется меньше чем через 2 часа после получения")
        elif gap <= timedelta(hours=24):
            add("same_day_forward", 10, "Перевод отправляется в течение суток после получения")

    if not _had_contact(conn, sender_id, receiver_id, now.isoformat()):
        add("new_receiver", 10, "Клиент впервые переводит этому получателю")

    rec = conn.execute("SELECT account_opened_at FROM clients WHERE id=?", (receiver_id,)).fetchone()
    if rec and now - _dt(rec["account_opened_at"]) < timedelta(days=YOUNG_ACCOUNT_DAYS):
        add("young_receiver", 10, "Счёт получателя открыт меньше месяца назад")

    hold = incoming is not None and score >= HOLD_THRESHOLD
    return {
        "score": min(score, 100),
        "decision": "hold" if hold else "allow",
        "reasons": reasons,
        "incoming_id": incoming["id"] if incoming else None,
    }

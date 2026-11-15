"""Движок перехвата схемы «ошибочный перевод».

Ищет связку: входящий перевод от незнакомца -> исходящий третьему лицу
на похожую сумму. Каждый признак даёт баллы и текст причины, поэтому
решение можно объяснить и клиенту, и аналитику.
"""
from datetime import datetime, timedelta

HOLD_THRESHOLD = 85
# Без чужих денег на счёте перевод останавливается только при сильном внешнем сигнале
# (звонок с подозрительного номера, получатель в «сером» списке) и новом получателе.
HOLD_THRESHOLD_NO_INCOMING = 60
CALL_SIGNAL_MINUTES = 15            # сколько длится сигнал оператора о подозрительном звонке
LOOKBACK_HOURS = 120
YOUNG_ACCOUNT_DAYS = 30
# Признаки вербовки в дропперы: деньги от нескольких незнакомцев за неделю на новый счёт или с пересылкой.
RECRUIT_DAYS = 7
RECRUIT_SENDERS = 3
RECRUIT_MIN_AMOUNT = 20_000
RECRUIT_ACCOUNT_DAYS = 90
SCAM_MESSAGE_MINUTES = 60           # сколько действует признак «клиент проверил сообщение, похожее на схему»


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


def _forwarded_to_new(conn, client_id: int, incoming) -> int:
    """Сколько денег после входящего ушло получателям, с которыми до него не было переводов."""
    rows = conn.execute(
        """SELECT receiver_id, amount FROM transfers
           WHERE sender_id=? AND status='done' AND return_of IS NULL
             AND created_at > ? AND receiver_id != ?""",
        (client_id, incoming["created_at"], incoming["sender_id"])).fetchall()
    return sum(r["amount"] for r in rows
               if not _had_contact(conn, client_id, r["receiver_id"], incoming["created_at"]))


def evaluate(conn, sender_id: int, receiver_id: int, amount: int, now: datetime) -> dict:
    """Оценивает исходящий перевод до его проведения."""
    reasons: list[dict] = []
    score = 0

    def add(code: str, points: int, text: str):
        nonlocal score
        score += points
        reasons.append({"code": code, "points": points, "text": text})

    incomings = _stranger_incomings(conn, sender_id, now)

    # Возврат тому же отправителю безопасен, но только в пределах того, что он прислал. Иначе мошенник
    # присылает 1 000 ₸ и просит перевести на ту же карту 150 000: такой перевод проверяется как обычный.
    from_receiver = [i for i in incomings if i["sender_id"] == receiver_id]
    if from_receiver and amount <= sum(i["amount"] for i in from_receiver):
        return {"score": 0, "decision": "allow", "reasons": [], "incoming_id": None, "return_to_sender": True}
    incomings = [i for i in incomings if i["sender_id"] != receiver_id]

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
        elif not _had_contact(conn, sender_id, receiver_id, incoming["created_at"]):
            # Дробление: считаем всё, что после этого входящего уже ушло новым получателям.
            sent = _forwarded_to_new(conn, sender_id, incoming)
            # Срабатывает на той части, которая добирает сумму; если деньги уже ушли раньше, молчит.
            if 0 < sent < 0.8 * incoming["amount"] <= sent + amount <= 1.1 * incoming["amount"]:
                add("split_forward", 40,
                    "Вместе с прошлыми переводами новым получателям уходит почти вся сумма от незнакомца")
            elif len({i["sender_id"] for i in incomings}) >= 2:
                # Сбор: деньги нескольких незнакомцев уходят одним переводом. Так работает «подработка» дроппера.
                pooled = sum(i["amount"] for i in incomings)
                if 0.8 * pooled <= amount <= 1.1 * pooled:
                    add("pooled_forward", 40, "Уходит почти вся сумма, которую прислали несколько незнакомых людей")
        gap = now - _dt(incoming["created_at"])
        if gap <= timedelta(hours=2):
            add("fast_forward", 15, "Перевод отправляется меньше чем через 2 часа после получения")
        elif gap <= timedelta(hours=24):
            add("same_day_forward", 10, "Перевод отправляется в течение суток после получения")

    new = not _had_contact(conn, sender_id, receiver_id, now.isoformat())
    if new:
        add("new_receiver", 10, "Клиент впервые переводит этому получателю")

    rec = conn.execute("SELECT account_opened_at FROM clients WHERE id=?", (receiver_id,)).fetchone()
    if rec and now - _dt(rec["account_opened_at"]) < timedelta(days=YOUNG_ACCOUNT_DAYS):
        add("young_receiver", 10, "Счёт получателя открыт меньше месяца назад")

    # Внешние сигналы: база Антифрод-центра и оператор связи («Телеком Антифрод»).
    if afc_status(conn, receiver_id) == "grey":
        add("afc_grey", 50, "Получатель в списке подозрительных счетов Антифрод-центра")
    if call_active(conn, sender_id, now):
        add("suspicious_call", 50, "Во время перевода клиенту звонят с номера, который оператор связи пометил как подозрительный")
    if new and scam_message(conn, sender_id, now):
        add("scam_message", 40, "Клиент недавно проверял сообщение, и оно похоже на схему мошенников")

    if incoming is not None:
        hold = score >= HOLD_THRESHOLD
    else:
        hold = score >= HOLD_THRESHOLD_NO_INCOMING
    return {
        "score": min(score, 100),
        "decision": "hold" if hold else "allow",
        "reasons": reasons,
        "incoming_id": incoming["id"] if incoming else None,
    }


def recruitment_signal(conn, client_id: int, now: datetime) -> dict | None:
    """Похоже, что счёт используют для приёма чужих денег: несколько незнакомцев за неделю.

    Одних входящих мало (так выглядит и честный продавец), поэтому нужен ещё один признак:
    счёт открыт недавно или деньги уже пересылались новым получателям.
    """
    since = (now - timedelta(days=RECRUIT_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT t.* FROM transfers t WHERE t.receiver_id=? AND t.status='done' AND t.return_of IS NULL
             AND t.amount >= ? AND t.created_at >= ? AND t.created_at <= ?
             AND NOT EXISTS (SELECT 1 FROM transfers r WHERE r.return_of=t.id AND r.status='done')
           ORDER BY t.created_at""", (client_id, RECRUIT_MIN_AMOUNT, since, now.isoformat())).fetchall()
    strangers = [r for r in rows if not _had_contact(conn, client_id, r["sender_id"], r["created_at"], r["id"])]
    senders = {r["sender_id"] for r in strangers}
    if len(senders) < RECRUIT_SENDERS:
        return None
    client = conn.execute("SELECT account_opened_at FROM clients WHERE id=?", (client_id,)).fetchone()
    young = now - _dt(client["account_opened_at"]) < timedelta(days=RECRUIT_ACCOUNT_DAYS)
    forwarded = any(find_forward(conn, client_id, r) for r in strangers)
    if not (young or forwarded):
        return None
    return {"senders": len(senders), "amount": sum(r["amount"] for r in strangers),
            "incoming_ids": [r["id"] for r in strangers], "last_at": strangers[-1]["created_at"],
            "young": young, "forwarded": forwarded}


def scam_message(conn, client_id: int, now: datetime) -> str | None:
    """Схема из сообщения, которое клиент проверил за последний час (если оно похоже на мошенничество)."""
    since = (now - timedelta(minutes=SCAM_MESSAGE_MINUTES)).isoformat()
    row = conn.execute("""SELECT scheme FROM message_checks WHERE client_id=? AND verdict='danger'
                          AND at >= ? AND at <= ? ORDER BY at DESC LIMIT 1""",
                       (client_id, since, now.isoformat())).fetchone()
    return row["scheme"] if row else None


def afc_status(conn, client_id: int) -> str | None:
    row = conn.execute("SELECT list FROM afc_list WHERE client_id=?", (client_id,)).fetchone()
    return row["list"] if row else None


def call_active(conn, client_id: int, now: datetime) -> bool:
    row = conn.execute("SELECT started_at FROM call_signals WHERE client_id=?", (client_id,)).fetchone()
    return bool(row) and timedelta(0) <= now - _dt(row["started_at"]) <= timedelta(minutes=CALL_SIGNAL_MINUTES)


def find_forward(conn, client_id: int, incoming) -> list:
    """Исходящие, которыми клиент переслал деньги этого входящего дальше (одним переводом или частями).

    Учитываются переводы в течение окна получателям, с которыми до входящего не было переводов.
    Пусто, если пересылки не было.
    """
    limit = (_dt(incoming["created_at"]) + timedelta(hours=LOOKBACK_HOURS)).isoformat()
    rows = conn.execute(
        """SELECT * FROM transfers WHERE sender_id=? AND status='done' AND return_of IS NULL
           AND receiver_id != ? AND created_at > ? AND created_at <= ? ORDER BY created_at, id""",
        (client_id, incoming["sender_id"], incoming["created_at"], limit)).fetchall()
    taken, total = [], 0
    for r in rows:
        if _had_contact(conn, client_id, r["receiver_id"], incoming["created_at"]):
            continue
        taken.append(r)
        total += r["amount"]
        if total >= 0.8 * incoming["amount"]:
            return taken if total <= 1.1 * incoming["amount"] else []
    return []


def forward_episodes(conn, client_id: int, until: datetime, days: int = 90) -> list[dict]:
    """Эпизоды «получил от незнакомца и переслал третьему лицу» за последние дни."""
    since = (until - timedelta(days=days)).isoformat()
    incomings = conn.execute(
        """SELECT * FROM transfers WHERE receiver_id=? AND status='done' AND return_of IS NULL
           AND created_at >= ? AND created_at <= ? ORDER BY created_at""",
        (client_id, since, until.isoformat())).fetchall()
    episodes = []
    for inc in incomings:
        if _had_contact(conn, client_id, inc["sender_id"], inc["created_at"], inc["id"]):
            continue
        limit = (_dt(inc["created_at"]) + timedelta(hours=LOOKBACK_HOURS)).isoformat()
        out = conn.execute(
            """SELECT * FROM transfers WHERE sender_id=? AND status='done' AND receiver_id != ?
               AND created_at > ? AND created_at <= ? AND amount BETWEEN ? AND ?
               ORDER BY created_at LIMIT 1""",
            (client_id, inc["sender_id"], inc["created_at"], limit,
             0.8 * inc["amount"], 1.02 * inc["amount"])).fetchone()
        if out:
            episodes.append({"incoming_id": inc["id"], "outgoing_id": out["id"], "from": inc["sender_id"]})
    return episodes


def assess_role(conn, client_id: int, case, now: datetime) -> dict:
    """Подсказка аналитику: невольный участник или организатор. Решение остаётся за человеком."""
    client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    factors: list[dict] = []
    score = 0

    def add(points: int, text: str):
        nonlocal score
        score += points
        factors.append({"points": points, "text": text})

    age = (now - _dt(client["account_opened_at"])).days
    if age >= 180:
        add(2, f"Счёт открыт давно: {age} дн. назад")
    elif age < 30:
        add(-2, f"Счёт открыт недавно: {age} дн. назад")

    episodes = forward_episodes(conn, client_id, now)
    if not episodes:
        pass                      # деньги остались на счёте: о пересылке судить не по чему
    elif len(episodes) == 1:
        add(2, "Пересылка чужих денег была один раз")
    elif len(episodes) >= 3:
        add(-3, f"Пересылка чужих денег повторялась, эпизодов за 90 дней: {len(episodes)}")
    else:
        add(-1, "Пересылка чужих денег была дважды")
    senders = {e["from"] for e in episodes}
    if len(senders) >= 3:
        add(-2, f"Деньги приходили от {len(senders)} разных незнакомых людей")

    first = min((e["incoming_id"] for e in episodes), default=case["incoming_transfer_id"])
    before = conn.execute("SELECT created_at FROM transfers WHERE id=?", (first,)).fetchone() if first else None
    cutoff = before["created_at"] if before else now.isoformat()
    usual = conn.execute(
        """SELECT COUNT(*) AS n FROM transfers WHERE status='done' AND created_at < ?
           AND (sender_id=? OR receiver_id=?)""", (cutoff, client_id, client_id)).fetchone()["n"]
    if usual >= 5:
        add(1, f"До этого счётом пользовались обычно: {usual} переводов")
    elif usual == 0:
        add(-1, "До этого по счёту не было ни одного перевода")

    warned = conn.execute("SELECT at FROM recruit_marks WHERE client_id=? AND action='dismissed' AND at <= ?",
                          (client_id, case["created_at"])).fetchone()
    if warned:
        add(-1, "Клиента предупреждали о признаках вербовки, он ответил, что знает отправителей")

    docs = conn.execute("SELECT COUNT(*) AS n FROM documents WHERE case_id=?", (case["id"],)).fetchone()["n"]
    if case["explanation"] and docs:
        add(1, "Клиент сам объяснил ситуацию и приложил документы")

    if score >= 3:
        verdict, label = "unwitting", "Похоже на невольного участника"
    elif score <= -1:
        verdict, label = "organizer", "Похоже на организатора или осознанного дроппера"
    else:
        verdict, label = "unclear", "Данных недостаточно, нужна ручная проверка"
    return {"verdict": verdict, "label": label, "score": score, "factors": factors, "episodes": len(episodes)}

"""Генератор синтетических данных и проверка движка.

Запуск:  python -m app.seed
Создаёт data.db, проигрывает все переводы по порядку через движок
и печатает, сколько схем поймано и сколько честных переводов остановлено зря.
"""
import os
import random
from datetime import datetime, timedelta

from . import db, engine

N_CLIENTS = 300
N_DROPPERS = 12
DAYS = 90          # длина истории
EVAL_DAYS = 45     # движок оцениваем только на последних днях: до этого копится история связей

FIRST = ["Айдана", "Нурлан", "Дина", "Ерлан", "Алия", "Тимур", "Мадина", "Арман", "Сауле",
         "Данияр", "Жанна", "Бекзат", "Асель", "Руслан", "Камила", "Олжас", "Анна", "Игорь"]
LAST = ["А.", "Б.", "К.", "М.", "Н.", "С.", "Т.", "Ж.", "О.", "Е."]


def generate(rng: random.Random, now: datetime):
    start = now - timedelta(days=DAYS)
    clients = []
    for i in range(1, N_CLIENTS + 1):
        dropper = i > N_CLIENTS - N_DROPPERS
        # У большинства дропперских счетов возраст маленький, но не у всех.
        young = dropper and rng.random() < 0.7
        if young:
            opened = now - timedelta(days=rng.randint(30, EVAL_DAYS - 2))
        else:
            opened = now - timedelta(days=rng.randint(DAYS + 40, 2000))
        clients.append({"id": i, "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                        "opened": opened, "young": young,
                        "kind": "dropper" if dropper else "honest"})
    honest = [c["id"] for c in clients if c["kind"] == "honest"]
    droppers = [c for c in clients if c["kind"] == "dropper"]
    contacts = {h: rng.sample([x for x in honest if x != h], rng.randint(3, 8)) for h in honest}

    events = []  # (time, sender, receiver, amount, scenario)

    def t_rand(recent=False):
        # Схемы и похожие на них случаи кладём в оцениваемый период.
        if recent:
            return now - timedelta(seconds=rng.randint(4 * 86400, EVAL_DAYS * 86400))
        return start + timedelta(seconds=rng.randint(0, DAYS * 86400 - 4 * 86400))

    # 1. Обычные переводы между знакомыми.
    for _ in range(12000):
        s = rng.choice(honest)
        events.append((t_rand(), s, rng.choice(contacts[s]), rng.randrange(1000, 80000, 500), "normal"))

    def stranger_for(u):
        return rng.choice([x for x in honest if x != u and x not in contacts[u] and u not in contacts[x]])

    # 2. Схема «ошибочный перевод»: жертва -> невольный участник -> дроппер.
    for k in range(60):
        u = rng.choice(honest)
        victim, d = stranger_for(u), rng.choice(droppers)
        # Молодой дропперский счёт используют в первые недели после открытия.
        t0 = d["opened"] + timedelta(hours=rng.randint(2, 24 * 24)) if d["young"] else t_rand(True)
        amount = rng.randrange(50000, 500000, 5000)
        events.append((t0, victim, u, amount, "tri_in"))
        r = rng.random()
        if r < 0.80:    # типичный случай
            events.append((t0 + timedelta(minutes=rng.randint(5, 1200)), u, d["id"],
                           int(amount * rng.uniform(0.9, 1.0)), "tri_out"))
        elif r < 0.90:  # трудный: деньги дробят на две части
            for part in (0.55, 0.45):
                events.append((t0 + timedelta(minutes=rng.randint(5, 600)), u, d["id"],
                               int(amount * part), "tri_out"))
        else:           # трудный: пересылают через несколько дней
            events.append((t0 + timedelta(hours=rng.randint(80, 95)), u, d["id"], amount, "tri_out"))

    # 3. Честные ситуации, похожие на схему.
    for _ in range(150):  # продавец получил оплату от незнакомца и платит знакомому поставщику
        u = rng.choice(honest); t0 = t_rand(True)
        events.append((t0, stranger_for(u), u, rng.randrange(5000, 200000, 500), "benign_in"))
        events.append((t0 + timedelta(minutes=rng.randint(10, 1400)), u, rng.choice(contacts[u]),
                       rng.randrange(5000, 200000, 500), "benign_out"))
    for _ in range(80):   # получил от незнакомца и платит новому человеку другую сумму
        u = rng.choice(honest); t0 = t_rand(True)
        events.append((t0, stranger_for(u), u, rng.randrange(5000, 200000, 500), "benign_in"))
        events.append((t0 + timedelta(minutes=rng.randint(10, 1400)), u, stranger_for(u),
                       rng.randrange(2000, 200000, 500), "benign_out"))
    for _ in range(40):   # получил от незнакомца и честно вернул ему же
        u = rng.choice(honest); t0 = t_rand(True); s = stranger_for(u)
        a = rng.randrange(5000, 200000, 500)
        events.append((t0, s, u, a, "benign_in"))
        events.append((t0 + timedelta(minutes=rng.randint(10, 600)), u, s, a, "benign_out"))

    events.sort(key=lambda e: e[0])
    return clients, events


def build(path: str | None = None, seed: int = 7, now: datetime | None = None, verbose: bool = True):
    path = path or db.DB_PATH
    if os.path.exists(path):
        os.remove(path)
    now = (now or datetime.now()).replace(microsecond=0)
    rng = random.Random(seed)
    clients, events = generate(rng, now)
    conn = db.connect(path)
    conn.executemany(
        "INSERT INTO clients(id,name,account_opened_at,balance,true_kind) VALUES(?,?,?,?,?)",
        [(c["id"], c["name"], c["opened"].isoformat(), rng.randrange(20000, 900000, 1000), c["kind"])
         for c in clients])

    stats = {"tri": 0, "tri_caught": 0, "honest": 0, "honest_held": 0}
    for t, s, r, amount, scen in events:
        if scen in ("tri_out", "benign_out", "normal") and t >= now - timedelta(days=EVAL_DAYS):
            res = engine.evaluate(conn, s, r, amount, t)
            held = res["decision"] == "hold"
            if scen == "tri_out":
                stats["tri"] += 1; stats["tri_caught"] += held
            else:
                stats["honest"] += 1; stats["honest_held"] += held
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (s, r, amount, t.isoformat(), scen))

    _demo_personas(conn, now)
    conn.commit()
    if verbose:
        print(f"Клиентов: {len(clients)}, переводов: {len(events)}")
        print(f"Поймано переводов дропперам: {stats['tri_caught']} из {stats['tri']} "
              f"({100 * stats['tri_caught'] / stats['tri']:.0f}%)")
        print(f"Честных переводов остановлено зря: {stats['honest_held']} из {stats['honest']} "
              f"({100 * stats['honest_held'] / stats['honest']:.2f}%)")
    return conn, stats


def _demo_personas(conn, now: datetime):
    """Три персонажа для показа на защите."""
    rows = [
        (1001, "Айдана (студентка)", now - timedelta(days=400), 42000, "honest"),
        (1002, "Незнакомый отправитель", now - timedelta(days=900), 300000, "honest"),
        (1003, "Карта «для возврата»", now - timedelta(days=6), 0, "dropper"),
        (1004, "Мама", now - timedelta(days=2000), 150000, "honest"),
    ]
    conn.executemany(
        "INSERT INTO clients(id,name,account_opened_at,balance,true_kind) VALUES(?,?,?,?,?)",
        [(i, n, o.isoformat(), b, k) for i, n, o, b, k in rows])
    tr = [
        (1004, 1001, 20000, now - timedelta(days=9), "demo"),
        (1001, 1004, 5000, now - timedelta(days=3), "demo"),
        (1002, 1001, 150000, now - timedelta(minutes=20), "demo_in"),
    ]
    conn.executemany(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
        [(s, r, a, t.isoformat(), sc) for s, r, a, t, sc in tr])
    conn.execute("UPDATE clients SET balance = balance + 150000 WHERE id=1001")


if __name__ == "__main__":
    build()

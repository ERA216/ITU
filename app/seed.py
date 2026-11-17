"""Генератор синтетических данных и проверка движка.

Запуск:  python -m app.seed
Создаёт data.db, проигрывает все переводы по порядку через движок
и печатает, сколько схем поймано и сколько честных переводов остановлено зря.
"""
import json
import os
import random
from datetime import datetime, timedelta

from . import clock, db, engine

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
                               int(amount * part), "tri_split"))
        else:           # трудный: пересылают через несколько дней
            events.append((t0 + timedelta(hours=rng.randint(80, 95)), u, d["id"], amount, "tri_late"))

    # 3. Честные ситуации, похожие на схему.
    for _ in range(150):  # продавец получил оплату от незнакомца и платит знакомому поставщику
        u = rng.choice(honest); t0 = t_rand(True)
        events.append((t0, stranger_for(u), u, rng.randrange(5000, 200000, 500), "benign_in"))
        events.append((t0 + timedelta(minutes=rng.randint(10, 1400)), u, rng.choice(contacts[u]),
                       rng.randrange(5000, 200000, 500), "benign_supplier"))
    for _ in range(80):   # получил от незнакомца и платит новому человеку другую сумму
        u = rng.choice(honest); t0 = t_rand(True)
        events.append((t0, stranger_for(u), u, rng.randrange(5000, 200000, 500), "benign_in"))
        events.append((t0 + timedelta(minutes=rng.randint(10, 1400)), u, stranger_for(u),
                       rng.randrange(2000, 200000, 500), "benign_new"))
    for _ in range(40):   # получил от незнакомца и честно вернул ему же
        u = rng.choice(honest); t0 = t_rand(True); s = stranger_for(u)
        a = rng.randrange(5000, 200000, 500)
        events.append((t0, s, u, a, "benign_in"))
        events.append((t0 + timedelta(minutes=rng.randint(10, 600)), u, s, a, "benign_return"))

    events.sort(key=lambda e: e[0])
    return clients, events


def populate(conn, seed: int = 7, now: datetime | None = None, evaluate: bool = True, reset: bool = False,
             with_history: bool = False) -> dict:
    """Заполняет базу. evaluate=True дополнительно проигрывает переводы через движок и считает качество.

    with_history=True добавляет историю случаев за 30 дней для панели эффективности (см. app/history.py).
    """
    if reset:
        for table in ("self_reports", "message_checks", "clearances", "guardians", "recruit_marks", "afc_list", "call_signals", "events", "complaints", "incoming_marks", "documents", "cases", "transfers", "clients", "meta"):
            conn.execute(f"DELETE FROM {table}")
    now = (now or clock.now()).replace(microsecond=0)
    rng = random.Random(seed)
    clients, events = generate(rng, now)
    conn.executemany(
        "INSERT INTO clients(id,name,account_opened_at,balance,true_kind) VALUES(?,?,?,?,?)",
        [(c["id"], c["name"], c["opened"].isoformat(), rng.randrange(20000, 900000, 1000), c["kind"])
         for c in clients])

    stats = {"tri": 0, "tri_caught": 0, "honest": 0, "honest_held": 0, "clients": len(clients), "events": len(events),
             "scored": [], "held": [], "passed": []}
    for t, s, r, amount, scen in events:
        outgoing = scen == "normal" or scen.startswith(("tri_out", "tri_split", "tri_late", "benign_s",
                                                         "benign_n", "benign_r"))
        if evaluate and outgoing and t >= now - timedelta(days=EVAL_DAYS):
            res = engine.evaluate(conn, s, r, amount, t)
            held = res["decision"] == "hold"
            scheme = scen.startswith("tri_")
            if scheme:
                stats["tri"] += 1; stats["tri_caught"] += held
            else:
                stats["honest"] += 1; stats["honest_held"] += held
            # Для страницы качества: оценка и могла ли сработать остановка при любом пороге.
            stats["scored"].append((res["score"], res["incoming_id"] is not None, scen))
        cur = conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                           (s, r, amount, t.isoformat(), scen))
        if evaluate and outgoing and t >= now - timedelta(days=EVAL_DAYS):
            # Для истории случаев: что движок остановил и что пропустил.
            (stats["held"] if held else stats["passed"]).append(
                {"id": cur.lastrowid, "t": t, "sender": s, "receiver": r, "amount": amount, "scen": scen, "res": res})

    if with_history:
        from . import history
        history.simulate(conn, stats, now, random.Random(seed + 1))
    _demo_personas(conn, now)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('seeded_at',?)", (now.isoformat(),))
    conn.commit()
    return stats


def build(path: str | None = None, seed: int = 7, now: datetime | None = None, verbose: bool = True):
    """Создаёт базу с нуля и печатает качество движка на этих данных."""
    path = path or db.DB_PATH
    if os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)
    stats = populate(conn, seed, now)
    if verbose:
        caught, held = stats["tri_caught"], stats["honest_held"]
        print(f"Клиентов: {stats['clients']}, переводов: {stats['events']}")
        print(f"Поймано переводов дропперам: {caught} из {stats['tri']} ({100 * caught / stats['tri']:.0f}%)")
        print(f"Честных переводов остановлено зря: {held} из {stats['honest']} "
              f"({100 * held / stats['honest']:.2f}%)")
        print(f"Среди всех остановок настоящих схем: {100 * caught / max(1, caught + held):.0f}%")
    return conn, stats


def _demo_personas(conn, now: datetime):
    """Персонажи для показа на защите."""
    rows = [
        # Последнее число — остаток до демо-переводов; итоговый баланс считается по самим переводам ниже.
        (1001, "Айдана (студентка)", now - timedelta(days=400), 12000, "honest"),
        (1002, "Незнакомый отправитель", now - timedelta(days=900), 450000, "honest"),
        (1003, "Карта «для возврата»", now - timedelta(days=6), 0, "dropper"),
        (1004, "Мама", now - timedelta(days=2000), 180000, "honest"),
        (1005, "Данияр (продаёт на маркетплейсе)", now - timedelta(days=700), 14500, "honest"),
        (1006, "Гульнар (купила телефон по объявлению)", now - timedelta(days=1200), 290000, "honest"),
        (1007, "Руслан (счёту 12 дней)", now - timedelta(days=12), 0, "dropper"),
        (1008, "Инвестплатформа «Профит»", now - timedelta(days=40), 0, "dropper"),
        (1009, "«Безопасный счёт»", now - timedelta(days=3), 0, "dropper"),
        (1010, "Ерлан (нашёл «подработку»)", now - timedelta(days=45), 8000, "honest"),
        (1011, "Оптовый склад (поставщик Данияра)", now - timedelta(days=1500), 320000, "honest"),
        (1012, "Тимур (студент, отдал карту «работодателю»)", now - timedelta(days=210), 6000, "honest"),
    ]
    conn.executemany(
        "INSERT INTO clients(id,name,account_opened_at,balance,true_kind) VALUES(?,?,?,?,?)",
        [(i, n, o.isoformat(), b, k) for i, n, o, b, k in rows])
    tr = [
        (1004, 1001, 15000, now - timedelta(days=200), "demo"),
        (1004, 1001, 20000, now - timedelta(days=9), "demo"),
        (1001, 1004, 5000, now - timedelta(days=3), "demo"),
        (1002, 1001, 150000, now - timedelta(minutes=20), "demo_in"),
    ]
    conn.executemany(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
        [(s, r, a, t.isoformat(), sc) for s, r, a, t, sc in tr])

    # У Данияра обычная история продавца: оплаты от покупателей и расчёты с поставщиком.
    # Поставщик — знакомый получатель: ему Данияр сможет платить и при соразмерной блокировке.
    for n, (other, amount, incoming) in enumerate([(21, 18000, True), (35, 42000, True), (1011, 30000, False),
                                                   (52, 9500, True), (1011, 25000, False), (64, 27000, True)]):
        pair = (other, 1005) if incoming else (1005, other)
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (*pair, amount, (now - timedelta(days=60 - 8 * n)).isoformat(), "demo"))

    # «Треугольник»: Гульнар оплатила телефон по объявлению на карту Данияра, а мошенник
    # попросил Данияра «вернуть ошибочный перевод» на другую карту. Данияр переслал деньги.
    # Блокировка появится, когда Гульнар пожалуется на перевод: так связаны обе половины продукта.
    t_in = now - timedelta(days=2, hours=3)
    conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                 (1006, 1005, 200000, t_in.isoformat(), "demo_in"))
    conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                 (1005, 1003, 195000, (t_in + timedelta(minutes=40)).isoformat(), "demo_out"))

    # Руслан за пять дней четыре раза переслал деньги от разных незнакомцев: для контраста в панели аналитика.
    last = None
    for n, (victim, amount) in enumerate([(11, 120000), (27, 300000), (43, 85000), (58, 210000)]):
        t_in = now - timedelta(days=5 - n, hours=2)
        t_out = t_in + timedelta(minutes=12)
        cur = conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                           (victim, 1007, amount, t_in.isoformat(), "demo_in"))
        inc_id = cur.lastrowid
        res = engine.evaluate(conn, 1007, 1003, int(amount * 0.95), t_out)
        cur = conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                           (1007, 1003, int(amount * 0.95), t_out.isoformat(), "demo_out"))
        last = (cur.lastrowid, inc_id, res, t_out)
    conn.execute(
        """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (1007, last[0], last[1], "block", last[2]["score"],
         json.dumps(last[2]["reasons"], ensure_ascii=False), (last[3] + timedelta(hours=1)).isoformat()))
    conn.execute("UPDATE clients SET status='blocked' WHERE id=1007")
    # Мама давно знает дочь и назначила её доверенным лицом: об остановленных переводах мамы узнает Айдана.
    conn.execute("INSERT INTO guardians(client_id,guardian_id,added_at) VALUES(1004,1001,?)",
                 ((now - timedelta(days=60)).isoformat(),))

    # Ерлану в чате предложили «подработку»: принимать переводы и пересылать их. За два дня пришли деньги
    # от трёх незнакомых людей, дальше он пока ничего не отправлял.
    for n, (sender, amount) in enumerate([(61, 45000), (66, 60000), (69, 38000)]):
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (sender, 1010, amount, (now - timedelta(hours=40 - 15 * n)).isoformat(), "demo_in"))

    # Тимур откликнулся на «подработку для студентов» и отдал карту. Обычная студенческая история,
    # а за последние сутки на карту пришли деньги от двух незнакомых людей.
    for n, (pair, amount) in enumerate([((17, 1012), 25000), ((1012, 17), 4000), ((17, 1012), 30000),
                                        ((1012, 23), 6500), ((17, 1012), 25000), ((1012, 23), 3000)]):
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (*pair, amount, (now - timedelta(days=150 - 25 * n)).isoformat(), "demo"))
    for n, (sender, amount) in enumerate([(72, 40000), (77, 55000)]):
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (sender, 1012, amount, (now - timedelta(hours=20 - 9 * n)).isoformat(), "demo_in"))

    # Имитация базы Антифрод-центра: «чёрный» список — подтверждённые мошенники, «серый» — подозрительные.
    conn.executemany("INSERT INTO afc_list(client_id,list,added_at,note) VALUES(?,?,?,?)", [
        (1008, "black", (now - timedelta(days=20)).isoformat(), "Лжеинвестиционная платформа, подтверждено"),
        (1003, "grey", (now - timedelta(days=4)).isoformat(), "Сообщения банков о переводах от незнакомцев"),
    ])

    # Балансы персонажей сходятся с их переводами: на экране видно, где на самом деле лежат деньги.
    conn.execute("""UPDATE clients SET balance = balance
                      + COALESCE((SELECT SUM(amount) FROM transfers WHERE receiver_id=clients.id AND status='done'), 0)
                      - COALESCE((SELECT SUM(amount) FROM transfers WHERE sender_id=clients.id AND status='done'), 0)
                    WHERE id >= 1001""")
    case_id = conn.execute("SELECT id FROM cases WHERE client_id=1007").fetchone()["id"]
    conn.execute("INSERT INTO events(at,actor,action,case_id,transfer_id,details) VALUES(?,?,?,?,?,?)",
                 ((last[3] + timedelta(hours=1)).isoformat(), "system", "blocked", case_id, last[0],
                  json.dumps({"text": "Антифрод-система приостановила операции: повторяющиеся пересылки"},
                             ensure_ascii=False)))


if __name__ == "__main__":
    build()

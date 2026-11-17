"""SQLite: схема и подключение."""
import os
import sqlite3

DB_PATH = os.environ.get("CA_DB", os.path.join(os.path.dirname(__file__), "..", "data.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    account_opened_at TEXT NOT NULL,
    balance INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',      -- active | limited (заморожена спорная сумма) | blocked
    true_kind TEXT NOT NULL DEFAULT 'honest'    -- honest | dropper (метка синтетики, банку неизвестна)
);
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    sender_id INTEGER NOT NULL REFERENCES clients(id),
    receiver_id INTEGER NOT NULL REFERENCES clients(id),
    amount INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done',        -- done | held | pending (период охлаждения) | cancelled
    return_of INTEGER REFERENCES transfers(id), -- если это безопасный возврат
    scenario TEXT,                               -- метка синтетики
    release_at TEXT                              -- когда уйдёт перевод в периоде охлаждения
);
CREATE INDEX IF NOT EXISTS ix_tr_sender ON transfers(sender_id, created_at);
CREATE INDEX IF NOT EXISTS ix_tr_receiver ON transfers(receiver_id, created_at);
CREATE INDEX IF NOT EXISTS ix_tr_pending ON transfers(release_at) WHERE status='pending';
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    transfer_id INTEGER REFERENCES transfers(id),          -- остановленный перевод
    incoming_transfer_id INTEGER REFERENCES transfers(id), -- "ошибочный" входящий
    kind TEXT NOT NULL,                 -- intercept | block
    score INTEGER NOT NULL,
    reasons TEXT NOT NULL,              -- JSON-список сработавших признаков
    status TEXT NOT NULL DEFAULT 'open',-- open | returned | under_review | unblocked | kept | released | cancelled
    explanation TEXT,                   -- объяснение клиента при апелляции
    decision_comment TEXT,              -- комментарий аналитика к решению
    decided_at TEXT,
    complaint_id INTEGER,               -- жалоба, по которой возник случай (для всей цепочки)
    hop INTEGER,                        -- звено цепочки: 1 — получил деньги от пострадавшего
    submitted_at TEXT,                  -- когда клиент отправил апелляцию
    due_at TEXT,                        -- до какого момента банк обещал ответить
    guardian_id INTEGER REFERENCES clients(id),  -- доверенное лицо, которому сообщили об остановке
    guardian_state TEXT,                -- notified | asked (клиент настаивает) | confirmed | cancelled
    scope TEXT NOT NULL DEFAULT 'full', -- блокировка: full — счёт целиком | partial — заморожена только спорная сумма
    frozen INTEGER NOT NULL DEFAULT 0,  -- сколько денег из цепочки осталось на счёте и заморожено по этому случаю
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY,
    transfer_id INTEGER NOT NULL UNIQUE REFERENCES transfers(id),  -- перевод, на который пожаловались
    complainant_id INTEGER NOT NULL REFERENCES clients(id),
    outcome TEXT NOT NULL,              -- blocked | warned | already_returned
    case_id INTEGER REFERENCES cases(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incoming_marks (
    transfer_id INTEGER PRIMARY KEY REFERENCES transfers(id),   -- клиент сказал, что знает отправителя
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,                -- client:<id> | analyst | system
    action TEXT NOT NULL,
    case_id INTEGER REFERENCES cases(id),
    transfer_id INTEGER REFERENCES transfers(id),
    details TEXT                        -- JSON
);
CREATE INDEX IF NOT EXISTS ix_events_case ON events(case_id, id);
CREATE TABLE IF NOT EXISTS afc_list (
    client_id INTEGER PRIMARY KEY REFERENCES clients(id),  -- счёт в базе Антифрод-центра (в демо — имитация)
    list TEXT NOT NULL,                 -- grey: подозрительный | black: подтверждённый мошенник
    added_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS call_signals (
    client_id INTEGER PRIMARY KEY REFERENCES clients(id),  -- сигнал оператора: клиенту звонят с подозрительного номера
    number TEXT NOT NULL,
    started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guardians (
    client_id INTEGER PRIMARY KEY REFERENCES clients(id),  -- кого защищают
    guardian_id INTEGER NOT NULL REFERENCES clients(id),   -- доверенное лицо
    added_at TEXT NOT NULL,
    remove_at TEXT                      -- отключение вступает в силу не сразу: мошенник не уговорит снять защиту
);
CREATE TABLE IF NOT EXISTS clearances (
    id INTEGER PRIMARY KEY,             -- справка о снятии ограничений
    case_id INTEGER NOT NULL REFERENCES cases(id),
    client_id INTEGER NOT NULL REFERENCES clients(id),
    code TEXT NOT NULL UNIQUE,          -- код проверки подлинности
    issued_at TEXT NOT NULL,
    revoked_at TEXT                     -- если счёт снова приостановили
);
CREATE TABLE IF NOT EXISTS recruit_marks (
    client_id INTEGER PRIMARY KEY REFERENCES clients(id),  -- клиент ответил на предупреждение о вербовке
    action TEXT NOT NULL,               -- dismissed: «это мои покупатели или знакомые»
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_checks (
    id INTEGER PRIMARY KEY,             -- проверка подозрительного сообщения; сам текст не хранится
    client_id INTEGER NOT NULL REFERENCES clients(id),
    scheme TEXT,                        -- mistaken | safe_account | investment | job | NULL
    verdict TEXT NOT NULL,              -- danger | caution | none
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS self_reports (
    id INTEGER PRIMARY KEY,             -- клиент сам сообщил банку, что его втянули в схему
    client_id INTEGER NOT NULL REFERENCES clients(id),
    kind TEXT NOT NULL,                 -- card: отдал карту или доступ | transit: принимал и пересылал | offer: только предложили
    details TEXT,                       -- что клиент рассказал своими словами
    case_id INTEGER REFERENCES cases(id),
    code TEXT NOT NULL UNIQUE,          -- код проверки подтверждения
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    path TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    media_type TEXT,
    uploaded_at TEXT NOT NULL
);
"""


SCHEMA_VERSION = 9


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        # Демо-база от старой версии кода: проще пересоздать, данные заполнятся заново.
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            conn.execute(f'DROP TABLE IF EXISTS "{t}"')
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return conn

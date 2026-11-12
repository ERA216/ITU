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
    status TEXT NOT NULL DEFAULT 'active',      -- active | blocked
    true_kind TEXT NOT NULL DEFAULT 'honest'    -- honest | dropper (метка синтетики, банку неизвестна)
);
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    sender_id INTEGER NOT NULL REFERENCES clients(id),
    receiver_id INTEGER NOT NULL REFERENCES clients(id),
    amount INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done',        -- done | held | cancelled
    return_of INTEGER REFERENCES transfers(id), -- если это безопасный возврат
    scenario TEXT                                -- метка синтетики
);
CREATE INDEX IF NOT EXISTS ix_tr_sender ON transfers(sender_id, created_at);
CREATE INDEX IF NOT EXISTS ix_tr_receiver ON transfers(receiver_id, created_at);
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
    created_at TEXT NOT NULL
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


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

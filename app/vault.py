"""Шифрование документов клиентов на диске.

Переписка и договоры — персональные данные, поэтому файлы хранятся зашифрованными (Fernet: AES-128 и
проверка целостности HMAC-SHA256). Без ключа содержимое на диске прочитать нельзя, а подменённый файл
не расшифруется.

Ключ берётся из переменной CA_DOC_KEY. Если её нет, ключ создаётся один раз и хранится в файле
рядом с базой (для демо). В реальной системе ключ живёт в хранилище секретов банка, а не на диске.
"""
import os

from cryptography.fernet import Fernet, InvalidToken  # noqa: F401  (InvalidToken нужен main.py)

KEY_FILE = os.environ.get("CA_DOC_KEY_FILE", os.path.join(os.path.dirname(__file__), "..", "doc.key"))


def _key() -> bytes:
    env = os.environ.get("CA_DOC_KEY")
    if env:
        return env.encode()
    if not os.path.exists(KEY_FILE):
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    with open(KEY_FILE, "rb") as f:
        return f.read().strip()


def encrypt(data: bytes) -> bytes:
    return Fernet(_key()).encrypt(data)


def decrypt(token: bytes) -> bytes:
    return Fernet(_key()).decrypt(token)

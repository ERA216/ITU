# Образ для хостинга (Render, Railway или любой сервер с Docker).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Данные демо, документы и ключ шифрования живут в /data: подключите туда постоянный диск,
# иначе после перезапуска всё создастся заново (для демо это тоже нормально).
# CA_PUBLIC=1: без заданного CA_ANALYST_PIN сервер придумает случайный код и напишет его в журнал запуска.
# CA_HTTPS=1: куки входа только по https (у Render и Railway https есть всегда).
ENV CA_DB=/data/data.db CA_UPLOADS=/data/uploads CA_DOC_KEY_FILE=/data/doc.key PORT=8000 CA_PUBLIC=1 CA_HTTPS=1
RUN mkdir -p /data && useradd --create-home app && chown app /data
USER app
EXPOSE 8000
# Код аналитика задайте переменной CA_ANALYST_PIN в настройках хостинга.
# --proxy-headers: адрес посетителя берётся из заголовка прокси хостинга (нужен для лимита попыток входа).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]

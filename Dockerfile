FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/temp /app/data \
    && chown -R appuser:appuser /app

COPY --chown=appuser:appuser . .

USER appuser

CMD ["python", "main.py"]

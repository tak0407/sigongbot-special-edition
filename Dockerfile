FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl dbus-daemon gnome-keyring libsecret-tools \
    && curl -fsSL https://antigravity.google/cli/install.sh \
        | bash -s -- --dir /usr/local/bin \
    && agy --version \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/temp /app/data /home/appuser/.gemini \
        /home/appuser/.local/share/keyrings \
    && chown -R appuser:appuser /app /home/appuser/.gemini /home/appuser/.local

COPY --chown=appuser:appuser . .
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENV DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/agy-session-bus

USER appuser

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "main.py"]

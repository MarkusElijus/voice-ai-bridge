FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.12-slim
RUN useradd -m -u 1000 app && mkdir -p /app && chown app:app /app
WORKDIR /app
COPY --from=builder /root/.local /home/app/.local
COPY --chown=app:app . .
USER app
ENV PATH=/home/app/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
# --timeout-graceful-shutdown=300: on SIGINT/SIGTERM, uvicorn waits up to 5 min
# for in-flight WebSocket connections (live calls) to finish before exiting.
# Pairs with fly.toml kill_timeout=5m so Fly doesn't SIGKILL the VM during
# graceful drain. Without this combo, a rolling deploy mid-call drops the
# active WS and the caller hears silence (diagnosed from call bSl5HIQl4w0
# 2026-05-12, where boss got hung up on by an unrelated chat-card deploy).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*", \
     "--timeout-graceful-shutdown", "300"]

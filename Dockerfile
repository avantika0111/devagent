# ── Stage 1: builder ────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies only in this stage
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: scanning tools ──────────────────────────────────────────────────
FROM python:3.11-slim AS tools

RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm curl \
    && rm -rf /var/lib/apt/lists/*

# Install security scanning tools
RUN pip install --no-cache-dir semgrep pip-audit bandit


# ── Stage 3: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user — never run as root in production
RUN groupadd --gid 1001 devagent \
 && useradd  --uid 1001 --gid devagent --shell /bin/bash --create-home devagent

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy scanning binaries from tools stage
COPY --from=tools /usr/local/bin/semgrep   /usr/local/bin/semgrep
COPY --from=tools /usr/local/bin/pip-audit /usr/local/bin/pip-audit
COPY --from=tools /usr/local/bin/bandit    /usr/local/bin/bandit

# Copy application source
COPY --chown=devagent:devagent . .

# Switch to non-root user
USER devagent

# Expose webhook port
EXPOSE 8080

# Health check — Docker and orchestrators use this
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

# Start the webhook server
CMD ["uvicorn", "platform.webhook:app", "--host", "0.0.0.0", "--port", "8080"]

# ============================================================================
# careeragent-code - container image (the read-only code workspace)
# ============================================================================
# Needs two system binaries the fetch box doesn't: `git` (shallow clone/fetch)
# and `ripgrep` (search). Everything cloned is DATA — this process never runs
# anything FROM a repo (no hooks, no build), and runs unprivileged (uid 1000).
# ============================================================================
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

# Drop root — untrusted repos in, unprivileged process. Own /cache (the volume
# mount inherits this ownership on first, empty mount) so appuser can clone.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && mkdir -p /cache \
    && chown -R appuser:appuser /app /cache
USER appuser

EXPOSE 8012

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8012", "--app-dir", "/app/src"]

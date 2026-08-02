# ============================================================================
# careeragent-fetch - container image
# ============================================================================
# A pure-Python egress + extraction box (FastAPI + httpx + trafilatura/bs4 +
# pdfplumber + python-docx). Every dependency ships manylinux wheels, so the
# slim base needs NO apt layer. Runs unprivileged (uid 1000) — this is the
# service that touches untrusted URLs and untrusted uploads, so it drops root.
# ============================================================================
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

# Drop root — untrusted input in, unprivileged process.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8008

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8008", "--app-dir", "/app/src"]

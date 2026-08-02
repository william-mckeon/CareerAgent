# ============================================================================
# careeragent-ats - container image
# ============================================================================
# A pure-Python, deterministic keyword-coverage scorer (FastAPI + rapidfuzz).
# NO model, NO database, NO network egress, NO file parsing. Every dependency
# ships manylinux wheels (rapidfuzz included), so the slim base needs NO apt
# layer. Runs unprivileged (uid 1000).
# ============================================================================
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0
# PYTHONHASHSEED=0 pins set/dict hash randomization so keyword extraction is
# byte-for-byte reproducible across container restarts (defense-in-depth; the
# real determinism fix is the alphabetical phrase tiebreak in ats.py).

# Drop root — least privilege for a box that touches user text.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8010

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8010", "--app-dir", "/app/src"]

# ============================================================================
# careeragent-render - container image
# ============================================================================
# A pure-Python résumé renderer (FastAPI + reportlab + python-docx). NO model,
# NO database, NO network egress. Every dependency ships self-contained
# manylinux wheels — reportlab bundles its own Base-14 fonts, python-docx rides
# lxml's prebuilt wheels — so the slim base needs NO apt layer and no build
# toolchain (confirmed: a clean `pip install` resolves wheels only and both
# `import reportlab, docx` succeed). Runs unprivileged (uid 1000).
# ============================================================================
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

# Drop root — least privilege for a box that renders user text.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8009

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8009", "--app-dir", "/app/src"]

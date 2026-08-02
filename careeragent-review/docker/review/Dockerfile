# ============================================================================
# careeragent-review - container image
# ============================================================================
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

EXPOSE 8007

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8007", "--app-dir", "/app/src"]

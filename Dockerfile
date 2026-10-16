FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ESSAY_DATA=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data && chown appuser:appuser /data
COPY --chown=appuser:appuser essay_web ./essay_web
USER appuser
EXPOSE 8000
CMD ["uvicorn", "essay_web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

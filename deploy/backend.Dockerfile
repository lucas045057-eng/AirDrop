FROM python:3.12-slim
WORKDIR /app
COPY requirements-lock.txt .
ARG PYPI_INDEX=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "$PYPI_INDEX" -r requirements-lock.txt && useradd --uid 10001 --create-home app
COPY . .
RUN mkdir -p /app/data /bus && chown -R app:app /app /bus
USER app
CMD ["sh","-c","alembic upgrade head && uvicorn backend.main:app --host 0.0.0.0 --port 8000"]

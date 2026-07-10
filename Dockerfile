FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application.
COPY src/ ./src/
COPY dashboard/ ./dashboard/
COPY api/ ./api/
COPY data/generate_fake_data.py ./data/generate_fake_data.py
COPY docker/entrypoint.sh ./docker/entrypoint.sh
RUN chmod +x ./docker/entrypoint.sh

ENV PYTHONPATH=/app/src \
    DATA_DIR=/app/data/output \
    TELEMETRY_JSONL=/app/data/output/telemetry_logs.jsonl \
    EMPLOYEES_CSV=/app/data/output/employees.csv \
    DUCKDB_PATH=/app/data/analytics.duckdb \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0

EXPOSE 8501

# Entrypoint generates data (if missing), runs the ETL, then serves the dashboard.
ENTRYPOINT ["./docker/entrypoint.sh"]

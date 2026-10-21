.PHONY: help install data etl pipeline dashboard api test clean docker

PY ?= python3
GEN_NUM_USERS ?= 100
GEN_NUM_SESSIONS ?= 5000
GEN_DAYS ?= 60
GEN_SEED ?= 42
GEN_PROVIDERS ?= claude_code,openai,gemini,deepseek

help:
	@echo "Targets:"
	@echo "  install    Install Python dependencies (requirements-dev.txt, pinned by constraints.txt)"
	@echo "  data       Generate synthetic usage data for all providers (GEN_PROVIDERS)"
	@echo "  etl        Build the DuckDB analytics database from the dataset"
	@echo "  pipeline   data + etl (full refresh)"
	@echo "  dashboard  Launch the Streamlit dashboard"
	@echo "  api        Launch the FastAPI server (docs at :8000/docs)"
	@echo "  test       Run the test suite"
	@echo "  clean      Remove generated data and database"
	@echo "  docker     Build and start the whole stack (docker compose up --build)"

install:
	$(PY) -m pip install -r requirements-dev.txt -c constraints.txt

data:
	$(PY) data/generate_fake_data.py \
		--num-users $(GEN_NUM_USERS) \
		--num-sessions $(GEN_NUM_SESSIONS) \
		--days $(GEN_DAYS) \
		--seed $(GEN_SEED) \
		--providers "$(GEN_PROVIDERS)" \
		--output-dir data/output

etl:
	PYTHONPATH=src $(PY) -m analytics.etl

pipeline: data etl

dashboard:
	PYTHONPATH=src streamlit run dashboard/app.py

api:
	PYTHONPATH=src $(PY) -m uvicorn api.main:app --host 0.0.0.0 --port 8000

test:
	PYTHONPATH=src $(PY) -m pytest -q

clean:
	rm -rf data/output data/analytics.duckdb data/analytics.duckdb.wal

docker:
	docker compose up --build

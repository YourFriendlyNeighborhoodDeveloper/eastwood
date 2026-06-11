#!/usr/bin/env bash
set -euo pipefail

write_file() {
  local path="$1"
  shift
  local dir
  dir="$(dirname "$path")"
  mkdir -p "$dir"
  if [[ -f "$path" ]]; then
    echo "skip  $path"
    return 0
  fi
  cat > "$path" <<'EOT'
'"$@"'
EOT
  echo "write $path"
}

mkdir -p \
  config/schemas \
  scripts \
  migrations/postgres \
  src/core \
  src/contracts \
  src/ingestion/webhooks \
  src/ingestion/vendors \
  src/normalize \
  src/validate/tcpadnc \
  src/enrich \
  src/scoring \
  src/suppression \
  src/routing \
  src/pipeline \
  src/storage/repository \
  src/api/routes \
  src/dashboard/views \
  src/observability \
  grafana/provisioning/dashboards \
  grafana/provisioning/datasources \
  tests/unit \
  tests/integration

write_file .gitignore '
__pycache__/
*.pyc
*.pyo
*.pyd
.venv/
.env
.DS_Store
.pytest_cache/
.mypy_cache/
dist/
build/
coverage.xml
'

write_file .env.example '
EASTWOOD_ENV=dev
EASTWOOD_CONFIG=config/config.yaml

POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=eastwood
POSTGRES_USER=eastwood
POSTGRES_PASSWORD=eastwood

REDIS_HOST=redis
REDIS_PORT=6379

KAFKA_BOOTSTRAP_SERVERS=kafka:9092

NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password
'

write_file README.md '
# Eastwood

Production-grade insurance lead intake + consent ledger + routing.

## Local
cp .env.example .env
docker compose up --build

API: http://localhost:8001
Metrics: http://localhost:8000/metrics
Grafana: http://localhost:3000
Prometheus: http://localhost:9090
'

write_file requirements.txt '
fastapi==0.115.6
uvicorn[standard]==0.30.6
pydantic==2.10.3
pydantic-settings==2.6.1
python-dotenv==1.0.1
pyyaml==6.0.2
kafka-python==2.0.2
redis==5.0.8
psycopg[binary,pool]==3.2.3
neo4j==5.24.0
prometheus_client==0.21.0
phonenumbers==8.13.49
email-validator==2.2.0
dash==2.18.2
dash-bootstrap-components==1.6.0
plotly==5.24.1
pandas==2.2.3
httpx==0.27.2
'

write_file Dockerfile '
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
  ca-certificates curl build-essential \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "src"]
'

write_file docker-compose.yml '
services:
  zookeeper:
    image: bitnami/zookeeper:3.9
    restart: unless-stopped
    environment:
      ALLOW_ANONYMOUS_LOGIN: "yes"
    ports:
      - "2181:2181"

  kafka:
    image: bitnami/kafka:3.7
    restart: unless-stopped
    depends_on:
      zookeeper:
        condition: service_started
    environment:
      KAFKA_CFG_ZOOKEEPER_CONNECT: zookeeper:2181
      KAFKA_CFG_LISTENERS: PLAINTEXT://:9092
      KAFKA_CFG_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092
      KAFKA_CFG_LISTENER_SECURITY_PROTOCOL_MAP: "PLAINTEXT:PLAINTEXT"
      ALLOW_PLAINTEXT_LISTENER: "yes"
      KAFKA_CFG_AUTO_CREATE_TOPICS_ENABLE: "true"
      KAFKA_CFG_OFFSETS_TOPIC_REPLICATION_FACTOR: "1"
      KAFKA_CFG_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: "1"
      KAFKA_CFG_TRANSACTION_STATE_LOG_MIN_ISR: "1"
    ports:
      - "9092:9092"
    healthcheck:
      test: ["CMD-SHELL", "/opt/bitnami/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092 >/dev/null 2>&1 || exit 1"]
      interval: 5s
      timeout: 5s
      retries: 30
      start_period: 15s

  kafka-init:
    image: bitnami/kafka:3.7
    depends_on:
      kafka:
        condition: service_healthy
    entrypoint: >
      bash -lc "
      /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic leads.raw --partitions 3 --replication-factor 1 &&
      /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic leads.normalized --partitions 3 --replication-factor 1 &&
      /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic leads.validated --partitions 3 --replication-factor 1 &&
      /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic leads.enriched --partitions 3 --replication-factor 1 &&
      /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic leads.scored --partitions 3 --replication-factor 1 &&
      /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic leads.routed --partitions 3 --replication-factor 1 &&
      /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic consent.events --partitions 3 --replication-factor 1 &&
      /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic touches.events --partitions 3 --replication-factor 1 &&
      /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic suppression.events --partitions 3 --replication-factor 1 &&
      /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic dlq.events --partitions 3 --replication-factor 1 &&
      echo 'Kafka topics ready.'"
    restart: "no"

  postgres:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_DB: eastwood
      POSTGRES_USER: eastwood
      POSTGRES_PASSWORD: eastwood
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7
    restart: unless-stopped
    ports:
      - "6379:6379"

  neo4j:
    image: neo4j:5
    restart: unless-stopped
    environment:
      NEO4J_AUTH: neo4j/password
    ports:
      - "7474:7474"
      - "7687:7687"

  prometheus:
    image: prom/prometheus
    restart: unless-stopped
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro

  grafana:
    image: grafana/grafana
    restart: unless-stopped
    depends_on:
      - prometheus
    ports:
      - "3000:3000"
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning:ro

  app:
    build: .
    working_dir: /app
    command: ["python", "-m", "src"]
    restart: unless-stopped
    volumes:
      - .:/app
    depends_on:
      kafka:
        condition: service_healthy
      kafka-init:
        condition: service_completed_successfully
      postgres:
        condition: service_started
      redis:
        condition: service_started
      prometheus:
        condition: service_started
    ports:
      - "8001:8001"
      - "8000:8000"
    environment:
      EASTWOOD_CONFIG: config/config.yaml
      POSTGRES_HOST: postgres
      POSTGRES_PORT: 5432
      POSTGRES_DB: eastwood
      POSTGRES_USER: eastwood
      POSTGRES_PASSWORD: eastwood
      REDIS_HOST: redis
      REDIS_PORT: 6379
      KAFKA_BOOTSTRAP_SERVERS: kafka:9092
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_USER: neo4j
      NEO4J_PASSWORD: password

volumes:
  pgdata:
'

write_file prometheus.yml '
global:
  scrape_interval: 15s
scrape_configs:
  - job_name: "eastwood"
    static_configs:
      - targets: ["app:8000"]
'

write_file Makefile '
up:
	docker compose up --build

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=200

fmt:
	python -m compileall src

test:
	python -m pytest -q
'

write_file config/config.yaml '
app:
  env: dev

api:
  host: 0.0.0.0
  port: 8001

metrics:
  host: 0.0.0.0
  port: 8000

kafka:
  bootstrap_servers: kafka:9092

topics:
  leads_raw: leads.raw
  leads_normalized: leads.normalized
  leads_validated: leads.validated
  leads_enriched: leads.enriched
  leads_scored: leads.scored
  leads_routed: leads.routed
  consent_events: consent.events
  touches_events: touches.events
  suppression_events: suppression.events
  dlq_events: dlq.events

redis:
  host: redis
  port: 6379
  db: 0

postgres:
  host: postgres
  port: 5432
  db: eastwood
  user: eastwood
  password: eastwood

neo4j:
  uri: bolt://neo4j:7687
  user: neo4j
  password: password

policy:
  recency_days: 30
  required_products: [final_expense, iul, annuity]
'

write_file config/logging.yaml '
version: 1
formatters:
  std:
    format: "%(asctime)s %(levelname)s %(name)s %(message)s"
handlers:
  console:
    class: logging.StreamHandler
    formatter: std
root:
  level: INFO
  handlers: [console]
'

write_file config/schemas/lead_event.json '
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "LeadEvent",
  "type": "object",
  "required": ["event_id", "ts", "source", "payload"],
  "properties": {
    "event_id": {"type": "string"},
    "ts": {"type": "string"},
    "source": {"type": "string"},
    "payload": {"type": "object"}
  }
}
'

write_file config/schemas/consent_event.json '
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ConsentEvent",
  "type": "object",
  "required": ["event_id", "ts", "lead_id", "evidence"],
  "properties": {
    "event_id": {"type": "string"},
    "ts": {"type": "string"},
    "lead_id": {"type": "string"},
    "evidence": {"type": "object"}
  }
}
'

write_file config/schemas/touch_event.json '
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "TouchEvent",
  "type": "object",
  "required": ["event_id", "ts", "lead_id", "channel", "result"],
  "properties": {
    "event_id": {"type": "string"},
    "ts": {"type": "string"},
    "lead_id": {"type": "string"},
    "channel": {"type": "string"},
    "result": {"type": "string"},
    "meta": {"type": "object"}
  }
}
'

write_file config/schemas/suppression_event.json '
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "SuppressionEvent",
  "type": "object",
  "required": ["event_id", "ts", "kind", "value"],
  "properties": {
    "event_id": {"type": "string"},
    "ts": {"type": "string"},
    "kind": {"type": "string"},
    "value": {"type": "string"},
    "meta": {"type": "object"}
  }
}
'

write_file migrations/postgres/0001_init.sql '
CREATE TABLE IF NOT EXISTS leads (
  lead_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL,
  source TEXT NOT NULL,
  product TEXT NOT NULL,
  phone_e164 TEXT NOT NULL,
  email TEXT,
  state TEXT,
  zip TEXT,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS consent_ledger (
  consent_id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
  ts TIMESTAMPTZ NOT NULL,
  evidence JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS touches (
  touch_id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
  ts TIMESTAMPTZ NOT NULL,
  channel TEXT NOT NULL,
  result TEXT NOT NULL,
  meta JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS suppressions (
  suppression_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL,
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  meta JSONB NOT NULL
);
'

write_file migrations/postgres/0002_indexes.sql '
CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone_e164);
CREATE INDEX IF NOT EXISTS idx_leads_product ON leads(product);
CREATE INDEX IF NOT EXISTS idx_consent_lead ON consent_ledger(lead_id);
CREATE INDEX IF NOT EXISTS idx_touches_lead ON touches(lead_id);
CREATE INDEX IF NOT EXISTS idx_suppressions_kind_value ON suppressions(kind, value);
'

write_file scripts/kafka_init.sh '
#!/usr/bin/env bash
set -euo pipefail
echo ok
'

write_file scripts/db_migrate.sh '
#!/usr/bin/env bash
set -euo pipefail
psql "postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@$POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB" -f migrations/postgres/0001_init.sql
psql "postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@$POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB" -f migrations/postgres/0002_indexes.sql
'

write_file scripts/seed_dev_data.py '
from datetime import datetime, timezone
import json
import os
import uuid
import psycopg

def main():
  host = os.getenv("POSTGRES_HOST", "localhost")
  port = int(os.getenv("POSTGRES_PORT", "5432"))
  db = os.getenv("POSTGRES_DB", "eastwood")
  user = os.getenv("POSTGRES_USER", "eastwood")
  pwd = os.getenv("POSTGRES_PASSWORD", "eastwood")
  dsn = f"postgresql://{user}:{pwd}@{host}:{port}/{db}"
  now = datetime.now(timezone.utc)
  lead_id = uuid.uuid4().hex
  payload = {"first_name": "Test", "last_name": "Lead", "intent": "iul", "ts": now.isoformat()}
  with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
      cur.execute(
        "INSERT INTO leads(lead_id, created_at, source, product, phone_e164, email, state, zip, payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (lead_id, now, "seed", "iul", "+15555550123", "test@example.com", "MI", "48734", json.dumps(payload)),
      )
      conn.commit()
  print(lead_id)

if __name__ == "__main__":
  main()
'

write_file src/__init__.py '
'

write_file src/__main__.py '
from .pipeline.orchestrator import run

if __name__ == "__main__":
  run()
'

write_file src/core/settings.py '
import os
import yaml
from dataclasses import dataclass
from typing import Any, Dict

@dataclass(frozen=True)
class Settings:
  cfg: Dict[str, Any]

  @property
  def api_host(self) -> str:
    return self.cfg["api"]["host"]

  @property
  def api_port(self) -> int:
    return int(self.cfg["api"]["port"])

  @property
  def metrics_port(self) -> int:
    return int(self.cfg["metrics"]["port"])

def load_settings() -> Settings:
  path = os.getenv("EASTWOOD_CONFIG", "config/config.yaml")
  with open(path, "r") as f:
    cfg = yaml.safe_load(f) or {}
  return Settings(cfg=cfg)
'

write_file src/core/ids.py '
import uuid

def new_id() -> str:
  return uuid.uuid4().hex
'

write_file src/observability/metrics.py '
from prometheus_client import Counter, Histogram

events_in = Counter("eastwood_events_in_total", "Inbound events", ["topic"])
events_out = Counter("eastwood_events_out_total", "Outbound events", ["topic"])
dlq_total = Counter("eastwood_dlq_total", "DLQ events", ["reason"])
stage_time = Histogram("eastwood_stage_duration_seconds", "Stage duration", ["stage"])
'

write_file src/api/app.py '
from fastapi import FastAPI
from .routes.health import router as health_router
from .routes.leads import router as leads_router

def create_app() -> FastAPI:
  app = FastAPI(title="Eastwood", version="0.1.0")
  app.include_router(health_router)
  app.include_router(leads_router)
  return app
'

write_file src/api/routes/health.py '
from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
def health():
  return {"ok": True}
'

write_file src/api/routes/leads.py '
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any, Dict

router = APIRouter(prefix="/leads")

class LeadIn(BaseModel):
  source: str
  payload: Dict[str, Any]

@router.post("/ingest")
def ingest(lead: LeadIn):
  return {"accepted": True, "source": lead.source}
'

write_file src/pipeline/topics.py '
from dataclasses import dataclass
from typing import Dict, Any

@dataclass(frozen=True)
class Topics:
  leads_raw: str
  leads_normalized: str
  leads_validated: str
  leads_enriched: str
  leads_scored: str
  leads_routed: str
  consent_events: str
  touches_events: str
  suppression_events: str
  dlq_events: str

  @staticmethod
  def from_cfg(cfg: Dict[str, Any]) -> "Topics":
    t = cfg["topics"]
    return Topics(
      leads_raw=t["leads_raw"],
      leads_normalized=t["leads_normalized"],
      leads_validated=t["leads_validated"],
      leads_enriched=t["leads_enriched"],
      leads_scored=t["leads_scored"],
      leads_routed=t["leads_routed"],
      consent_events=t["consent_events"],
      touches_events=t["touches_events"],
      suppression_events=t["suppression_events"],
      dlq_events=t["dlq_events"],
    )
'

write_file src/pipeline/orchestrator.py '
import os
import subprocess
import threading
from prometheus_client import start_http_server
import uvicorn
from ..core.settings import load_settings
from ..api.app import create_app

def _migrate():
  env = os.environ.copy()
  env.setdefault("POSTGRES_HOST", "postgres")
  env.setdefault("POSTGRES_PORT", "5432")
  env.setdefault("POSTGRES_DB", "eastwood")
  env.setdefault("POSTGRES_USER", "eastwood")
  env.setdefault("POSTGRES_PASSWORD", "eastwood")
  subprocess.run(["bash", "scripts/db_migrate.sh"], check=False, env=env)

def run():
  s = load_settings()
  start_http_server(s.metrics_port)
  _migrate()
  app = create_app()
  uvicorn.run(app, host=s.api_host, port=s.api_port, log_level="info")
'

echo "done"

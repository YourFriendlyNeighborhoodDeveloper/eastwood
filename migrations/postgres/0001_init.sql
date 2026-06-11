CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS leads (
  lead_id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source TEXT,
  first_name TEXT,
  last_name TEXT,
  email TEXT,
  phone TEXT,
  state TEXT,
  zip TEXT,
  product TEXT,
  intent_score DOUBLE PRECISION,
  raw JSONB NOT NULL DEFAULT '{}'::jsonb
);

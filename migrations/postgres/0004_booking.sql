CREATE TABLE IF NOT EXISTS consent_ledger (
  lead_id BIGINT PRIMARY KEY REFERENCES leads(lead_id) ON DELETE CASCADE,
  sms_consent BOOLEAN NOT NULL DEFAULT false,
  email_consent BOOLEAN NOT NULL DEFAULT false,
  consent_source TEXT,
  consented_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  raw JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS booking_tokens (
  token TEXT PRIMARY KEY,
  lead_id BIGINT NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_booking_tokens_lead_id ON booking_tokens(lead_id);
CREATE INDEX IF NOT EXISTS idx_booking_tokens_expires_at ON booking_tokens(expires_at);

CREATE TABLE IF NOT EXISTS appointments (
  appointment_id TEXT PRIMARY KEY,
  lead_id BIGINT NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
  start_at TIMESTAMPTZ NOT NULL,
  tz TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'booked',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  confirm_sent_at TIMESTAMPTZ NULL,
  reminder_24h_sent_at TIMESTAMPTZ NULL,
  reminder_2h_sent_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_appointments_start_at ON appointments(start_at);
CREATE INDEX IF NOT EXISTS idx_appointments_lead_id ON appointments(lead_id);
CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments(status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_consent_ledger_lead_id ON consent_ledger(lead_id);

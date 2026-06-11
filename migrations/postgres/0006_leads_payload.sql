ALTER TABLE leads
  ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;

-- optional: if you have an existing "raw" column and want to copy it over once
UPDATE leads
SET payload = raw
WHERE payload = '{}'::jsonb AND raw IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_leads_payload_gin ON leads USING GIN (payload);

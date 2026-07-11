-- National Do Not Call Registry clearance.
--
-- Separate from the suppressions table, which records entity-specific DNC
-- requests made to us directly. This records whether the number may be
-- cold-called at all.
--
-- Existing rows default to 'unknown', which is not dialable. Backfilling to
-- 'clear' would silently release every lead already in the table.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS national_dnc_status VARCHAR(16) NOT NULL DEFAULT 'unknown';
ALTER TABLE leads ADD COLUMN IF NOT EXISTS national_dnc_checked_at TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS national_dnc_provider VARCHAR(64);
ALTER TABLE leads ADD COLUMN IF NOT EXISTS national_dnc_source VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_leads_national_dnc_status ON leads(national_dnc_status);

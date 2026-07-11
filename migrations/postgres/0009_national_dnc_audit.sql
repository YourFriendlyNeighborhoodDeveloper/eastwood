-- Structured provenance for a National DNC Registry scrub decision.
--
-- Holds the audit trail returned by the ftc_registry provider: dataset age,
-- SAN fingerprint (never the raw SAN), area-code scope, checksum, and reason.
-- Never holds a raw SAN, credentials, or any registry phone number.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS national_dnc_audit JSONB;

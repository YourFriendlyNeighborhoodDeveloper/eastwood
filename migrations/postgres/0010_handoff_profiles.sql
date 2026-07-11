-- Prospect handoff profile assembled at booking.
--
-- The canonical store for the brief the licensed agent receives by SMS, so it
-- can be retrieved in full later even if the SMS was truncated. The SMS is the
-- alert; this is the record.

CREATE TABLE IF NOT EXISTS handoff_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    appointment_id UUID,
    tenant_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64),
    profile JSONB NOT NULL,
    agent_notified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_handoff_profiles_lead_id ON handoff_profiles(lead_id);
CREATE INDEX IF NOT EXISTS ix_handoff_profiles_appointment_id ON handoff_profiles(appointment_id);

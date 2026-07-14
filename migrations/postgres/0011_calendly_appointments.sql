-- Calendly booking record on appointments.
-- calendly_status: not_requested | confirmed | failed
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS prospect_email VARCHAR(256);
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS timezone VARCHAR(64);
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendly_event_uri TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendly_invitee_uri TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendly_cancel_url TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendly_reschedule_url TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendly_status VARCHAR(32) NOT NULL DEFAULT 'not_requested';

CREATE TABLE IF NOT EXISTS carrier_rate_tables (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    carrier_name VARCHAR(128) NOT NULL,
    product_line VARCHAR(64) NOT NULL,
    rate_tier VARCHAR(32) NOT NULL,
    state CHAR(2),
    age INTEGER NOT NULL,
    gender CHAR(1),
    tobacco BOOLEAN NOT NULL DEFAULT FALSE,
    face_value_cents BIGINT NOT NULL,
    monthly_premium_cents INTEGER NOT NULL,
    effective_date DATE NOT NULL,
    expires_date DATE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_crt_lookup
    ON carrier_rate_tables (product_line, age, tobacco, face_value_cents)
    WHERE is_active;

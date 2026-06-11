#!/usr/bin/env bash
set -euo pipefail

dsn="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"

psql "$dsn" -f migrations/postgres/0001_init.sql
psql "$dsn" -f migrations/postgres/0002_indexes.sql
psql "$dsn" -f migrations/postgres/0003_leads_status.sql
psql "$dsn" -f migrations/postgres/0004_booking.sql
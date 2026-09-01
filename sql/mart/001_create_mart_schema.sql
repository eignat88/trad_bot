-- 001_create_mart_schema.sql
-- Idempotent: safe to run multiple times.
-- Creates the `mart` analytics schema and grants read-only access to grafana_reader.

-- 1. Create schema
CREATE SCHEMA IF NOT EXISTS mart;

-- 2. Create grafana_reader role (login disabled by default; enable on VPS)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        CREATE ROLE grafana_reader LOGIN NOINHERIT;
    END IF;
END
$$;

-- 3. Grant usage on schemas
GRANT USAGE ON SCHEMA dds    TO grafana_reader;
GRANT USAGE ON SCHEMA mart   TO grafana_reader;

-- 4. Grant SELECT on existing tables
GRANT SELECT ON ALL TABLES IN SCHEMA dds  TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO grafana_reader;

-- 5. Default privileges so future tables/views are also readable
ALTER DEFAULT PRIVILEGES IN SCHEMA dds  GRANT SELECT ON TABLES TO grafana_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA mart GRANT SELECT ON TABLES TO grafana_reader;

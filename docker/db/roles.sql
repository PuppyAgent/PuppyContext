-- Supabase platform credentials only. Product schema belongs in supabase/migrations.
-- Pattern: supabase/supabase docker/volumes/db/roles.sql (Apache-2.0).
\getenv pgpass POSTGRES_PASSWORD
ALTER USER authenticator WITH PASSWORD :'pgpass';
ALTER USER supabase_auth_admin WITH PASSWORD :'pgpass';

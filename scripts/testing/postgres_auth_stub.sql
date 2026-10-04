-- Disposable native PostgreSQL fixtures only; NOT a Supabase Auth emulator.
-- Roles are provisioned by the owning stack, not by this per-database stub.
CREATE SCHEMA auth;
CREATE SCHEMA extensions;
CREATE TABLE auth.users(
    instance_id uuid, id uuid PRIMARY KEY, aud text, role text, email text,
    encrypted_password text, email_confirmed_at timestamptz, raw_app_meta_data jsonb,
    raw_user_meta_data jsonb, created_at timestamptz, updated_at timestamptz
);
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid
$$;
CREATE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT coalesce(nullif(current_setting('request.jwt.claims',true),''),'{}')::jsonb
$$;

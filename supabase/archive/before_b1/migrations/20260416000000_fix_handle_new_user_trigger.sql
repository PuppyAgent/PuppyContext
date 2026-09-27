-- ============================================================
-- Migration: Fix handle_new_user() trigger function
--
-- Problem: The production version of handle_new_user() still
-- references the dropped columns `role` and `plan`, causing
-- 500 "Database error saving new user" on every signup.
--
-- Root cause: The prod_alignment migration (20260308) dropped
-- `role` and `plan` from profiles but did not update the
-- trigger function that referenced them.
--
-- This migration replaces the function with the correct version
-- that only inserts (user_id, email, display_name).
-- ============================================================

CREATE OR REPLACE FUNCTION "public"."handle_new_user"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
AS $$
BEGIN
    INSERT INTO public.profiles (user_id, email, display_name)
    VALUES (
        NEW.id,
        COALESCE(
            NEW.email,
            NEW.raw_user_meta_data ->> 'email',
            ''
        ),
        COALESCE(
            NEW.raw_user_meta_data ->> 'full_name',
            split_part(COALESCE(NEW.email, ''), '@', 1),
            'User'
        )
    )
    ON CONFLICT (user_id) DO NOTHING;

    RETURN NEW;
END;
$$;

ALTER FUNCTION "public"."handle_new_user"() OWNER TO "postgres";

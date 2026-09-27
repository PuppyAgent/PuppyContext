DO $$
DECLARE
    has_plaintext boolean;
    has_hash boolean;
    pending_count bigint;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'repo_scopes'
          AND column_name = 'access_key'
    ) INTO has_plaintext;
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'repo_scopes'
          AND column_name = 'access_key_hash'
    ) INTO has_hash;

    IF NOT has_plaintext AND NOT has_hash THEN
        RETURN;
    END IF;
    IF has_plaintext IS DISTINCT FROM has_hash THEN
        RAISE EXCEPTION
          'scope credential verification found a partially retired schema';
    END IF;

    EXECUTE 'SELECT count(*) FROM public.repo_scopes '
            'WHERE access_key IS NOT NULL AND access_key_hash IS NULL'
       INTO pending_count;
    IF pending_count > 0 THEN
        RAISE EXCEPTION
          'scope credential backfill incomplete: % row(s) remain', pending_count;
    END IF;
END;
$$;

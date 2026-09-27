-- Read-only catalog projection. No customer rows or sequence current values.
-- OIDs, physical column positions and platform extension internals are excluded.
-- SET search_path = pg_catalog before evaluating this query.
WITH relations AS (
    SELECT c.* FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
      AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
          AND d.objid = c.oid AND d.deptype = 'e')
), routines AS (
    SELECT p.* FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
      AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
          AND d.objid = p.oid AND d.deptype = 'e')
), objects AS (
    SELECT 'schema' AS kind, n.nspname::text AS name,
        jsonb_build_object('owner', pg_get_userbyid(n.nspowner),
            'acl', (SELECT jsonb_agg(a::text ORDER BY a::text COLLATE "C") FROM unnest(coalesce(n.nspacl, acldefault('n', n.nspowner))) a)) AS definition
    FROM pg_namespace n WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'relation', c.relname,
        jsonb_build_object('kind', c.relkind, 'owner', pg_get_userbyid(c.relowner),
            'rls', c.relrowsecurity, 'force_rls', c.relforcerowsecurity,
            'replica_identity', c.relreplident, 'persistence', c.relpersistence,
            'options', c.reloptions, 'partition', pg_get_partkeydef(c.oid),
            'view', CASE WHEN c.relkind IN ('v', 'm') THEN pg_get_viewdef(c.oid, false) END,
            'acl', (SELECT jsonb_agg(a::text ORDER BY a::text COLLATE "C") FROM unnest(coalesce(c.relacl, acldefault(CASE WHEN c.relkind='S' THEN 's'::"char" ELSE 'r'::"char" END, c.relowner))) a))
    FROM relations c
    UNION ALL
    SELECT 'column', c.relname || '.' || a.attname,
        jsonb_build_object('type', format_type(a.atttypid, a.atttypmod),
            'not_null', a.attnotnull, 'identity', a.attidentity, 'generated', a.attgenerated,
            'default', pg_get_expr(d.adbin, d.adrelid),
            'collation', CASE WHEN a.attcollation <> 0 THEN a.attcollation::regcollation::text END,
            'acl', (SELECT jsonb_agg(x::text ORDER BY x::text COLLATE "C") FROM unnest(a.attacl) x))
    FROM relations c JOIN pg_attribute a ON a.attrelid = c.oid
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attnum > 0 AND NOT a.attisdropped
    UNION ALL
    SELECT 'constraint', c.relname || '.' || k.conname,
        jsonb_build_object('definition', pg_get_constraintdef(k.oid, false), 'validated', k.convalidated)
    FROM relations c JOIN pg_constraint k ON k.conrelid = c.oid
    UNION ALL
    SELECT 'index', i.indexrelid::regclass::text,
        jsonb_build_object('definition', pg_get_indexdef(i.indexrelid),
            'valid', i.indisvalid, 'ready', i.indisready, 'clustered', i.indisclustered)
    FROM relations c JOIN pg_index i ON i.indrelid = c.oid
    UNION ALL
    SELECT 'routine', p.oid::regprocedure::text,
        jsonb_build_object('definition', pg_get_functiondef(p.oid), 'owner', pg_get_userbyid(p.proowner),
            'acl', (SELECT jsonb_agg(a::text ORDER BY a::text COLLATE "C") FROM unnest(coalesce(p.proacl, acldefault('f', p.proowner))) a))
    FROM routines p
    UNION ALL
    SELECT 'policy', c.relname || '.' || p.polname,
        jsonb_build_object('command', p.polcmd, 'permissive', p.polpermissive,
            'roles', (SELECT jsonb_agg(CASE WHEN r = 0 THEN 'public' ELSE pg_get_userbyid(r) END
                ORDER BY r::regrole::text COLLATE "C") FROM unnest(p.polroles) r),
            'using', pg_get_expr(p.polqual, p.polrelid), 'check', pg_get_expr(p.polwithcheck, p.polrelid))
    FROM relations c JOIN pg_policy p ON p.polrelid = c.oid
    UNION ALL
    SELECT 'trigger', n.nspname || '.' || c.relname || '.' || t.tgname,
        jsonb_build_object('definition', pg_get_triggerdef(t.oid, false), 'enabled', t.tgenabled)
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_proc p ON p.oid = t.tgfoid JOIN pg_namespace fn ON fn.oid = p.pronamespace
    WHERE NOT t.tgisinternal AND (c.oid IN (SELECT oid FROM relations) OR fn.nspname = 'public')
    UNION ALL
    SELECT 'sequence', c.relname,
        jsonb_build_object('type', format_type(s.seqtypid, NULL), 'start', s.seqstart,
            'increment', s.seqincrement, 'min', s.seqmin, 'max', s.seqmax,
            'cache', s.seqcache, 'cycle', s.seqcycle,
            'owned_by', (SELECT rc.relname || '.' || a.attname FROM pg_depend d
                JOIN pg_class rc ON rc.oid = d.refobjid
                JOIN pg_attribute a ON a.attrelid = rc.oid AND a.attnum = d.refobjsubid
                WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('a','i')))
    FROM relations c JOIN pg_sequence s ON s.seqrelid = c.oid
    UNION ALL
    SELECT 'enum', t.typname,
        jsonb_build_object('owner', pg_get_userbyid(t.typowner),
            'labels', (SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid = t.oid),
            'acl', (SELECT jsonb_agg(a::text ORDER BY a::text COLLATE "C") FROM unnest(coalesce(t.typacl, acldefault('T', t.typowner))) a))
    FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'public' AND t.typtype = 'e'
    UNION ALL
    SELECT 'default_acl', pg_get_userbyid(d.defaclrole) || '.' || d.defaclobjtype::text,
        jsonb_build_object('acl', (SELECT jsonb_agg(a::text ORDER BY a::text COLLATE "C") FROM unnest(d.defaclacl) a))
    FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace
    WHERE n.nspname = 'public' AND pg_get_userbyid(d.defaclrole) = 'postgres'
)
SELECT encode(sha256(convert_to(coalesce(jsonb_agg(
    jsonb_build_object('kind', kind, 'name', name, 'definition', definition)
    ORDER BY kind COLLATE "C", name COLLATE "C"), '[]'::jsonb)::text, 'UTF8')), 'hex') AS fingerprint
FROM objects;

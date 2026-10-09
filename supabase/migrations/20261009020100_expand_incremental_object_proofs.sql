-- Durable immutable-object facts. No object migration or S3 I/O in DDL.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';
CREATE TABLE public.version_object_proofs (
 project_id text NOT NULL REFERENCES public.version_repositories(project_id) ON DELETE CASCADE,
 generation bigint NOT NULL,
 object_id text NOT NULL CHECK(object_id ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
 object_format text NOT NULL CHECK(object_format IN ('sha1','sha256')),
 kind text NOT NULL CHECK(kind IN ('commit','tree','blob','tag')),
 body_bytes bigint NOT NULL CHECK(body_bytes>=0),
 body_sha256 text NOT NULL CHECK(body_sha256 ~ '^[0-9a-f]{64}$'),
 edges jsonb NOT NULL CHECK(jsonb_typeof(edges)='array'),
 logical_bytes bigint NOT NULL CHECK(logical_bytes>=0),
 max_blob_bytes bigint NOT NULL CHECK(max_blob_bytes>=0),
 issuer_pin uuid NOT NULL,
 published boolean NOT NULL DEFAULT false,
 valid boolean NOT NULL DEFAULT true,
 PRIMARY KEY(project_id,generation,object_id)
);
CREATE INDEX version_object_proofs_issuer ON public.version_object_proofs(project_id,issuer_pin) WHERE NOT published;
CREATE TABLE public.version_object_proof_edges (
 project_id text NOT NULL,
 generation bigint NOT NULL,
 parent_oid text NOT NULL,
 child_oid text NOT NULL,
 PRIMARY KEY(project_id,generation,parent_oid,child_oid),
 FOREIGN KEY(project_id,generation,parent_oid) REFERENCES public.version_object_proofs(project_id,generation,object_id) ON DELETE CASCADE
);
CREATE INDEX version_object_proof_dependents ON public.version_object_proof_edges(project_id,generation,child_oid,parent_oid);
ALTER TABLE public.version_object_proofs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_object_proof_edges ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.version_object_proofs,public.version_object_proof_edges FROM PUBLIC,anon,authenticated,service_role;

CREATE FUNCTION public._version_proof_pin(p_project_id text,p_actor text,p_pin_id uuid,p_purpose text DEFAULT 'publication')
RETURNS public.version_object_pins LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE; pin public.version_object_pins%ROWTYPE;
BEGIN
 SELECT * INTO STRICT repo FROM public.version_repositories WHERE project_id=p_project_id FOR SHARE;
 SELECT * INTO pin FROM public.version_object_pins WHERE id=p_pin_id FOR SHARE;
 IF NOT FOUND OR p_purpose NOT IN ('publication','read') OR pin.project_id<>p_project_id OR pin.actor<>p_actor OR pin.purpose<>p_purpose
   OR pin.state='released' OR pin.expires_at<=clock_timestamp() OR pin.generation<>repo.generation
   OR pin.object_format<>repo.object_format OR pin.gc_epoch<>repo.gc_epoch OR repo.gc_token IS NOT NULL
   THEN RAISE EXCEPTION 'publication_pin_unavailable'; END IF;
 RETURN pin;
END $$;

CREATE FUNCTION public.get_version_object_proofs(p_project_id text,p_actor text,p_pin_id uuid,p_oids jsonb,p_purpose text DEFAULT 'publication')
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE pin public.version_object_pins%ROWTYPE; result jsonb;
BEGIN
 pin:=public._version_proof_pin(p_project_id,p_actor,p_pin_id,p_purpose);
 IF jsonb_typeof(p_oids)<>'array' OR jsonb_array_length(p_oids)>200 THEN RAISE EXCEPTION 'object_proof_batch_invalid'; END IF;
 SELECT coalesce(jsonb_object_agg(p.object_id,to_jsonb(p)),'{}'::jsonb) INTO result
 FROM public.version_object_proofs p WHERE p.project_id=p_project_id AND p.generation=pin.generation
 AND p.object_id IN (SELECT jsonb_array_elements_text(p_oids))
 AND (p.published OR (p_purpose='publication' AND p.issuer_pin=p_pin_id));
 RETURN result;
END $$;

CREATE FUNCTION public.register_version_object_proofs(p_project_id text,p_actor text,p_pin_id uuid,p_rows jsonb)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE pin public.version_object_pins%ROWTYPE; item jsonb; prior public.version_object_proofs%ROWTYPE; n integer:=0;
BEGIN
 pin:=public._version_proof_pin(p_project_id,p_actor,p_pin_id);
 IF pin.state<>'uploading' THEN RAISE EXCEPTION 'publication_already_sealed'; END IF;
 IF jsonb_typeof(p_rows)<>'array' OR jsonb_array_length(p_rows) NOT BETWEEN 1 AND 200
   OR octet_length(p_rows::text)>4194304 THEN RAISE EXCEPTION 'object_proof_batch_invalid'; END IF;
 -- Input is dependency ordered. Bytes and edge types are physically verified
 -- by the same trusted backend that issues publication seal attestations.
 FOR item IN SELECT value FROM jsonb_array_elements(p_rows) LOOP
   IF NOT public._version_oid_valid(item->>'object_id',pin.object_format)
     OR NOT EXISTS(SELECT 1 FROM public.version_repository_object_capacity
       WHERE project_id=p_project_id AND object_id=item->>'object_id'
         AND object_kind=item->>'kind' AND body_bytes=(item->>'body_bytes')::bigint)
     THEN RAISE EXCEPTION 'object_proof_capacity_missing'; END IF;
   -- One set query for the object's edges, not one SQL command per edge.
   IF EXISTS(SELECT 1 FROM jsonb_array_elements(item->'edges') edge
     WHERE NOT EXISTS(SELECT 1 FROM public.version_object_proofs WHERE project_id=p_project_id
       AND generation=pin.generation AND object_id=edge->>0 AND kind=edge->>1 AND valid
       AND (published OR issuer_pin=p_pin_id))) THEN
     RAISE EXCEPTION 'object_proof_dependency_missing'; END IF;
   SELECT * INTO prior FROM public.version_object_proofs WHERE project_id=p_project_id
     AND generation=pin.generation AND object_id=item->>'object_id' FOR UPDATE;
   IF FOUND AND (prior.kind<>item->>'kind' OR prior.body_bytes<>(item->>'body_bytes')::bigint
     OR prior.body_sha256<>item->>'body_sha256' OR prior.edges<>item->'edges') THEN
     RAISE EXCEPTION 'immutable_object_proof_conflict'; END IF;
   INSERT INTO public.version_object_proofs(project_id,generation,object_id,object_format,kind,body_bytes,body_sha256,
     edges,logical_bytes,max_blob_bytes,issuer_pin)
   VALUES(p_project_id,pin.generation,item->>'object_id',pin.object_format,item->>'kind',(item->>'body_bytes')::bigint,
     item->>'body_sha256',item->'edges',(item->>'logical_bytes')::bigint,(item->>'max_blob_bytes')::bigint,p_pin_id)
   ON CONFLICT(project_id,generation,object_id) DO UPDATE SET valid=true,
     issuer_pin=CASE WHEN version_object_proofs.valid AND version_object_proofs.published THEN version_object_proofs.issuer_pin ELSE EXCLUDED.issuer_pin END,
     published=version_object_proofs.valid AND version_object_proofs.published,
     logical_bytes=EXCLUDED.logical_bytes,max_blob_bytes=EXCLUDED.max_blob_bytes;
   INSERT INTO public.version_object_proof_edges(project_id,generation,parent_oid,child_oid)
   SELECT p_project_id,pin.generation,item->>'object_id',value->>0 FROM jsonb_array_elements(item->'edges') ON CONFLICT DO NOTHING;
   n:=n+1;
 END LOOP;
 RETURN n;
END $$;

CREATE FUNCTION public._version_publish_object_proofs() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NEW.result->>'status'='committed' AND NEW.result->>'receipt_id' IS NOT NULL THEN
   UPDATE public.version_object_proofs SET published=true
   WHERE project_id=NEW.project_id AND issuer_pin=(NEW.result->>'receipt_id')::uuid AND NOT published AND valid;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER version_publish_object_proofs AFTER INSERT ON public.version_ref_transactions
 FOR EACH ROW EXECUTE FUNCTION public._version_publish_object_proofs();

CREATE FUNCTION public._version_check_incremental_publication() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE receipt public.version_publication_receipts%ROWTYPE; root record;
BEGIN
 IF TG_TABLE_NAME='version_publication_receipts' THEN receipt:=NEW;
 ELSE
   IF NEW.result->>'status'<>'committed' OR NEW.result->>'receipt_id' IS NULL THEN RETURN NEW; END IF;
   SELECT * INTO STRICT receipt FROM public.version_publication_receipts WHERE id=(NEW.result->>'receipt_id')::uuid;
 END IF;
 FOR root IN SELECT key,value FROM jsonb_each_text(receipt.roots) LOOP
   -- Primitive full-verification callers need no derived index. Once a proof
   -- exists, a known-invalid root cannot bypass its invalidation at seal/CAS.
   IF EXISTS(SELECT 1 FROM public.version_object_proofs WHERE project_id=receipt.project_id
     AND generation=receipt.generation AND object_id=root.key AND (NOT valid OR kind<>root.value)) THEN
     RAISE EXCEPTION 'object_proof_invalidated'; END IF;
 END LOOP;
 RETURN NEW;
END $$;
CREATE TRIGGER version_receipt_incremental_fence BEFORE INSERT ON public.version_publication_receipts
 FOR EACH ROW EXECUTE FUNCTION public._version_check_incremental_publication();
CREATE TRIGGER version_transaction_incremental_fence BEFORE INSERT ON public.version_ref_transactions
 FOR EACH ROW EXECUTE FUNCTION public._version_check_incremental_publication();

CREATE FUNCTION public.invalidate_version_object_proofs(p_project_id text,p_oids jsonb)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE n bigint; g bigint;
BEGIN
 -- Called before physical collection, or after a confirmed integrity failure.
 -- Invalidates dependent proofs, not unrelated history. Maintenance has its
 -- own work budget; this reverse walk never runs in normal publication.
 SELECT generation INTO STRICT g FROM public.version_repositories WHERE project_id=p_project_id FOR UPDATE;
 IF jsonb_typeof(p_oids)<>'array' OR jsonb_array_length(p_oids) NOT BETWEEN 1 AND 200 THEN
   RAISE EXCEPTION 'object_proof_batch_invalid'; END IF;
 WITH RECURSIVE affected(oid) AS (
   SELECT jsonb_array_elements_text(p_oids)
   UNION
   SELECT e.parent_oid FROM public.version_object_proof_edges e JOIN affected a ON e.child_oid=a.oid
     WHERE e.project_id=p_project_id AND e.generation=g
 ) UPDATE public.version_object_proofs SET valid=false
 WHERE project_id=p_project_id AND generation=g AND object_id IN (SELECT oid FROM affected) AND valid;
 GET DIAGNOSTICS n=ROW_COUNT;
 RETURN n;
END $$;

REVOKE ALL ON FUNCTION public._version_proof_pin(text,text,uuid,text),public._version_publish_object_proofs(),public._version_check_incremental_publication() FROM PUBLIC,anon,authenticated,service_role;
REVOKE ALL ON FUNCTION public.get_version_object_proofs(text,text,uuid,jsonb,text),public.register_version_object_proofs(text,text,uuid,jsonb),public.invalidate_version_object_proofs(text,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.get_version_object_proofs(text,text,uuid,jsonb,text),public.register_version_object_proofs(text,text,uuid,jsonb),public.invalidate_version_object_proofs(text,jsonb) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;

-- Durable input/base selection for retryable Upload/Import/Synchronize jobs.
BEGIN;
CREATE TABLE public.version_producer_inputs (
    project_id text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    actor text NOT NULL,
    request_key uuid NOT NULL,
    input_sha256 text NOT NULL CHECK(input_sha256 ~ '^[0-9a-f]{64}$'),
    base jsonb NOT NULL CHECK(jsonb_typeof(base)='object' AND octet_length(base::text)<8192),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id,actor,request_key)
);
ALTER TABLE public.version_producer_inputs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.version_producer_inputs FROM PUBLIC,anon,authenticated,service_role;
CREATE FUNCTION public.bind_version_producer_input(p_project_id text,p_actor text,p_request_key uuid,
    p_input_sha256 text,p_base jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE project public.projects%ROWTYPE; input public.version_producer_inputs%ROWTYPE;
BEGIN
    project:=public._version_lock_admission_project(p_project_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    SELECT * INTO input FROM public.version_producer_inputs
      WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,true);
        INSERT INTO public.version_producer_inputs(project_id,actor,request_key,input_sha256,base)
          VALUES(p_project_id,p_actor,p_request_key,p_input_sha256,p_base)
          ON CONFLICT DO NOTHING;
        SELECT * INTO input FROM public.version_producer_inputs
          WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key FOR UPDATE;
    END IF;
    IF input.input_sha256 IS DISTINCT FROM p_input_sha256 THEN
        RAISE EXCEPTION 'request_key_reused' USING ERRCODE='22023'; END IF;
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    RETURN input.base;
END $$;
REVOKE ALL ON FUNCTION public.bind_version_producer_input(text,text,uuid,text,jsonb) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.bind_version_producer_input(text,text,uuid,text,jsonb) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;

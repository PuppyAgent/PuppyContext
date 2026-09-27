BEGIN;
-- Synthetic B1 records. Never apply to a hosted database.
INSERT INTO auth.users (instance_id,id,aud,role,email,encrypted_password,email_confirmed_at,
 raw_app_meta_data,raw_user_meta_data,created_at,updated_at)
VALUES ('00000000-0000-0000-0000-000000000000','00000000-0000-4000-8000-000000000049',
 'authenticated','authenticated','issue049@example.test','',now(),'{}','{}',now(),now());
INSERT INTO public.organizations(id,name,slug,type,plan,seat_limit,created_by)
VALUES ('issue049-org','Migration fixture','issue049-org','team','free',1,'00000000-0000-4000-8000-000000000049'),
 ('issue049-other-org','Other tenant','issue049-other-org','team','free',1,'00000000-0000-4000-8000-000000000049');
INSERT INTO public.org_members(id,org_id,user_id,role)
VALUES ('issue049-member','issue049-org','00000000-0000-4000-8000-000000000049','owner'),
 ('issue049-other-member','issue049-other-org','00000000-0000-4000-8000-000000000049','owner');
INSERT INTO public.projects(id,name,org_id,created_by,share_token,lifecycle_status)
VALUES ('issue049-project','Project','issue049-org','00000000-0000-4000-8000-000000000049','issue049-share','ready'),
 ('issue049-other-project','Other project','issue049-other-org','00000000-0000-4000-8000-000000000049','issue049-other-share','ready');
INSERT INTO public.project_members(id,org_id,project_id,user_id,role,granted_by)
VALUES ('issue049-pm','issue049-org','issue049-project','00000000-0000-4000-8000-000000000049','admin','00000000-0000-4000-8000-000000000049'),
 ('issue049-other-pm','issue049-other-org','issue049-other-project','00000000-0000-4000-8000-000000000049','admin','00000000-0000-4000-8000-000000000049');
INSERT INTO public.access_surfaces(id,org_id,project_id,scope_id,kind,name,status,principal_type,principal_id,config,created_by)
VALUES ('issue049-surface','issue049-org','issue049-project',NULL,'agent','Agent','active','project','issue049-project','{}','00000000-0000-4000-8000-000000000049'),
 ('issue049-other-surface','issue049-other-org','issue049-other-project',NULL,'agent','Agent','active','project','issue049-other-project','{}','00000000-0000-4000-8000-000000000049');
INSERT INTO public.tools(id,created_by,project_id,json_path,type,name,category,org_id)
SELECT 'issue049-tool-'||n,'00000000-0000-4000-8000-000000000049','issue049-project','','search','Search '||n,'builtin','issue049-org'
FROM generate_series(1,5) n;
INSERT INTO public.access_tools(id,access_point_id,tool_id,enabled,mcp_exposed)
VALUES ('issue049-link','issue049-surface','issue049-tool-1',true,true);
INSERT INTO public.oauth_connections(user_id,provider,access_token,metadata)
VALUES ('00000000-0000-4000-8000-000000000049','github','synthetic-token','{"fixture":49}');
INSERT INTO public.github_integrations(id,project_id,oauth_connection_id,github_repo_owner,github_repo_name,
 default_branch,webhook_secret,auto_import,last_imported_sha,last_imported_at,last_exported_sha,last_exported_at)
SELECT 'issue049-binding','issue049-project',id,'fixture','repository','main','synthetic-secret',true,
 repeat('a',40),now(),repeat('b',40),now() FROM public.oauth_connections WHERE provider='github';
INSERT INTO public.github_sync_log(id,integration_id,direction,git_sha,status,error_message,files_changed)
VALUES ('issue049-log-success','issue049-binding','import',repeat('a',40),'success',NULL,7),
 ('issue049-log-failed','issue049-binding','export',repeat('b',40),'failed','fixture failure',0);
INSERT INTO public.uploads(id,created_by,project_id,path,type,config,status,progress,message,error,result_path,result,
 created_at,updated_at,started_at,completed_at)
SELECT 'issue049-tool-'||n,'00000000-0000-4000-8000-000000000049','issue049-project','docs/'||n,'search_index',
 jsonb_build_object('tool_id','issue049-tool-'||n,'json_path','/items','folder_path','docs','extension','preserved'),
 (ARRAY['pending','running','completed','failed','cancelled'])[n],n*20,'progress note','retained note','s3://fixture/result',
 '{"nodes_count":8,"chunks_count":10,"indexed_chunks_count":3,"total_files":6,"indexed_files":2,"extension":"preserved"}',
 '2026-01-01T00:00:00Z','2026-01-02T00:00:00Z','2026-01-01T01:00:00Z','2026-01-02T01:00:00Z'
FROM generate_series(1,5) n;
INSERT INTO public.uploads(id,created_by,project_id,path,type,config,status)
SELECT 'issue049-upload-'||n,'00000000-0000-4000-8000-000000000049','issue049-project','file/'||n,
 (ARRAY['file_ocr','file_postprocess','import'])[n],'{"sentinel":"keep"}','pending' FROM generate_series(1,3) n;

COMMIT;

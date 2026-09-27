-- Drop legacy tables from pre-entitlement / pre-access-surfaces schemas.
--
-- Current runtime replacements:
-- - MCP endpoints are backed by access_surfaces + repo_scopes.
-- - Agent chat is backed by chat_sessions + chat_messages.
-- - Billing is backed by organization_entitlements.
--
-- Intentionally not dropping public.mcps: the legacy MCP instance repository
-- still references that table.

BEGIN;

DROP VIEW IF EXISTS public.credit_usage_by_prefix;
DROP VIEW IF EXISTS public.credit_balance;

DROP FUNCTION IF EXISTS public.sp_consume_credits(uuid, integer, text, jsonb);
DROP FUNCTION IF EXISTS public.sp_grant_credits(uuid, integer, text, jsonb);

DROP TABLE IF EXISTS public.mcp_binding;
DROP TABLE IF EXISTS public.mcp_bindings;
DROP TABLE IF EXISTS public.mcp_endpoints;
DROP TABLE IF EXISTS public.mcp;
DROP TABLE IF EXISTS public.messages;
DROP TABLE IF EXISTS public.threads;
DROP TABLE IF EXISTS public.credit_ledger;

DROP SEQUENCE IF EXISTS public.mcp_binding_id_seq;
DROP SEQUENCE IF EXISTS public.mcp_bindings_id_seq;
DROP SEQUENCE IF EXISTS public.mcp_id_seq;
DROP SEQUENCE IF EXISTS public.credit_ledger_id_seq;

COMMIT;

-- Reviewed fresh-install reference data, NOT production data or demo seeds.
-- The inventory starts incomplete. This must not fabricate an S3 inventory
-- completion receipt or enable deletion before its normal admission checks.
INSERT INTO public.project_storage_inventory_state (singleton) VALUES (true);

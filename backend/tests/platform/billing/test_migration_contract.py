from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "supabase/archive/before_b1/migrations/20260714010000_unified_billing_control_plane.sql"
)
_LIFECYCLE_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "supabase/archive/before_b1/migrations/20260715010000_durable_billing_operation_lifecycle.sql"
)


def _migration_sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _lifecycle_migration_sql() -> str:
    return _LIFECYCLE_MIGRATION.read_text(encoding="utf-8")


def test_reconciliation_claim_uses_unambiguous_primary_key_conflict_target() -> None:
    assert "ON CONFLICT ON CONSTRAINT organization_usage_counters_pkey" in _migration_sql()


def test_ownership_transfer_does_not_shadow_postgres_current_role() -> None:
    migration = _migration_sql()

    assert "stored_current_role text;" in migration
    assert "IF current_role IS DISTINCT FROM 'owner'" not in migration


def test_entitlement_provisioning_uses_a_service_role_lease_and_stable_key() -> None:
    migration = _migration_sql()

    assert "'entitlement_provision'" in migration
    assert "'entitlement-provision:v1'" in migration
    assert "FOR UPDATE SKIP LOCKED" in migration
    assert (
        "REVOKE ALL ON FUNCTION public.enqueue_missing_entitlement_provisioning(integer)"
        in migration
    )
    assert (
        "REVOKE ALL ON FUNCTION public.claim_entitlement_provisioning_batch(integer, integer)"
        in migration
    )


def test_seat_proposal_outbox_uses_a_service_role_skip_locked_lease() -> None:
    migration = _migration_sql()

    assert "claim_seat_proposal_batch" in migration
    assert "candidate.kind IN ('member_activation', 'member_deactivation')" in migration
    assert "FOR UPDATE SKIP LOCKED" in migration
    assert "REVOKE ALL ON FUNCTION public.claim_seat_proposal_batch(integer, integer)" in migration


def test_atomic_seat_admission_never_bootstraps_from_caller_values() -> None:
    migration = _migration_sql()

    assert "an effective entitlement snapshot is required for seat admission" in migration
    assert "p_purchased_seat_quantity" not in migration
    assert "requires_business_plan" not in migration
    assert "target_quantity < 15" not in migration


def test_atomic_storage_admission_uses_persisted_revision_pinned_limit() -> None:
    migration = _migration_sql()

    assert "effective_storage_limit" in migration
    assert "storage_billing_entitlement_changed" in migration
    assert "p_entitlement_source_revision IS DISTINCT FROM" in migration
    assert "new_value > p_storage_limit" not in migration


def test_usage_reconciliation_allows_zero_quota_and_rejects_mutated_replays() -> None:
    migration = _migration_sql()

    assert "p_limit IS NOT NULL AND p_limit < 0" in migration
    assert "WHEN p_limit = 0 AND p_value = 0 THEN 0" in migration
    assert "usage_idempotency_payload_mismatch" in migration
    assert "'requested_value', p_value" in migration
    assert "'requested_limit', p_limit" in migration


def test_entitlement_publication_rejects_incomplete_or_wrong_authority_rows() -> None:
    migration = _migration_sql()

    assert "p_source IS DISTINCT FROM 'puppypay'" in migration
    assert "p_catalog_version = 'legacy'" in migration
    assert "p_effective_at IS NULL" in migration


def test_commercial_operations_are_correlated_atomically_to_entitlement_revision() -> None:
    migration = _lifecycle_migration_sql()

    assert "source_quote_id" in migration
    assert "trg_reset_entitlement_quote_on_revision_advance" in migration
    assert "NEW.source_quote_id := NULL" in migration
    assert "trg_confirm_correlated_billing_operations" in migration
    assert "operation.quote_id = NEW.source_quote_id" in migration
    assert "operation.target_plan_id = NEW.plan_id" in migration
    assert "operation.target_seat_quantity = NEW.seat_quantity" in migration
    assert "operation.baseline_source_revision IS NOT NULL" in migration
    assert "operation.baseline_source_revision < NEW.source_revision" in migration
    assert "COALESCE(operation.baseline_source_revision" not in migration
    assert "'submitted', 'failed'" not in migration
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS organization_billing_operations_quote_idx" in migration
    )
    assert "organization_billing_operations_quote_shape" in migration
    assert "source_quote_id requires entitlement schema_version 1.1 or newer" in migration


def test_webhook_before_response_race_has_explicit_service_role_reconciliation() -> None:
    migration = _lifecycle_migration_sql()

    assert "reconcile_billing_operation_from_entitlement" in migration
    assert "entitlement.source_quote_id = operation.quote_id" in migration
    assert (
        "REVOKE ALL ON FUNCTION public.reconcile_billing_operation_from_entitlement(text, text)"
        in migration
    )
    assert (
        "GRANT EXECUTE ON FUNCTION public.reconcile_billing_operation_from_entitlement(text, text)"
        in migration
    )

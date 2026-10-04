"""Legacy scope-bound ref snapshots shared by Git advertise/receive paths.

This is not the native repository authority or an atomic publication service.
"""

def scope_named_refs(repo, scope_path: str, *, strict: bool = False) -> dict[str, str]:
    from src.version_engine.infrastructure.supabase.version_ref_repository import VersionRefStore

    project_id = getattr(repo, "_project_id", "") or ""
    if not project_id:
        if strict:
            raise RuntimeError("Git ref snapshot requires a Project identity")
        return {}
    rows = VersionRefStore().list_refs(project_id, scope_path, strict=strict)
    return {
        row["ref_name"]: row["commit_id"]
        for row in rows
        if row.get("ref_name") and row.get("commit_id")
    }

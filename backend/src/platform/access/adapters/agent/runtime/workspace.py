"""Workspace identity and policy, independent of provider and Git object storage."""


def allowed(path, policy):
    prefix = policy["path_prefix"]
    return (not prefix or path == prefix or path.startswith(prefix + "/")) and not any(
        path == excluded or path.startswith(excluded + "/") for excluded in policy["excludes"]
    )


def require_full_project(run):
    policy = run["policy"]
    if (
        run.get("scope_id")
        or policy["path_prefix"]
        or policy["excludes"]
        or not policy["materialize"]
    ):
        raise ValueError("Agent Git requires an unrestricted Project-root view")


def workspace_state(run, base):
    require_full_project(run)
    target = base.get("target_ref")
    if not isinstance(target, str) or not target.startswith("refs/heads/"):
        raise ValueError("Agent Git requires a selected cloud branch")
    return {
        "version": 2,
        "pi_version": "0.85.1",
        "entries": None,
        "leaf_id": None,
        "base": base,
        "reason": "prepared",
        "recovery": None,
        "workspace": {
            "object_format": base["object_format"],
            "target_ref": target,
            "base_oid": base["expected_oid"],
            "readonly": run["policy"]["readonly"],
        },
    }

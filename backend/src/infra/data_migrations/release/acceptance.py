"""Bounded authenticated acceptance on a newly created, owned test project."""

from __future__ import annotations

import time
from uuid import uuid4

import httpx


def accept(config: dict) -> dict:
    with httpx.Client(
        base_url=config["api_url"].rstrip("/"), timeout=45, trust_env=False
    ) as client:

        def call(method, path, **kwargs):
            response = client.request(method, "/api/v1" + path, **kwargs)
            if response.is_error:
                raise RuntimeError(f"Release acceptance HTTP {response.status_code}")
            body = response.json()
            return body.get("data", body)

        login = call(
            "POST", "/auth/login", json={"email": config["email"], "password": config["password"]}
        )
        client.headers["Authorization"] = "Bearer " + login["access_token"]
        client.headers["X-PuppyOne-Repository-Contract"] = "2"
        project = call(
            "POST",
            "/projects/",
            headers={"Idempotency-Key": str(uuid4())},
            json={"name": "Release acceptance " + uuid4().hex[:8], "org_id": config["org_id"]},
        )
        project_id = project["id"]
        run_id = None
        terminal = False
        try:
            nonce = "release-" + uuid4().hex
            written = call(
                "POST",
                f"/content/{project_id}/write",
                json={"path": "release-check.md", "content": nonce, "node_type": "markdown"},
            )
            if not written.get("commit_id"):
                raise RuntimeError("Acceptance write produced no version")
            body = call("GET", f"/content/{project_id}/cat", params={"path": "release-check.md"})
            if body.get("content_text") != nonce:
                raise RuntimeError("Acceptance read differs from write")
            started = time.monotonic()
            run = call(
                "POST",
                "/agents/runs",
                json={
                    "project_id": project_id,
                    "request_id": str(uuid4()),
                    "prompt": "Read release-check.md and reply with its exact content. Do not modify any files.",
                },
            )
            run_id = run["id"]
            while time.monotonic() - started < 300:
                snapshot = call("GET", "/agents/runs/" + run_id)
                for tool in snapshot.get("tools", []):
                    if tool["state"] == "waiting":
                        call(
                            "POST",
                            f"/agents/runs/{run_id}/approvals/{tool['call_id']}",
                            json={"decision_id": str(uuid4()), "allow": False},
                        )
                        raise RuntimeError("Read-only Agent acceptance requested a mutation")
                if snapshot["state"] == "succeeded":
                    terminal = True
                    if nonce not in snapshot.get("snapshot", {}).get("text", ""):
                        raise RuntimeError("Agent did not read the stored file")
                    return {
                        "authenticated_read_write": True,
                        "git_commit_created": True,
                        "agent_reply": True,
                        "agent_seconds": round(time.monotonic() - started, 3),
                    }
                if snapshot["state"] in {"failed", "stopped", "conflict", "outcome_unknown"}:
                    terminal = True
                    raise RuntimeError("Agent acceptance did not succeed")
                time.sleep(2)
            raise TimeoutError("Agent acceptance timed out")
        finally:
            try:
                if run_id and not terminal:
                    call("POST", "/agents/runs/" + run_id + "/stop")
            finally:
                call("DELETE", "/projects/" + project_id)

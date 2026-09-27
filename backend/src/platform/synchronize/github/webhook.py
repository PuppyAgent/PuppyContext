"""GitHub webhook receiver.

Validates the ``X-Hub-Signature-256`` HMAC against the integration's
``webhook_secret``, then enqueues an import job. We never run the
import synchronously — GitHub gives webhook receivers a 5-second
budget, and a real branch import involves N+1 GitHub API round-trips
plus an S3 upload pass; missing the budget makes GitHub mark the
delivery as failed and start retrying, which we then dedupe via
``github_sync_log``. Cleaner: 200 fast, run async.

References
----------
* GitHub webhook docs: https://docs.github.com/en/webhooks
* Signature verification: HMAC-SHA256 hex of the raw request body
  using ``webhook_secret`` as the key.
"""
from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Optional

from src.platform.synchronize.github.repository import GithubSyncRepository
from src.utils.logger import log_error, log_info, log_warning


class WebhookRejection(Exception):
    """Raised when a webhook delivery is rejected (signature mismatch,
    no integration found, unsupported event, etc.). HTTP layer turns
    this into 4xx with the supplied message.
    """

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def verify_signature(secret: str, raw_body: bytes, signature_header: str) -> bool:
    """Constant-time HMAC verify of ``X-Hub-Signature-256``.

    Header format is ``sha256=<hex>``. Returns False on any malformed
    input — the caller should refuse.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = signature_header[len("sha256="):].strip()
    if not expected:
        return False
    mac = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
    return hmac.compare_digest(mac, expected)


async def handle_webhook(
    raw_body: bytes,
    headers: dict,
    json_payload: dict,
) -> dict:
    """Top-level webhook entry. Returns a small ack dict that the
    router serialises as JSON.

    Steps:
        1. Parse the event type. We only act on ``push`` events.
        2. Look up the integration by ``(owner, repo_name)``. If none
           is bound, we 200 the delivery to keep GitHub happy but log
           the orphan for ops to review.
        3. Verify HMAC. Reject 401 on mismatch.
        4. If the push was for the wrong branch, ack-and-skip.
        5. Idempotency on ``git_sha``: if already imported, ack.
        6. Enqueue the import onto the durable ``imports`` worker queue
           (``execute_github_import``) and ack — the import runs out of
           process, surviving API deploys/crashes.
    """
    event_type = headers.get("x-github-event", "").lower()
    delivery_id = headers.get("x-github-delivery", "?")
    log_info(f"[GithubWebhook] event={event_type} delivery={delivery_id}")

    if event_type == "ping":
        return {"status": "ok", "event": "ping"}

    if event_type != "push":
        # Silently accept other events so GitHub stops retrying. Logs
        # so we know to add handling later.
        log_info(f"[GithubWebhook] ignoring unsupported event={event_type}")
        return {"status": "ignored", "event": event_type}

    repo_obj = json_payload.get("repository") or {}
    owner = (repo_obj.get("owner") or {}).get("login")
    repo_name = repo_obj.get("name")
    pushed_ref = json_payload.get("ref", "")  # e.g. "refs/heads/main"
    pushed_sha = json_payload.get("after")

    if not owner or not repo_name:
        raise WebhookRejection(400, "missing repository.owner.login / repository.name")

    pushed_branch = pushed_ref.removeprefix("refs/heads/") if pushed_ref else ""
    binding_repo = GithubSyncRepository()
    rows = await binding_repo.find_by_repo(owner, repo_name)
    if not rows:
        log_warning(
            f"[GithubWebhook] no integration bound to {owner}/{repo_name}; "
            f"acking delivery"
        )
        return {"status": "no_integration", "delivery_id": delivery_id}

    # One repo can be bound to multiple PuppyOne projects (different
    # users / orgs). Handle each independently.
    results: list[dict] = []
    for binding in rows:
        result = await _maybe_dispatch(binding, pushed_branch, pushed_sha,
                                       raw_body, headers)
        results.append(result)
    return {"status": "ok", "delivery_id": delivery_id, "results": results}


async def _maybe_dispatch(
    binding: dict, pushed_branch: str, pushed_sha: Optional[str],
    raw_body: bytes, headers: dict,
) -> dict:
    """Validate signature + branch + idempotency for one integration,
    then schedule the import job."""
    binding_id = binding["id"]
    secret = binding.get("webhook_secret")

    if not secret:
        log_warning(
            f"[GithubWebhook] integration={binding_id} has no webhook_secret; "
            f"refusing delivery"
        )
        return {"integration_id": binding_id, "status": "skipped",
                "reason": "no_webhook_secret"}

    sig_header = headers.get("x-hub-signature-256", "")
    if not verify_signature(secret, raw_body, sig_header):
        log_error(f"[GithubWebhook] HMAC mismatch for integration={binding_id}")
        raise WebhookRejection(401, "signature mismatch")

    if binding.get("default_branch") and pushed_branch != binding["default_branch"]:
        return {"integration_id": binding_id, "status": "skipped",
                "reason": f"branch_mismatch ({pushed_branch} vs {binding['default_branch']})"}

    if not binding.get("auto_import"):
        return {"integration_id": binding_id, "status": "skipped",
                "reason": "auto_import_disabled"}

    if pushed_sha and binding.get("last_imported_sha") == pushed_sha:
        return {"integration_id": binding_id, "status": "skipped",
                "reason": "already_imported"}

    # Enqueue the import onto the durable imports worker queue (out of the API
    # process). The webhook only validates + enqueues + acks, staying well
    # inside GitHub's 5s budget; the worker survives API deploys/crashes. The
    # ARQ _job_id dedups a redelivered webhook for the same push, and
    # import_branch's own has_successful_sha check is a second guard.
    from src.platform.synchronize.github.arq_client import get_github_sync_arq_client

    dedup_key = f"gh-import:{binding_id}:{pushed_sha}" if pushed_sha else None
    try:
        worker_job_id = await get_github_sync_arq_client().enqueue_pull(
            binding_id,
            branch=pushed_branch,
            force=False,
            triggered_by="webhook",
            dedup_key=dedup_key,
        )
    except Exception as exc:  # noqa: BLE001
        # Ack the delivery (GitHub redelivers on non-2xx); surface to ops.
        log_error(
            f"[GithubWebhook] failed to enqueue import integration={binding_id}: {exc!r}"
        )
        return {"integration_id": binding_id, "status": "error",
                "reason": "enqueue_failed"}

    if worker_job_id is None:
        # ARQ deduped against an in-flight job for the same push.
        return {"integration_id": binding_id, "status": "queued",
                "reason": "already_queued"}
    return {"integration_id": binding_id, "status": "queued",
            "worker_job_id": worker_job_id}

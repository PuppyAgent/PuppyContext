"""Immutable, identity-bound admission result. No lazy reads or client handles."""

import json
from dataclasses import dataclass

from src.platform.authorization.models import ProjectGrant


@dataclass(frozen=True, slots=True)
class RunContext:
    user_id: str
    project_id: str
    agent_id: str
    grant: ProjectGrant
    policy_json: str
    receipt_json: str = "null"

    @property
    def receipt(self) -> dict | None:
        return json.loads(self.receipt_json)

    @property
    def policy(self) -> dict:
        # A fresh decoded value prevents a consumer mutating another operation's
        # context. The immutable representation is also safe across threads.
        return json.loads(self.policy_json)

"""Internal capability for a leased, unpublished native Project initializer."""

from dataclasses import dataclass


@dataclass(frozen=True)
class InitializationGrant:
    project_id: str
    user_id: str
    lease: object

    def actor(self):
        if (
            not self.lease.is_active
            or self.lease.project_id != self.project_id
            or self.lease.initialization_actor != self.user_id
            or not self.lease.initialization_operation_key
        ):
            raise PermissionError("initialization lease is unavailable")
        return "initialize:" + self.lease.lease_id

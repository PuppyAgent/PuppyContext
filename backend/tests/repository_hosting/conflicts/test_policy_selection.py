import pytest

from src.version_engine.domain.conflicts import ConflictPolicyConfig, ConflictPolicyRule
from src.version_engine.write_engine.conflict_policy import select_conflict_policy

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize(
    "channel,path,wanted",
    [
        ("agent", "file.txt", "manual_review"),
        ("papi", "package.lock", "reject"),
        ("papi", "file.txt", "last_write_wins"),
    ],
)
def test_first_matching_policy_rule_uses_channel_then_glob(channel, path, wanted):
    config = ConflictPolicyConfig(
        rules=[
            ConflictPolicyRule(policy="manual_review", source_channel="agent"),
            ConflictPolicyRule(policy="reject", path_glob="*.lock"),
            ConflictPolicyRule(policy="last_write_wins", path_glob="*.txt"),
        ]
    )
    result = select_conflict_policy(
        config=config, source_channel=channel, actor="user:test", paths=[path]
    )
    assert result.policy == wanted

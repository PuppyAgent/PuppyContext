"""Shared stock Git conformance recipes for native S3 transport."""

from tests.repository_hosting.harness.history_workflows import HISTORY_WORKFLOWS
from tests.repository_hosting.harness.network_workflows import NETWORK_WORKFLOWS
from tests.repository_hosting.harness.ref_workflows import REF_WORKFLOWS

WORKFLOWS = [*HISTORY_WORKFLOWS, *REF_WORKFLOWS, *NETWORK_WORKFLOWS]

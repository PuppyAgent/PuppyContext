"""Railway boundary for an explicitly configured environment and service inventory.

No secret enumeration, automatic service discovery, or latest-branch deployment.
The coordinator owns deployment; GitHub autodeploy must be disabled while the
repository connection remains attached for exact-commit deployments.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx


class Railway:
    STOPPED = frozenset({"REMOVED", "FAILED", "CRASHED", "SKIPPED"})

    def __init__(
        self,
        config: dict,
        token: str,
        *,
        client=None,
        sleep: Callable = time.sleep,
        clock: Callable = time.monotonic,
    ):
        self.config, self.sleep, self.clock = config, sleep, clock
        self.client = client or httpx.Client(
            base_url="https://backboard.railway.com/graphql/v2",
            headers={"Project-Access-Token": token},
            timeout=30,
            trust_env=False,
        )

    def query(self, query: str, variables: dict) -> dict:
        response = self.client.post("", json={"query": query, "variables": variables})
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise RuntimeError("Railway release request failed")
        return body["data"]

    def preflight(self) -> dict:
        actual = {}
        for role, service in self.config["services"].items():
            data = self.query(
                "query($p:String!,$e:String!,$s:String!){"
                "serviceInstanceAutoDeployStatus(projectId:$p,environmentId:$e,serviceId:$s){enabled}"
                "serviceInstance(environmentId:$e,serviceId:$s){source{repo} latestDeployment{id status meta}}}",
                {
                    "p": self.config["project_id"],
                    "e": self.config["environment_id"],
                    "s": service["id"],
                },
            )
            if data["serviceInstanceAutoDeployStatus"]["enabled"] is not False:
                raise ValueError(
                    "Independent Railway autodeploy must be disabled before coordinator activation"
                )
            instance = data["serviceInstance"]
            if (instance.get("source") or {}).get("repo") != "puppyone-ai/puppyone-cloud":
                raise ValueError(
                    "Release service must remain connected to the candidate repository"
                )
            deployment = instance["latestDeployment"]
            if deployment and deployment["status"] not in self.STOPPED | {"SUCCESS"}:
                raise ValueError("An unmanaged deployment is already in progress")
            actual[role] = deployment
        return actual

    def deployments(self, service: str) -> list[dict]:
        # Enumerate all live versions, including an old overlapping deployment.
        records, cursor = [], None
        while True:
            data = self.query(
                "query($i:DeploymentListInput!,$a:String){deployments(input:$i,first:100,after:$a){"
                "edges{node{id status meta}} pageInfo{hasNextPage endCursor}}}",
                {
                    "i": {
                        "environmentId": self.config["environment_id"],
                        "serviceId": service,
                        "status": {"notIn": sorted(self.STOPPED)},
                    },
                    "a": cursor,
                },
            )["deployments"]
            records.extend(
                edge["node"] for edge in data["edges"] if edge["node"]["status"] not in self.STOPPED
            )
            if not data["pageInfo"]["hasNextPage"]:
                return records
            cursor = data["pageInfo"]["endCursor"]

    def stop(self, roles: list[str]) -> dict:
        stopped = []
        for role in roles:
            service = self.config["services"][role]["id"]
            for deployment in self.deployments(service):
                if not self.query(
                    "mutation($id:String!){deploymentStop(id:$id)}", {"id": deployment["id"]}
                )["deploymentStop"]:
                    raise RuntimeError("Railway did not accept the stop request")
                stopped.append(deployment["id"])
        deadline = self.clock() + self.config.get("stop_timeout_seconds", 600)
        while self.clock() < deadline:
            if all(not self.deployments(self.config["services"][role]["id"]) for role in roles):
                return {"deployment_ids": stopped, "provider_confirmed_stopped": True}
            self.sleep(2)
        raise TimeoutError("Old Railway deployments did not stop")

    def deploy(self, source: str) -> dict:
        expected = {}
        for role, service in self.config["services"].items():
            data = self.query(
                "query($e:String!,$s:String!){serviceInstance(environmentId:$e,serviceId:$s){latestDeployment{id status meta}}}",
                {"e": self.config["environment_id"], "s": service["id"]},
            )["serviceInstance"]["latestDeployment"]
            if (
                data
                and data["status"] == "SUCCESS"
                and (data.get("meta") or {}).get("commitHash") == source
            ):
                expected[role] = data["id"]
                continue
            deployment_id = self.query(
                "mutation($e:String!,$s:String!,$sha:String!){serviceInstanceDeployV2(environmentId:$e,serviceId:$s,commitSha:$sha)}",
                {"e": self.config["environment_id"], "s": service["id"], "sha": source},
            )["serviceInstanceDeployV2"]
            if not isinstance(deployment_id, str) or not deployment_id:
                raise RuntimeError("Railway did not accept the candidate deployment")
            expected[role] = deployment_id
        deadline = self.clock() + self.config.get("deploy_timeout_seconds", 1800)
        while self.clock() < deadline:
            complete = True
            for deployment_id in expected.values():
                deployment = self.query(
                    "query($id:String!){deployment(id:$id){id status meta}}",
                    {"id": deployment_id},
                )["deployment"]
                if not deployment:
                    complete = False
                elif deployment["status"] in self.STOPPED:
                    raise RuntimeError("Candidate deployment failed")
                elif deployment["status"] != "SUCCESS":
                    complete = False
                elif (
                    deployment["id"] != deployment_id
                    or (deployment.get("meta") or {}).get("commitHash") != source
                ):
                    raise RuntimeError(
                        "Candidate deployment source differs from the requested commit"
                    )
            if complete:
                return expected
            self.sleep(3)
        raise TimeoutError("Candidate deployments did not become ready")

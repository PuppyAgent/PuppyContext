"""Railway boundary for an explicitly configured environment and service inventory.

No secret enumeration, automatic service discovery, or latest-branch deployment.
The coordinator owns deployment; independent GitHub triggers must be disabled.
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
                "deploymentTriggers(projectId:$p,environmentId:$e,serviceId:$s){edges{node{id}}}"
                "serviceInstance(environmentId:$e,serviceId:$s){latestDeployment{id status meta}}}",
                {
                    "p": self.config["project_id"],
                    "e": self.config["environment_id"],
                    "s": service["id"],
                },
            )
            if data["deploymentTriggers"]["edges"]:
                raise ValueError(
                    "Independent Railway autodeploy must be disabled before coordinator activation"
                )
            deployment = data["serviceInstance"]["latestDeployment"]
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
            ok = self.query(
                "mutation($e:String!,$s:String!,$sha:String!){serviceInstanceDeploy(environmentId:$e,serviceId:$s,commitSha:$sha,latestCommit:false)}",
                {"e": self.config["environment_id"], "s": service["id"], "sha": source},
            )["serviceInstanceDeploy"]
            if not ok:
                raise RuntimeError("Railway did not accept the candidate deployment")
        deadline = self.clock() + self.config.get("deploy_timeout_seconds", 1800)
        while self.clock() < deadline:
            complete = True
            for role, service in self.config["services"].items():
                deployment = self.query(
                    "query($e:String!,$s:String!){serviceInstance(environmentId:$e,serviceId:$s){latestDeployment{id status meta}}}",
                    {"e": self.config["environment_id"], "s": service["id"]},
                )["serviceInstance"]["latestDeployment"]
                if not deployment or (deployment.get("meta") or {}).get("commitHash") != source:
                    complete = False
                elif deployment["status"] in self.STOPPED:
                    raise RuntimeError("Candidate deployment failed")
                elif deployment["status"] != "SUCCESS":
                    complete = False
                else:
                    expected[role] = deployment["id"]
            if complete:
                return expected
            self.sleep(3)
        raise TimeoutError("Candidate deployments did not become ready")

"""Private durable release state, serialized by one database-wide advisory lock."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..database import PsqlClient


class Journal:
    def __init__(self, root: Path, db: PsqlClient):
        self.root, self.db = root, db
        self.source = ""

    def begin(self, source: str, checksum: str) -> bool:
        if not re.fullmatch(r"[a-f0-9]{40}", source):
            raise ValueError("A complete immutable source SHA is required")
        self.db.command(
            ["-q", "-v", "ON_ERROR_STOP=1"],
            input_text=(self.root / "supabase/releases/control.sql").read_text(),
        )
        rows = json.loads(
            self.db.scalar(
                "SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY (r.source_sha=:'source') DESC),'[]') FROM ("
                "(SELECT source_sha,plan_checksum,state FROM puppyone_release.runs WHERE source_sha=:'source') "
                "UNION (SELECT source_sha,plan_checksum,state FROM puppyone_release.runs "
                "WHERE state<>'superseded' ORDER BY started_at DESC LIMIT 1)) r",
                variables={"source": source},
            )
        )
        already_accepted = False
        for row in rows:
            if row["source_sha"] == source:
                if row["plan_checksum"] != checksum:
                    raise ValueError("Release plan changed for an existing source SHA")
                if row["state"] == "accepted":
                    already_accepted = True
                if row["state"] == "superseded":
                    raise ValueError("Cannot resume a superseded release")
            elif row["state"] != "superseded":
                # This admits a forward fix after a failed release, but never an
                # old queued deployment or a sibling branch overwriting new data.
                result = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", row["source_sha"], source],
                    cwd=self.root,
                    capture_output=True,
                    check=False,
                )
                if result.returncode:
                    raise ValueError("Release does not descend from the target's recorded version")
        if already_accepted:
            return False
        self.source = source
        self.db.scalar(
            "BEGIN; UPDATE puppyone_release.runs SET state='superseded',updated_at=now() "
            "WHERE source_sha<>:'source' AND state IN ('running','failed'); "
            "INSERT INTO puppyone_release.runs(source_sha,plan_checksum,state) VALUES(:'source',:'checksum','running') "
            "ON CONFLICT(source_sha) DO UPDATE SET state='running',updated_at=now(); COMMIT;",
            variables={"source": source, "checksum": checksum},
        )
        return True

    def checkpoint(self, phase: str, evidence: dict | None = None) -> None:
        self.db.scalar(
            "UPDATE puppyone_release.runs SET phase=:'phase', updated_at=now(), "
            "evidence=evidence || jsonb_build_object(:'phase',:'evidence'::jsonb) WHERE source_sha=:'source' AND state='running';",
            variables={
                "source": self.source,
                "phase": phase,
                "evidence": json.dumps(evidence or {"completed": True}),
            },
        )

    def evidence(self, phase: str) -> dict | None:
        value = self.db.scalar(
            "SELECT evidence->:'phase' FROM puppyone_release.runs WHERE source_sha=:'source'",
            variables={"source": self.source, "phase": phase},
        )
        return json.loads(value) if value else None

    def finish(self, state: str, phase: str) -> None:
        self.db.scalar(
            "UPDATE puppyone_release.runs SET state=:'state',phase=:'phase',updated_at=now() WHERE source_sha=:'source'",
            variables={"source": self.source, "state": state, "phase": phase},
        )

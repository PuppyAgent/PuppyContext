"""Small libpq/psql boundary for migration coordination and receipts."""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import subprocess
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import ExecutionError, MigrationBusyError, PrerequisiteError


class PsqlClient:
    """Execute trusted repository SQL without placing credentials in argv."""

    def __init__(
        self,
        database_url: str,
        *,
        executable: str = "psql",
        base_environment: dict[str, str] | None = None,
    ) -> None:
        database_url = database_url.strip()
        if not database_url:
            raise PrerequisiteError("DATA_MIGRATION_DATABASE_URL is required")
        resolved = shutil.which(executable)
        if resolved is None:
            raise PrerequisiteError("psql is required to run data migrations")
        self.executable = resolved
        self._database_url = database_url
        self.environment = dict(os.environ if base_environment is None else base_environment)
        self._bind_libpq_environment(database_url)
        # Supabase session poolers do not reliably forward startup PGOPTIONS.
        # The official expiring login is a member of postgres, but starts with
        # its own restricted role until explicitly switched in the session.
        self.session_prefix = (
            "SET SESSION ROLE postgres;\n"
            if self.environment.get("PGUSER", "").startswith("cli_login_")
            else ""
        )

    def _bind_libpq_environment(self, database_url: str) -> None:
        """Translate a PostgreSQL URI into discrete libpq environment fields.

        `PGDATABASE` is a database *name*, not a portable connection-URI slot.
        Splitting the URI keeps the password out of process arguments while
        preserving the same behavior for every `psql` subprocess.
        """

        database = urlsplit(database_url)
        if database.scheme not in {"postgres", "postgresql"} or not database.hostname:
            raise PrerequisiteError("DATA_MIGRATION_DATABASE_URL must be a PostgreSQL URI")
        try:
            port = database.port or 5432
        except ValueError as error:
            raise PrerequisiteError("database URL has an invalid port") from error
        database_name = unquote(database.path.removeprefix("/"))
        if not database_name:
            raise PrerequisiteError("database URL must name a database")

        for name in (
            "PGSERVICE",
            "PGSERVICEFILE",
            "PGHOSTADDR",
            "PGUSER",
            "PGPASSWORD",
            "PGSSLMODE",
            "PGAPPNAME",
            "PGCONNECT_TIMEOUT",
        ):
            self.environment.pop(name, None)
        self.environment.update(
            {
                "PGHOST": database.hostname,
                "PGPORT": str(port),
                "PGDATABASE": database_name,
            }
        )
        if database.username:
            self.environment["PGUSER"] = unquote(database.username)
        if database.password:
            self.environment["PGPASSWORD"] = unquote(database.password)

        query = parse_qs(database.query, keep_blank_values=False)
        query_environment = {
            "sslmode": "PGSSLMODE",
            "application_name": "PGAPPNAME",
            "connect_timeout": "PGCONNECT_TIMEOUT",
        }
        for parameter, environment_name in query_environment.items():
            values = query.get(parameter)
            if values:
                self.environment[environment_name] = values[-1]

    def assert_supabase_target(self, *, project_ref: str, api_url: str) -> None:
        """Prove that hosted API and PostgreSQL credentials target one project.

        Legacy Python jobs use PostgREST for row work and PostgreSQL for locks,
        verification, and receipts. A split environment configuration must not
        mutate one project and publish success in another.
        """

        project_ref = project_ref.strip().lower()
        if not project_ref or not api_url.strip():
            raise PrerequisiteError(
                "SUPABASE_PROJECT_ID and SUPABASE_URL are required together "
                "for hosted target verification"
            )

        api = urlsplit(api_url)
        expected_api_host = f"{project_ref}.supabase.co"
        if api.scheme != "https" or (api.hostname or "").lower() != expected_api_host:
            raise PrerequisiteError("SUPABASE_URL does not match the protected Supabase project")

        database = urlsplit(self._database_url)
        database_host = (database.hostname or "").lower()
        database_user = unquote(database.username or "").lower()
        try:
            database_port = database.port or 5432
        except ValueError as error:
            raise PrerequisiteError("database URL has an invalid port") from error
        direct_match = database_host == f"db.{project_ref}.supabase.co" and database_port == 5432
        pooler_match = (
            database_host.endswith(".pooler.supabase.com")
            and (
                database_user == f"postgres.{project_ref}"
                or re.fullmatch(r"cli_login_[a-z0-9_]+\." + re.escape(project_ref), database_user)
            )
            and database_port == 5432
        )
        if not direct_match and not pooler_match:
            raise PrerequisiteError(
                "database URL cannot be proven to target the protected Supabase project; "
                "use its direct URL or session-pooler URL"
            )

    def command(
        self,
        arguments: list[str],
        *,
        timeout: int = 60,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if self.session_prefix:
            if input_text is not None:
                input_text = self.session_prefix + input_text
            else:
                arguments = ["-c", self.session_prefix, *arguments]
        try:
            result = subprocess.run(
                [self.executable, "-X", *arguments],
                check=False,
                capture_output=True,
                text=True,
                env=self.environment,
                input=input_text,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise ExecutionError(f"psql timed out after {timeout} seconds") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ExecutionError(detail or f"psql exited with {result.returncode}")
        return result

    def scalar(self, sql: str, *, variables: dict[str, str] | None = None) -> str:
        arguments = ["-qAt", "-v", "ON_ERROR_STOP=1"]
        for name, value in sorted((variables or {}).items()):
            arguments.extend(["-v", f"{name}={value}"])
        # psql variable interpolation is a client-side script feature. In
        # particular, variables in SQL supplied through `-c` are not expanded.
        # Feeding the script over stdin preserves `:'name'` literal quoting and
        # keeps dynamic values out of both SQL string construction and argv.
        return self.command(arguments, input_text=f"{sql.rstrip()}\n").stdout.strip()

    def applied_schema_versions(self) -> set[str]:
        output = self.scalar(
            "SELECT version::text FROM supabase_migrations.schema_migrations ORDER BY version"
        )
        return {line.strip() for line in output.splitlines() if line.strip()}

    def receipt(self, migration_id: str) -> dict[str, Any] | None:
        output = self.scalar(
            "SELECT COALESCE(summary, '{}'::jsonb)::text "
            "FROM public.migration_log WHERE name = :'migration_id'",
            variables={"migration_id": migration_id},
        )
        if not output:
            return None
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise ExecutionError(f"invalid migration receipt for {migration_id}") from error
        if not isinstance(value, dict):
            raise ExecutionError(f"invalid migration receipt for {migration_id}")
        return value

    def verify(self, verify_path: Path, *, timeout: int) -> None:
        verification = verify_path.read_text(encoding="utf-8")
        script = (
            "BEGIN READ ONLY;\n"
            "SET LOCAL lock_timeout = '5s';\n"
            "SET LOCAL statement_timeout = :'statement_timeout_ms';\n"
            f"{verification.rstrip()}\n"
            "ROLLBACK;\n"
        )
        self.command(
            [
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"statement_timeout_ms={timeout * 1000}ms",
            ],
            timeout=timeout,
            input_text=script,
        )

    def record_receipt(
        self,
        *,
        migration_id: str,
        checksum: str,
        source_sha: str,
        legacy: bool,
    ) -> None:
        summary = json.dumps(
            {
                "artifact_checksum": checksum,
                "source_sha": source_sha,
                "runner_version": 1,
                "verified": True,
                "legacy": legacy,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        script = (
            "INSERT INTO public.migration_log(name, applied_at, summary) "
            "VALUES (:'migration_id', now(), :'summary'::jsonb) "
            "ON CONFLICT (name) DO NOTHING;\n"
        )
        self.command(
            [
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"migration_id={migration_id}",
                "-v",
                f"summary={summary}",
            ],
            input_text=script,
        )

    def run_sql_transaction(
        self,
        *,
        migration_id: str,
        entrypoint_path: Path,
        verify_path: Path,
        checksum: str,
        source_sha: str,
        legacy: bool,
        timeout: int,
    ) -> None:
        summary = json.dumps(
            {
                "artifact_checksum": checksum,
                "source_sha": source_sha,
                "runner_version": 1,
                "verified": True,
                "legacy": legacy,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        entrypoint = entrypoint_path.read_text(encoding="utf-8")
        verification = verify_path.read_text(encoding="utf-8")
        script = (
            "BEGIN;\n"
            "SET LOCAL lock_timeout = '5s';\n"
            "SET LOCAL statement_timeout = :'statement_timeout_ms';\n"
            "SELECT set_config("
            "'puppyone.data_migration_id', :'migration_id', true);\n"
            "DO $puppyone$ BEGIN "
            "IF NOT pg_try_advisory_xact_lock(hashtextextended("
            "current_setting('puppyone.data_migration_id'), 0)) THEN "
            "RAISE EXCEPTION 'DATA_MIGRATION_BUSY:%', "
            "current_setting('puppyone.data_migration_id'); "
            "END IF; END; $puppyone$;\n"
            f"{entrypoint.rstrip()}\n"
            f"{verification.rstrip()}\n"
            "INSERT INTO public.migration_log(name, applied_at, summary) "
            "VALUES (:'migration_id', now(), :'summary'::jsonb) "
            "ON CONFLICT (name) DO NOTHING;\n"
            "COMMIT;\n"
        )
        try:
            self.command(
                [
                    "-q",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-v",
                    f"migration_id={migration_id}",
                    "-v",
                    f"summary={summary}",
                    "-v",
                    f"statement_timeout_ms={timeout * 1000}ms",
                ],
                timeout=timeout,
                input_text=script,
            )
        except ExecutionError as error:
            if "DATA_MIGRATION_BUSY:" in str(error):
                raise MigrationBusyError(
                    f"data migration is already running: {migration_id}"
                ) from error
            raise

    def advisory_lock(self, migration_id: str, *, timeout: int = 30) -> PsqlAdvisoryLock:
        return PsqlAdvisoryLock(self, migration_id, timeout=timeout)


class PsqlAdvisoryLock(AbstractContextManager):
    """Hold a session advisory lock while application-language work runs."""

    def __init__(self, client: PsqlClient, migration_id: str, *, timeout: int) -> None:
        self.client = client
        self.migration_id = migration_id
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self):
        self.process = subprocess.Popen(
            [
                self.client.executable,
                "-X",
                "-qAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"migration_id={self.migration_id}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self.client.environment,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(
            self.client.session_prefix + "SELECT CASE WHEN pg_try_advisory_lock("
            "hashtextextended(:'migration_id', 0)) "
            "THEN 'LOCKED' ELSE 'BUSY' END;\n"
        )
        self.process.stdin.flush()

        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        events = selector.select(self.timeout)
        selector.close()
        if not events:
            self._terminate()
            raise ExecutionError("timed out while acquiring the data migration lock")
        result = self.process.stdout.readline().strip()
        if result == "BUSY":
            self._terminate()
            raise MigrationBusyError(f"data migration is already running: {self.migration_id}")
        if result != "LOCKED":
            detail = self._terminate()
            raise ExecutionError(detail or "failed to acquire data migration lock")
        return self

    def assert_held(self) -> None:
        """Probe the original owning session before another release side effect."""
        if self.process is None or self.process.poll() is not None:
            raise ExecutionError("Release database lock connection was lost")
        try:
            self.process.stdin.write("SELECT 'STILL_LOCKED';\n")
            self.process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if (
                    not selector.select(self.timeout)
                    or self.process.stdout.readline().strip() != "STILL_LOCKED"
                ):
                    raise ExecutionError("Release database lock connection was lost")
        except (BrokenPipeError, OSError) as error:
            raise ExecutionError("Release database lock connection was lost") from error

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.process is None:
            return None
        if self.process.stdin is not None and self.process.poll() is None:
            try:
                self.process.stdin.write(
                    "SELECT pg_advisory_unlock(hashtextextended(:'migration_id', 0));\n\\q\n"
                )
                self.process.stdin.flush()
            except BrokenPipeError:
                pass
        detail = self._terminate()
        if self.process.returncode not in {0, None} and exc_value is None:
            raise ExecutionError(detail or "failed to release data migration lock")
        return None

    def _terminate(self) -> str:
        if self.process is None:
            return ""
        try:
            _, stderr = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _, stderr = self.process.communicate()
        return stderr.strip()

"""Default materializers for first-party SaaS integration providers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from src.provider._base import FetchResult
from src.provider.materializers.base import (
    MaterializationSchema,
    MaterializedOutput,
    SourceMaterializer,
    csv_text,
    ensure_mapping,
    frontmatter,
    parse_datetime,
    relative_path,
    safe_name,
    source_meta,
)
from src.provider.schemas import MaterializationInput


class GmailMaterializer(SourceMaterializer):
    provider = "gmail"
    schema = MaterializationSchema(
        id="puppyone.gmail.thread_markdown",
        version=1,
        label="Gmail threads",
        description="Threads are stored as Markdown with a machine-readable index.",
        preview_paths=(
            "_meta/source.json",
            "index.json",
            "inbox/YYYY/MM/thread_<thread_id>.md",
        ),
    )

    def materialize(self, result: FetchResult, sync: MaterializationInput) -> MaterializedOutput:
        content = ensure_mapping(result.content)
        emails = content.get("emails") if isinstance(content.get("emails"), list) else []
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for email in emails:
            if isinstance(email, dict):
                thread_id = str(email.get("thread_id") or email.get("id") or "thread")
                groups[thread_id].append(email)

        files: dict[str, Any] = {
            "_meta/source.json": source_meta(
                provider=self.provider,
                schema=self.schema,
                result=result,
                content=content,
                sync=sync,
            ),
        }
        index_entries: list[dict[str, Any]] = []

        for thread_id, messages in sorted(groups.items()):
            messages.sort(key=lambda item: str(item.get("date") or ""))
            latest = parse_datetime(messages[-1].get("date"))
            first = messages[0]
            rel = relative_path(
                "inbox",
                latest.strftime("%Y"),
                latest.strftime("%m"),
                f"thread_{safe_name(thread_id, 'thread', 48)}.md",
            )
            files[rel] = self._thread_markdown(thread_id, messages)
            index_entries.append({
                "thread_id": thread_id,
                "path": rel,
                "subject": first.get("subject") or "(No Subject)",
                "from": first.get("from"),
                "message_count": len(messages),
                "latest_date": latest.isoformat(),
                "labels": sorted({
                    label
                    for message in messages
                    for label in (message.get("labels") or [])
                    if isinstance(label, str)
                }),
            })

        files["index.json"] = {
            "provider": self.provider,
            "schema": self.schema.id,
            "schema_version": self.schema.version,
            "account": content.get("account"),
            "query": content.get("query", ""),
            "email_count": content.get("email_count", len(emails)),
            "thread_count": len(index_entries),
            "threads": index_entries,
        }

        return MaterializedOutput(
            files=files,
            summary=result.summary or f"Fetched {len(emails)} Gmail emails",
            primary_path="index.json",
            content_hash=result.content_hash,
        )

    def _thread_markdown(self, thread_id: str, messages: list[dict[str, Any]]) -> str:
        first = messages[0] if messages else {}
        metadata = {
            "source": "gmail",
            "thread_id": thread_id,
            "subject": first.get("subject") or "(No Subject)",
            "message_count": len(messages),
            "latest_date": messages[-1].get("date") if messages else "",
        }
        lines = [frontmatter(metadata), "", f"# {metadata['subject']}", ""]
        for message in messages:
            lines.extend([
                f"## {message.get('date') or ''}",
                "",
                f"- From: {message.get('from') or ''}",
                f"- To: {message.get('to') or ''}",
                f"- Labels: {', '.join(message.get('labels') or [])}",
                f"- Gmail: {message.get('url') or ''}",
                "",
                str(message.get("body") or message.get("snippet") or "").strip(),
                "",
            ])
        return "\n".join(lines).rstrip() + "\n"


class GoogleDocsMaterializer(SourceMaterializer):
    provider = "google_docs"
    schema = MaterializationSchema(
        id="puppyone.google_docs.markdown",
        version=1,
        label="Google Docs documents",
        description="Each Google Doc is stored as Markdown with an index.",
        preview_paths=(
            "_meta/source.json",
            "index.json",
            "documents/<document-title>.md",
        ),
    )

    def materialize(self, result: FetchResult, sync: MaterializationInput) -> MaterializedOutput:
        source = sync.source
        title = str(result.node_name or source.get("resource_name") or "Google Doc")
        if title.endswith(".md"):
            title = title[:-3]
        doc_path = relative_path("documents", f"{safe_name(title, 'document')}.md")
        content = {"source": source}
        files = {
            "_meta/source.json": source_meta(
                provider=self.provider,
                schema=self.schema,
                result=result,
                content=content,
                sync=sync,
                source_name=title,
            ),
            "index.json": {
                "provider": self.provider,
                "schema": self.schema.id,
                "schema_version": self.schema.version,
                "documents": [{
                    "title": title,
                    "path": doc_path,
                    "source_url": source.get("resource_url"),
                    "source_id": source.get("resource_id"),
                }],
            },
            doc_path: str(result.content or ""),
        }
        return MaterializedOutput(
            files=files,
            summary=result.summary,
            primary_path=doc_path,
            content_hash=result.content_hash,
        )


class GoogleSheetsMaterializer(SourceMaterializer):
    provider = "google_sheets"
    schema = MaterializationSchema(
        id="puppyone.google_sheets.workbook",
        version=1,
        label="Google Sheets workbook",
        description="Workbook metadata is stored as JSON; each sheet is stored as CSV plus schema.",
        preview_paths=(
            "_meta/source.json",
            "index.json",
            "spreadsheets/<workbook>/workbook.json",
            "spreadsheets/<workbook>/sheets/<sheet>.csv",
            "spreadsheets/<workbook>/sheets/<sheet>.schema.json",
        ),
    )

    def materialize(self, result: FetchResult, sync: MaterializationInput) -> MaterializedOutput:
        content = ensure_mapping(result.content)
        workbook = safe_name(content.get("spreadsheet_title") or result.node_name or "Workbook", "workbook")
        root = relative_path("spreadsheets", workbook)
        sheets = content.get("sheets") if isinstance(content.get("sheets"), list) else []
        files: dict[str, Any] = {
            "_meta/source.json": source_meta(
                provider=self.provider,
                schema=self.schema,
                result=result,
                content=content,
                sync=sync,
                source_name=content.get("spreadsheet_title"),
            ),
        }
        index_sheets: list[dict[str, Any]] = []
        workbook_payload = {
            key: value
            for key, value in content.items()
            if key != "sheets"
        }

        for idx, sheet in enumerate(sheets):
            if not isinstance(sheet, dict):
                continue
            sheet_name = safe_name(sheet.get("name") or f"Sheet {idx + 1}", f"sheet-{idx + 1}")
            headers = [str(header) for header in (sheet.get("headers") or [])]
            rows = sheet.get("rows") if isinstance(sheet.get("rows"), list) else []
            csv_path = relative_path(root, "sheets", f"{sheet_name}.csv")
            schema_path = relative_path(root, "sheets", f"{sheet_name}.schema.json")
            files[csv_path] = csv_text(headers, [row for row in rows if isinstance(row, dict)])
            files[schema_path] = {
                "name": sheet.get("name") or sheet_name,
                "sheet_id": sheet.get("sheet_id"),
                "headers": headers,
                "row_count": sheet.get("row_count", len(rows)),
                "total_rows_in_source": sheet.get("total_rows_in_source"),
                "truncated": sheet.get("truncated", False),
            }
            index_sheets.append({
                "name": sheet.get("name") or sheet_name,
                "csv_path": csv_path,
                "schema_path": schema_path,
                "row_count": sheet.get("row_count", len(rows)),
            })

        workbook_payload["sheets"] = index_sheets
        files[relative_path(root, "workbook.json")] = workbook_payload
        files["index.json"] = {
            "provider": self.provider,
            "schema": self.schema.id,
            "schema_version": self.schema.version,
            "spreadsheet_id": content.get("spreadsheet_id"),
            "spreadsheet_title": content.get("spreadsheet_title"),
            "workbook_path": relative_path(root, "workbook.json"),
            "sheets": index_sheets,
        }
        return MaterializedOutput(
            files=files,
            summary=result.summary,
            primary_path="index.json",
            content_hash=result.content_hash,
        )


class GoogleDriveMaterializer(SourceMaterializer):
    provider = "google_drive"
    schema = MaterializationSchema(
        id="puppyone.google_drive.manifest",
        version=1,
        label="Google Drive manifest",
        description="Drive folders are stored as a manifest until file export is enabled.",
        preview_paths=(
            "_meta/source.json",
            "index.json",
            "folders.json",
            "files/manifest.json",
        ),
    )

    def materialize(self, result: FetchResult, sync: MaterializationInput) -> MaterializedOutput:
        content = ensure_mapping(result.content)
        files = content.get("files") if isinstance(content.get("files"), list) else []
        manifest = [
            item for item in files if isinstance(item, dict)
        ]
        output = {
            "_meta/source.json": source_meta(
                provider=self.provider,
                schema=self.schema,
                result=result,
                content=content,
                sync=sync,
                source_name=content.get("folder_name"),
            ),
            "index.json": {
                "provider": self.provider,
                "schema": self.schema.id,
                "schema_version": self.schema.version,
                "folder_name": content.get("folder_name"),
                "total_files": content.get("total_files", len(manifest)),
                "manifest_path": "files/manifest.json",
            },
            "folders.json": {
                "name": content.get("folder_name"),
                "source_type": content.get("source_type"),
            },
            "files/manifest.json": {
                "files": manifest,
            },
        }
        return MaterializedOutput(
            files=output,
            summary=result.summary,
            primary_path="index.json",
            content_hash=result.content_hash,
        )


class GoogleCalendarMaterializer(SourceMaterializer):
    provider = "google_calendar"
    schema = MaterializationSchema(
        id="puppyone.google_calendar.daily_events",
        version=1,
        label="Google Calendar events",
        description="Events are grouped into daily Markdown files with a machine-readable index.",
        preview_paths=(
            "_meta/source.json",
            "index.json",
            "events/YYYY/MM/YYYY-MM-DD.md",
        ),
    )

    def materialize(self, result: FetchResult, sync: MaterializationInput) -> MaterializedOutput:
        content = ensure_mapping(result.content)
        events = content.get("events") if isinstance(content.get("events"), list) else []
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            if not isinstance(event, dict):
                continue
            date = parse_datetime(event.get("start")).date().isoformat()
            grouped[date].append(event)

        files: dict[str, Any] = {
            "_meta/source.json": source_meta(
                provider=self.provider,
                schema=self.schema,
                result=result,
                content=content,
                sync=sync,
            ),
        }
        index_days: list[dict[str, Any]] = []
        for day, day_events in sorted(grouped.items()):
            dt = parse_datetime(day)
            path = relative_path("events", dt.strftime("%Y"), dt.strftime("%m"), f"{day}.md")
            day_events.sort(key=lambda item: str(item.get("start") or ""))
            files[path] = self._day_markdown(day, day_events)
            index_days.append({
                "date": day,
                "path": path,
                "event_count": len(day_events),
            })

        files["index.json"] = {
            "provider": self.provider,
            "schema": self.schema.id,
            "schema_version": self.schema.version,
            "account": content.get("account"),
            "calendar_count": content.get("calendar_count"),
            "event_count": content.get("event_count", len(events)),
            "time_range": content.get("time_range"),
            "days": index_days,
        }
        return MaterializedOutput(
            files=files,
            summary=result.summary,
            primary_path="index.json",
            content_hash=result.content_hash,
        )

    def _day_markdown(self, day: str, events: list[dict[str, Any]]) -> str:
        lines = [
            frontmatter({
                "source": "google_calendar",
                "date": day,
                "event_count": len(events),
            }),
            "",
            f"# {day}",
            "",
        ]
        for event in events:
            lines.extend([
                f"## {event.get('summary') or 'Untitled Event'}",
                "",
                f"- Start: {event.get('start') or ''}",
                f"- End: {event.get('end') or ''}",
                f"- Calendar: {event.get('calendar') or ''}",
                f"- Location: {event.get('location') or ''}",
                f"- Link: {event.get('html_link') or ''}",
                "",
                str(event.get("description") or "").strip(),
                "",
            ])
        return "\n".join(lines).rstrip() + "\n"


DEFAULT_MATERIALIZERS: tuple[SourceMaterializer, ...] = (
    GmailMaterializer(),
    GoogleDocsMaterializer(),
    GoogleSheetsMaterializer(),
    GoogleDriveMaterializer(),
    GoogleCalendarMaterializer(),
)

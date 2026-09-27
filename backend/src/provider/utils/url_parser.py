"""
URL Parser - Detect import type and parse URLs for preview.

This module provides:
1. URL type detection (GitHub, Notion, generic URL, etc.)
2. URL parsing with Firecrawl/BeautifulSoup for preview data
3. Safety checks (SSRF prevention)
"""

import re
import json
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.provider.utils.firecrawl_client import FirecrawlClient
from src.utils.logger import log_info, log_error, log_warning


def detect_source_type(url: str) -> str:
    """Detect source type string from URL."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()

    if scheme == "oauth":
        oauth_type = host or parsed.path.strip("/")
        mapping = {"gmail": "gmail", "drive": "google_drive", "google-drive": "google_drive",
                   "calendar": "google_calendar", "google-calendar": "google_calendar"}
        return mapping.get(oauth_type, "url")

    if host in ("github.com", "www.github.com"):
        return "github"
    if host in ("notion.so", "www.notion.so") or "notion.site" in host:
        return "notion"
    if "airtable.com" in host:
        return "airtable"
    if "docs.google.com" in host and "/spreadsheets/" in url:
        return "google_sheets"
    if "docs.google.com" in host and "/document/" in url:
        return "google_docs"
    if "linear.app" in host:
        return "linear"

    return "url"


class UrlParser:
    """
    URL Parser for preview data extraction.

    Supports:
    - Generic URLs (via Firecrawl or BeautifulSoup fallback)
    - JSON endpoints
    - HTML pages
    """

    # Security settings
    MAX_CONTENT_SIZE = 10 * 1024 * 1024  # 10MB
    REQUEST_TIMEOUT = 30.0

    # Blocked internal IP patterns (SSRF prevention)
    BLOCKED_PATTERNS = [
        r"^127\.",
        r"^10\.",
        r"^172\.(1[6-9]|2[0-9]|3[0-1])\.",
        r"^192\.168\.",
        r"^localhost$",
        r"^0\.0\.0\.0$",
    ]

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=self.REQUEST_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=10),
        )
        self.firecrawl_client = FirecrawlClient()

    def _is_safe_url(self, url: str) -> bool:
        """Check URL is safe (prevent SSRF attacks)."""
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if not hostname:
                return False
            for pattern in self.BLOCKED_PATTERNS:
                if re.match(pattern, hostname, re.IGNORECASE):
                    log_warning(f"Blocked URL with internal IP: {url}")
                    return False
            return True
        except Exception as e:
            log_error(f"Error checking URL safety: {e}")
            return False

    async def parse(
        self,
        url: str,
        crawl_options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Parse a URL and return structured preview data.

        Args:
            url: URL to parse
            crawl_options: Optional Firecrawl crawl options for multi-page crawl

        Returns:
            Dict with: data, source_type, title, fields (optional)
        """
        if not self._is_safe_url(url):
            raise ValueError("Internal network addresses are not allowed")

        source_type = detect_source_type(url)

        try:
            log_info(f"Parsing URL: {url}")

            # HTTP request
            response = await self.client.get(url)
            response.raise_for_status()

            # Check content size
            content_length = len(response.content)
            if content_length > self.MAX_CONTENT_SIZE:
                raise ValueError(f"Content size exceeds limit ({content_length} > {self.MAX_CONTENT_SIZE})")

            content_type = response.headers.get("content-type", "").lower()

            # JSON content
            if "application/json" in content_type or url.lower().endswith(".json"):
                return self._parse_json(response.text, url, source_type)

            # HTML content - try Firecrawl first
            if self.firecrawl_client.is_available():
                firecrawl_result = await self._parse_with_firecrawl(url, source_type, crawl_options)
                if firecrawl_result:
                    return firecrawl_result
                log_info(f"Firecrawl failed, falling back to BeautifulSoup for {url}")

            return self._parse_html(response.text, url, source_type)

        except httpx.HTTPStatusError as e:
            log_error(f"HTTP error fetching {url}: {e}")
            raise ValueError(f"HTTP error: {e.response.status_code}")
        except httpx.TimeoutException:
            log_error(f"Timeout fetching {url}")
            raise ValueError("Request timeout")
        except Exception as e:
            log_error(f"Error parsing {url}: {e}")
            raise ValueError(f"Parse error: {e!s}")

    async def _parse_with_firecrawl(
        self,
        url: str,
        source_type: str,
        crawl_options: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Parse URL with Firecrawl (scrape or crawl mode)."""
        try:
            if crawl_options:
                # Multi-page crawl
                log_info(f"Crawling URL with Firecrawl: {url}")
                result = await self.firecrawl_client.crawl_url(
                    url=url,
                    limit=crawl_options.get("limit", 100),
                    max_depth=crawl_options.get("max_depth") or crawl_options.get("maxDepth"),
                    include_paths=crawl_options.get("include_paths") or crawl_options.get("includePaths"),
                    exclude_paths=crawl_options.get("exclude_paths") or crawl_options.get("excludePaths"),
                    crawl_entire_domain=crawl_options.get("crawl_entire_domain", True),
                    sitemap=crawl_options.get("sitemap"),
                    allow_subdomains=crawl_options.get("allow_subdomains", False),
                    allow_external_links=crawl_options.get("allow_external_links", False),
                    delay=crawl_options.get("delay"),
                    formats=["markdown", "html"],
                    only_main_content=True,
                )
                if result:
                    return self._process_crawl_result(result, url, source_type)
            else:
                # Single page scrape
                log_info(f"Scraping URL with Firecrawl: {url}")
                result = await self.firecrawl_client.scrape_url(
                    url,
                    formats=["markdown", "html"],
                    only_main_content=True,
                )
                if result:
                    return self._process_scrape_result(result, url, source_type)

            return None
        except Exception as e:
            log_error(f"Firecrawl error for {url}: {e}")
            return None

    def _process_scrape_result(
        self,
        result: Dict[str, Any],
        url: str,
        source_type: str
    ) -> Dict[str, Any]:
        """Process single-page scrape result from Firecrawl."""
        metadata = result.get("metadata", {})
        title = metadata.get("title") or metadata.get("ogTitle") or urlparse(url).netloc
        markdown_content = result.get("markdown", "")

        if not markdown_content:
            return None

        # Parse markdown into sections
        data = self._markdown_to_sections(markdown_content, title)

        return {
            "data": data,
            "source_type": source_type,
            "title": title,
            "raw_markdown": markdown_content,
        }

    def _process_crawl_result(
        self,
        result: Dict[str, Any],
        url: str,
        source_type: str
    ) -> Dict[str, Any]:
        """Process multi-page crawl result from Firecrawl."""
        crawl_data = result.get("data", [])
        if not crawl_data:
            return None

        all_data = []
        title = f"Crawled from {urlparse(url).netloc}"

        for page in crawl_data:
            page_url = page.get("url") or page.get("metadata", {}).get("sourceURL", url)
            page_title = page.get("metadata", {}).get("title", "Untitled")
            markdown_content = page.get("markdown", "")

            if markdown_content:
                sections = self._markdown_to_sections(markdown_content, page_title, page_url)
                all_data.extend(sections)

        if crawl_data and crawl_data[0].get("metadata", {}).get("title"):
            title = crawl_data[0]["metadata"]["title"]

        return {
            "data": all_data,
            "source_type": source_type,
            "title": title,
            "crawl_info": {
                "total_pages": len(crawl_data),
                "total_sections": len(all_data),
            },
        }

    def _markdown_to_sections(
        self,
        markdown: str,
        default_title: str,
        page_url: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Convert markdown to structured sections."""
        lines = markdown.split("\n")
        sections = []
        current_section = {"title": default_title, "content": ""}
        if page_url:
            current_section["url"] = page_url

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith("#"):
                # Save previous section
                if current_section["content"]:
                    sections.append(current_section.copy())
                # Start new section
                heading_text = re.sub(r"^#+\s*", "", line)
                current_section = {"title": heading_text, "content": ""}
                if page_url:
                    current_section["url"] = page_url
            else:
                if current_section["content"]:
                    current_section["content"] += " "
                current_section["content"] += line

        # Save last section
        if current_section["content"]:
            sections.append(current_section)

        # If no sections found, create one
        if not sections:
            sections = [{"title": default_title, "content": markdown[:500]}]

        return sections

    def _parse_json(self, content: str, url: str, source_type: str) -> Dict[str, Any]:
        """Parse JSON content."""
        try:
            data = json.loads(content)

            if isinstance(data, list):
                return {
                    "data": data,
                    "source_type": source_type,
                    "title": f"JSON data from {urlparse(url).netloc}",
                }

            if isinstance(data, dict):
                # Look for data arrays in common field names
                for key in ["data", "items", "results", "entries", "records"]:
                    if key in data and isinstance(data[key], list):
                        return {
                            "data": data[key],
                            "source_type": source_type,
                            "title": data.get("title") or data.get("name") or f"JSON from {urlparse(url).netloc}",
                        }
                # Wrap dict as single item
                return {
                    "data": [data],
                    "source_type": source_type,
                    "title": data.get("title") or data.get("name") or f"JSON from {urlparse(url).netloc}",
                }

            return {
                "data": [{"value": data}],
                "source_type": source_type,
                "title": f"JSON data from {urlparse(url).netloc}",
            }

        except json.JSONDecodeError as e:
            log_error(f"JSON decode error: {e}")
            raise ValueError("Invalid JSON format")

    def _parse_html(self, content: str, url: str, source_type: str) -> Dict[str, Any]:
        """Parse HTML content with BeautifulSoup."""
        try:
            soup = BeautifulSoup(content, "lxml")
            title_tag = soup.find("title")
            title = title_tag.get_text().strip() if title_tag else urlparse(url).netloc

            # Try tables first
            tables = soup.find_all("table")
            if tables:
                data = self._extract_table_data(tables[0])
                if data:
                    return {"data": data, "source_type": source_type, "title": title}

            # Try lists
            lists = soup.find_all(["ul", "ol"])
            if lists:
                data = self._extract_list_data(lists[0])
                if data:
                    return {"data": data, "source_type": source_type, "title": title}

            # Extract paragraphs
            paragraphs = soup.find_all("p")
            if paragraphs:
                data = [{"content": p.get_text().strip()} for p in paragraphs if p.get_text().strip()]
                if data:
                    return {"data": data, "source_type": source_type, "title": title}

            return {
                "data": [{"title": title, "url": url, "content": "No structured data found"}],
                "source_type": source_type,
                "title": title,
            }

        except Exception as e:
            log_error(f"HTML parsing error: {e}")
            raise ValueError(f"HTML parse error: {e!s}")

    def _extract_table_data(self, table) -> List[Dict[str, Any]]:
        """Extract data from HTML table."""
        data = []
        headers = []

        thead = table.find("thead")
        if thead:
            header_row = thead.find("tr")
            if header_row:
                headers = [th.get_text().strip() for th in header_row.find_all(["th", "td"])]

        if not headers:
            first_row = table.find("tr")
            if first_row:
                headers = [th.get_text().strip() for th in first_row.find_all(["th", "td"])]

        tbody = table.find("tbody") or table
        rows = tbody.find_all("tr")
        start_idx = 1 if not thead and headers else 0

        for row in rows[start_idx:]:
            cells = row.find_all(["td", "th"])
            if cells:
                row_data = {}
                for i, cell in enumerate(cells):
                    header = headers[i] if i < len(headers) else f"column_{i}"
                    row_data[header] = cell.get_text().strip()
                data.append(row_data)

        return data

    def _extract_list_data(self, list_element) -> List[Dict[str, Any]]:
        """Extract data from HTML list."""
        data = []
        items = list_element.find_all("li", recursive=False)
        for item in items:
            text = item.get_text().strip()
            if text:
                data.append({"item": text})
        return data

    async def close(self):
        """Clean up resources."""
        await self.client.aclose()
        await self.firecrawl_client.close()

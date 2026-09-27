"""
Firecrawl client wrapper
Provides async-friendly interface to Firecrawl scraping API
"""

import os
import asyncio
import traceback
from typing import Optional, Dict, Any
from src.utils.logger import log_info, log_warning, log_error


class FirecrawlClient:
    """Async wrapper for Firecrawl scraping service"""

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Firecrawl client

        Args:
            api_key: Firecrawl API key, defaults to FIRECRAWL_API_KEY env var
        """
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY")
        self._client = None

        if not self.api_key:
            log_warning("FIRECRAWL_API_KEY not found, Firecrawl scraping will be disabled")

        # Configure HTTP client with retries for better reliability
        self._http_config = {
            "timeout": 60.0,  # Longer timeout for crawl status checks
            "max_retries": 3,  # Retry on network errors
        }

    def is_available(self) -> bool:
        """Check if Firecrawl is available (API key is set)"""
        return self.api_key is not None

    async def scrape_url(
        self,
        url: str,
        formats: Optional[list[str]] = None,
        only_main_content: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Scrape a URL using Firecrawl

        Args:
            url: URL to scrape
            formats: Output formats (e.g. ["markdown", "html"])
            only_main_content: Extract only main content

        Returns:
            Dictionary with scraped data or None if unavailable/failed
        """
        if not self.is_available():
            log_warning("Firecrawl not available, API key missing")
            return None

        try:
            # Import here to avoid errors when firecrawl-py is not installed
            from firecrawl import AsyncFirecrawl

            # Initialize client if needed
            if self._client is None:
                self._client = AsyncFirecrawl(api_key=self.api_key)

            # Default to markdown for cleaner parsing
            if formats is None:
                formats = ["markdown"]

            log_info(f"Scraping URL with Firecrawl: {url}")

            # Call Firecrawl API
            result = await self._client.scrape(
                url,
                formats=formats,
                only_main_content=only_main_content,
            )

            log_info(f"Successfully scraped URL with Firecrawl: {url}")

            # Convert Document object to dict if needed
            if hasattr(result, "model_dump"):
                return result.model_dump()
            elif hasattr(result, "dict"):
                return result.dict()
            elif isinstance(result, dict):
                return result
            else:
                return dict(result) if result else None

        except ImportError:
            log_error("firecrawl-py not installed, falling back to BeautifulSoup")
            return None
        except Exception as e:
            log_error(f"Firecrawl scraping failed for {url}: {e}")
            return None

    async def crawl_url(
        self,
        url: str,
        limit: int = 100,
        max_depth: Optional[int] = None,
        include_paths: Optional[list[str]] = None,
        exclude_paths: Optional[list[str]] = None,
        crawl_entire_domain: bool = True,
        sitemap: Optional[str] = None,
        allow_subdomains: bool = False,
        allow_external_links: bool = False,
        delay: Optional[int] = None,
        formats: Optional[list[str]] = None,
        only_main_content: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Crawl a URL and its linked pages using Firecrawl

        Args:
            url: Starting URL to crawl
            limit: Maximum number of pages to crawl
            max_depth: Maximum crawl depth
            include_paths: URL patterns to include
            exclude_paths: URL patterns to exclude
            crawl_entire_domain: Allow crawling entire domain
            sitemap: Sitemap usage strategy ('only', 'include', 'skip')
            allow_subdomains: Allow crawling subdomains
            allow_external_links: Follow external links
            delay: Delay between requests in milliseconds
            formats: Output formats (e.g. ["markdown", "html"])
            only_main_content: Extract only main content

        Returns:
            Dictionary with crawled data or None if unavailable/failed
        """
        if not self.is_available():
            log_warning("Firecrawl not available, API key missing")
            return None

        try:
            from firecrawl import AsyncFirecrawl

            if self._client is None:
                self._client = AsyncFirecrawl(api_key=self.api_key)

            if formats is None:
                formats = ["markdown"]

            params = {"limit": limit}

            if max_depth is not None:
                params["max_discovery_depth"] = max_depth
            if include_paths is not None:
                params["include_paths"] = include_paths
            if exclude_paths is not None:
                params["exclude_paths"] = exclude_paths
            if crawl_entire_domain is not None:
                params["crawl_entire_domain"] = crawl_entire_domain
            if sitemap is not None:
                params["sitemap"] = sitemap if sitemap != 'only' else 'include'
            if allow_subdomains is not None:
                params["allow_subdomains"] = allow_subdomains
            if allow_external_links is not None:
                params["allow_external_links"] = allow_external_links
            if delay is not None:
                params["delay"] = delay

            params["scrape_options"] = {
                "formats": formats,
                "only_main_content": only_main_content,
            }

            timeout_seconds = max(60, limit * 2)

            log_info(f"Crawling URL with Firecrawl: {url}")
            log_info(f"Crawl params: {params}")

            max_retries = 2
            result = None

            for attempt in range(max_retries + 1):
                try:
                    if attempt > 0:
                        log_info(f"Retry attempt {attempt}/{max_retries} for crawl")

                    result = await self._client.crawl(
                        url=url,
                        **params,
                        poll_interval=2,
                        timeout=timeout_seconds,
                        request_timeout=60.0
                    )

                    log_info(f"Successfully crawled URL with Firecrawl: {url}")
                    break

                except Exception as e:
                    error_name = type(e).__name__
                    error_msg = str(e)

                    retryable_errors = [
                        'ConnectError', 'TimeoutError', 'Timeout',
                        'RemoteProtocolError', 'ReadTimeout', 'PoolTimeout',
                    ]

                    is_retryable = any(err in error_name or err in error_msg for err in retryable_errors)

                    if is_retryable and attempt < max_retries:
                        wait_time = 2 ** attempt
                        log_warning(f"Network error ({error_name}: {error_msg}), retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue

                    if attempt >= max_retries:
                        log_error(f"Max retries ({max_retries}) reached for {url}")
                    raise

            if hasattr(result, 'model_dump'):
                return result.model_dump()
            elif hasattr(result, 'dict'):
                return result.dict()
            elif isinstance(result, dict):
                return result
            else:
                return dict(result) if result else None

        except ImportError:
            log_error("firecrawl-py not installed, cannot use crawl feature")
            return None
        except Exception as e:
            log_error(f"Firecrawl crawling failed for {url}: {e}")
            log_error(f"Traceback: {traceback.format_exc()}")
            return None

    async def close(self):
        """Clean up resources"""
        self._client = None


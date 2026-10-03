"""
DeepSeek OCR Provider

Uses DeepSeek-OCR model via DeepInfra's OpenAI-compatible API.
- Model: deepseek-ai/DeepSeek-OCR
- Pricing: $0.03/M input tokens, $0.10/M output tokens
- Accepts images via URL or base64
- For multi-page PDFs: converts pages to images first

API docs: https://deepinfra.com/deepseek-ai/DeepSeek-OCR/api
"""

import asyncio
import base64
import logging
from pathlib import Path

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.infra.file_processing.ocr.base import (
    OCRProvider,
    OCRProviderAPIError,
    OCRProviderConfigError,
    OCRProviderTimeoutError,
    ParsedDocument,
)

logger = logging.getLogger(__name__)


class DeepSeekOCRConfig(BaseSettings):
    """Configuration for DeepSeek OCR via DeepInfra."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    deepinfra_api_key: str | None = Field(
        default=None,
        description="DeepInfra API Key (also used for DeepSeek OCR)",
    )

    deepseek_ocr_api_base: str = Field(
        default="https://api.deepinfra.com/v1/openai",
        description="DeepInfra OpenAI-compatible API base URL",
    )

    deepseek_ocr_model: str = Field(
        default="deepseek-ai/DeepSeek-OCR",
        description="Model identifier on DeepInfra",
    )

    deepseek_ocr_max_tokens: int = Field(
        default=8192,
        description="Max output tokens for OCR response",
    )

    deepseek_ocr_timeout: int = Field(
        default=120,
        description="HTTP request timeout in seconds",
    )

    deepseek_ocr_max_pages: int = Field(
        default=50,
        description="Max PDF pages to process (avoid excessive costs)",
    )


deepseek_ocr_config = DeepSeekOCRConfig()

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


class DeepSeekOCRProvider(OCRProvider):
    """
    DeepSeek OCR Provider via DeepInfra.

    Uses OpenAI-compatible chat completions API with vision.
    - Images: passed directly via presigned URL
    - PDFs: downloaded and converted to page images, then sent as base64
    """

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or deepseek_ocr_config.deepinfra_api_key
        self._base_url = deepseek_ocr_config.deepseek_ocr_api_base
        self._model = deepseek_ocr_config.deepseek_ocr_model
        self._max_tokens = deepseek_ocr_config.deepseek_ocr_max_tokens
        self._timeout = deepseek_ocr_config.deepseek_ocr_timeout
        self._max_pages = deepseek_ocr_config.deepseek_ocr_max_pages

        if not self._api_key:
            logger.warning("DeepInfra API key not configured for DeepSeek OCR")

    @property
    def name(self) -> str:
        return "deepseek"

    def _get_headers(self) -> dict:
        if not self._api_key:
            raise OCRProviderConfigError(
                provider=self.name,
                message="DeepInfra API key not configured. Set DEEPINFRA_API_KEY environment variable.",
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def parse_document(
        self,
        file_url: str,
        data_id: str | None = None,
    ) -> ParsedDocument:
        headers = self._get_headers()

        ext = self._guess_extension(file_url)
        is_pdf = ext in PDF_EXTENSIONS

        if is_pdf:
            markdown = await self._process_pdf(file_url, headers)
        else:
            markdown = await self._process_single_image(file_url, headers)

        return ParsedDocument(
            task_id=data_id or "deepseek-ocr",
            markdown_content=markdown,
            metadata={"provider": "deepseek", "model": self._model, "is_pdf": is_pdf},
        )

    async def _process_single_image(self, image_url: str, headers: dict) -> str:
        """Send a single image URL to the model."""
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    }
                ],
            }
        ]
        return await self._chat_completion(messages, headers)

    async def _process_pdf(self, pdf_url: str, headers: dict) -> str:
        """Download PDF, convert pages to images, send each page."""
        page_images = await self._pdf_url_to_base64_images(pdf_url)

        if not page_images:
            raise OCRProviderAPIError(
                provider=self.name,
                message="Failed to convert PDF to images (0 pages extracted)",
            )

        page_markdowns: list[str] = []
        for i, b64_img in enumerate(page_images):
            logger.info(f"[DeepSeek OCR] Processing page {i + 1}/{len(page_images)}")
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64_img}",
                            },
                        }
                    ],
                }
            ]
            md = await self._chat_completion(messages, headers)
            page_markdowns.append(md)

        return "\n\n---\n\n".join(page_markdowns)

    async def _chat_completion(self, messages: list[dict], headers: dict) -> str:
        """Call DeepInfra chat completions endpoint."""
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": messages,
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            except httpx.TimeoutException:
                raise OCRProviderTimeoutError(
                    provider=self.name,
                    message=f"Request timed out after {self._timeout}s",
                )
            except httpx.HTTPError as e:
                raise OCRProviderAPIError(
                    provider=self.name,
                    message=f"HTTP error: {e}",
                )

            if response.status_code == 401:
                raise OCRProviderConfigError(
                    provider=self.name,
                    message="Authentication failed. Check your DEEPINFRA_API_KEY.",
                    status_code=401,
                    raw_response=response.text,
                )

            if response.status_code == 429:
                raise OCRProviderAPIError(
                    provider=self.name,
                    message="Rate limit exceeded. Try again later.",
                    status_code=429,
                    raw_response=response.text,
                )

            if response.status_code != 200:
                raise OCRProviderAPIError(
                    provider=self.name,
                    message=f"API error (status {response.status_code}): {response.text}",
                    status_code=response.status_code,
                    raw_response=response.text,
                )

            result = response.json()

        choices = result.get("choices", [])
        if not choices:
            raise OCRProviderAPIError(
                provider=self.name,
                message="Empty response from model",
                raw_response=str(result),
            )

        content = choices[0].get("message", {}).get("content", "")
        if not content:
            raise OCRProviderAPIError(
                provider=self.name,
                message="No content in model response",
                raw_response=str(result),
            )

        return content.strip()

    async def _pdf_url_to_base64_images(self, pdf_url: str) -> list[str]:
        """Download PDF from URL and convert each page to a base64 PNG."""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise OCRProviderConfigError(
                provider=self.name,
                message=(
                    "PyMuPDF (fitz) is required for PDF processing. "
                    "Install it: pip install PyMuPDF"
                ),
            )

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            try:
                resp = await client.get(pdf_url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise OCRProviderAPIError(
                    provider=self.name,
                    message=f"Failed to download PDF: {e}",
                )

        pdf_bytes = resp.content

        def _convert() -> list[str]:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            images: list[str] = []
            page_count = min(len(doc), self._max_pages)
            for page_num in range(page_count):
                page = doc[page_num]
                # 2x zoom for better OCR quality
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat)
                png_data = pix.tobytes("png")
                b64 = base64.b64encode(png_data).decode("ascii")
                images.append(b64)
            doc.close()
            return images

        return await asyncio.to_thread(_convert)

    @staticmethod
    def _guess_extension(url: str) -> str:
        """Extract file extension from URL (ignore query params)."""
        path = url.split("?")[0]
        return Path(path).suffix.lower()

    async def health_check(self) -> bool:
        if not self._api_key:
            return False
        try:
            headers = self._get_headers()
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self._base_url}/models",
                    headers=headers,
                )
                return response.status_code == 200
        except Exception as e:
            logger.warning(f"[DeepSeek OCR] Health check failed: {e}")
            return False

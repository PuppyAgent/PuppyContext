"""
File Ingest Module - Handles file upload and OCR processing.

Architecture:
- service.py: ETLService - file task management
- jobs/: ARQ worker jobs (ocr_job, postprocess_job)
- ocr/: MineRU OCR client
- rules/: ETL extraction rules
- tasks/: Task models and repository

Supported file types:
- PDF: OCR via MineRU
- Images: OCR via MineRU
- Text files: Direct import (txt, md, json, code files)
- Documents: OCR processing (docx, xlsx)

NOTE: All API endpoints are now unified under /api/v1/ingest/*
See src/upload/router.py for the unified entry point.
"""

from src.infra.file_processing.service import ETLService

__all__ = ["ETLService"]

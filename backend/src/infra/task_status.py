"""Shared presentation status vocabulary; domain repositories own transitions."""
from enum import Enum


class IngestStatus(str, Enum):
    """Unified status - maps underlying ETL/Import statuses."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

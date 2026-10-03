"""
Ingest Module Dependencies - FastAPI dependency injection.
"""

from typing import Annotated

from fastapi import Depends

from src.platform.upload.dependencies import get_etl_service
from src.ingest.service import IngestService
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.service import AuthorizationService


def get_ingest_service(
    file_service=Depends(get_etl_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> IngestService:
    """Get IngestService instance (file-only)."""
    return IngestService(file_service=file_service, authorization=authorization)


IngestServiceDep = Annotated[IngestService, Depends(get_ingest_service)]

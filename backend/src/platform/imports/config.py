"""Import-owned task policy; Redis transport is shared infrastructure."""
from src.infra.queue_config import QueueConnectionConfig


class ImportWorkerConfig(QueueConnectionConfig):
    import_arq_queue_name: str = "imports"
    import_task_timeout: int = 900


import_config = ImportWorkerConfig()

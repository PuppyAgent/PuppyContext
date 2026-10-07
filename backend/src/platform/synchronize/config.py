"""Synchronize-owned queue/timeout for both generic and GitHub jobs."""
from src.infra.queue_config import QueueConnectionConfig


class SynchronizeWorkerConfig(QueueConnectionConfig):
    synchronize_arq_queue_name: str = "synchronize"
    synchronize_task_timeout: int = 900


synchronize_config = SynchronizeWorkerConfig()

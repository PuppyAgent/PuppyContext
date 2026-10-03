"""
MineRU Client Exceptions

Custom exceptions for MineRU client errors.
"""


class MineRUError(Exception):
    """Base exception for MineRU client errors."""

    def __init__(self, message: str, original_error: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.original_error = original_error


class MineRUAPIError(MineRUError):
    """Exception raised when MineRU API call fails."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"MineRU API error (status {status_code}): {message}")
        self.status_code = status_code


class MineRUTaskFailedError(MineRUError):
    """Exception raised when MineRU parsing task fails."""

    def __init__(self, task_id: str, error_message: str):
        super().__init__(f"MineRU task {task_id} failed: {error_message}")
        self.task_id = task_id
        self.error_message = error_message


class MineRUTimeoutError(MineRUError):
    """Exception raised when polling for task completion times out."""

    def __init__(self, task_id: str, timeout: int):
        super().__init__(
            f"MineRU task {task_id} did not complete within {timeout} seconds"
        )
        self.task_id = task_id
        self.timeout = timeout


class MineRUAPIKeyError(MineRUError):
    """Exception raised when MineRU API key is missing or invalid."""

    def __init__(self):
        super().__init__(
            "MineRU API key is missing. Please set the MINERU_API_KEY environment variable."
        )

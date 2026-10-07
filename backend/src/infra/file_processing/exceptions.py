"""
ETL Exceptions

Custom exceptions for ETL service.
"""


class ETLError(Exception):
    """Base exception for ETL errors."""

    def __init__(self, message: str, original_error: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.original_error = original_error


class RuleNotFoundError(ETLError):
    """Exception raised when ETL rule is not found."""

    def __init__(self, rule_id: str):
        super().__init__(f"ETL rule not found: {rule_id}")
        self.rule_id = rule_id


class ETLTransformationError(ETLError):
    """Exception raised when transformation fails."""

    def __init__(self, message: str, rule_id: str):
        super().__init__(f"Transformation failed for rule {rule_id}: {message}")
        self.rule_id = rule_id


class ETLTaskTimeoutError(ETLError):
    """Exception raised when ETL task times out."""

    def __init__(self, task_id: str, timeout: int):
        super().__init__(f"ETL task {task_id} timed out after {timeout} seconds")
        self.task_id = task_id
        self.timeout = timeout


class FileNotFoundError(ETLError):
    """Exception raised when source file is not found."""

    def __init__(self, file_path: str):
        super().__init__(f"File not found: {file_path}")
        self.file_path = file_path

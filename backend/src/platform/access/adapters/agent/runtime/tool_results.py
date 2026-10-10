"""Recoverable tool outcomes, distinct from execution-lease/run failures."""


def tool_error(code: str, message: str) -> dict:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
        "details": {"code": code, "recoverable": True},
    }


def tool_denial(name: str, readonly: bool) -> dict | None:
    allowed = {"read", "ls", "find", "grep"}
    if not readonly:
        allowed |= {"write", "edit", "bash"}
    if name in allowed:
        return None
    if readonly and name in {"write", "edit", "bash"}:
        return tool_error(
            "tool_permission_denied",
            "This run is read-only. The tool was not executed. Continue using the available "
            "read tools; do not retry this operation without write authorization.",
        )
    return tool_error(
        "tool_unavailable",
        "This tool is not available in this run and was not executed. "
        "Continue using the tools provided to you.",
    )


def approval_denied() -> dict:
    return tool_error(
        "tool_approval_denied",
        "The user declined this tool call. It was not executed. "
        "Continue with another approach; do not repeat the declined operation.",
    )

from fastapi import HTTPException, status


class WebchartError(Exception):
    """Base exception for WebchartAPI."""

    def __init__(self, message: str, code: str = "INTERNAL_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class DatasetNotFoundError(WebchartError):
    def __init__(self, dataset_id: str):
        super().__init__(f"Dataset not found: {dataset_id}", "DATASET_NOT_FOUND")


class DatasetProcessingError(WebchartError):
    def __init__(self, message: str):
        super().__init__(message, "DATASET_PROCESSING_ERROR")


class UnsupportedFormatError(WebchartError):
    def __init__(self, fmt: str):
        super().__init__(f"Unsupported format: {fmt}", "UNSUPPORTED_FORMAT")


class SessionNotFoundError(WebchartError):
    def __init__(self, session_id: str):
        super().__init__(f"Session not found: {session_id}", "SESSION_NOT_FOUND")


class RenderError(WebchartError):
    def __init__(self, message: str):
        super().__init__(message, "RENDER_ERROR")


class ProtocolNotSupportedError(WebchartError):
    def __init__(self, protocol: str):
        super().__init__(f"Unsupported protocol: {protocol}", "PROTOCOL_NOT_SUPPORTED")


class FileTooLargeError(WebchartError):
    def __init__(self, size: int, max_size: int):
        super().__init__(
            f"File size {size} exceeds maximum {max_size}",
            "FILE_TOO_LARGE",
        )


class QueryTooLargeError(WebchartError):
    def __init__(self, row_count: int, max_rows: int):
        super().__init__(
            f"Query would return {row_count} rows (max {max_rows}); narrow the range",
            "QUERY_TOO_LARGE",
        )


def webchart_error_to_http(err: WebchartError) -> HTTPException:
    """Convert a WebchartError to an appropriate HTTPException."""
    status_map = {
        "DATASET_NOT_FOUND": status.HTTP_404_NOT_FOUND,
        "SESSION_NOT_FOUND": status.HTTP_404_NOT_FOUND,
        "UNSUPPORTED_FORMAT": status.HTTP_400_BAD_REQUEST,
        "PROTOCOL_NOT_SUPPORTED": status.HTTP_400_BAD_REQUEST,
        "FILE_TOO_LARGE": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        "QUERY_TOO_LARGE": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        "DATASET_PROCESSING_ERROR": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "RENDER_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
    return HTTPException(
        status_code=status_map.get(err.code, status.HTTP_500_INTERNAL_SERVER_ERROR),
        detail={"code": err.code, "message": err.message},
    )

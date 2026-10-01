"""业务错误类型：携带稳定的机器可读 code 与 HTTP 状态码。"""
from __future__ import annotations

from typing import Any


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        # 结构化逐批明细（例如确认失败时不符批次列表）
        self.details = details


def bad_request(
    code: str, message: str, details: list[dict[str, Any]] | None = None
) -> APIError:
    return APIError(400, code, message, details)


def not_found(
    code: str, message: str, details: list[dict[str, Any]] | None = None
) -> APIError:
    return APIError(404, code, message, details)


def conflict(
    code: str, message: str, details: list[dict[str, Any]] | None = None
) -> APIError:
    return APIError(409, code, message, details)


def unprocessable(
    code: str, message: str, details: list[dict[str, Any]] | None = None
) -> APIError:
    return APIError(422, code, message, details)

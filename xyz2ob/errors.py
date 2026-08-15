"""统一错误类型：所有面向用户的失败都带一句「怎么办」。"""

from __future__ import annotations


class PodError(Exception):
    """带可执行建议的业务异常。"""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - 展示用
        if self.hint:
            return f"{self.message}\n  → {self.hint}"
        return self.message

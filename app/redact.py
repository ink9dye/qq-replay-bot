"""日志脱敏：接口令牌、密码、会话 token 永不进入界面或日志文件。"""
from __future__ import annotations

from typing import Iterable


class Redactor:
    def __init__(self, values: Iterable[str] = ()):
        self._values: list[str] = []
        self.update(values)

    def update(self, values: Iterable[str]) -> None:
        for value in values:
            text = str(value or "")
            if len(text) >= 3 and text not in self._values:
                self._values.append(text)
        self._values.sort(key=len, reverse=True)

    def __call__(self, text: str) -> str:
        result = str(text or "")
        for value in self._values:
            result = result.replace(value, "***")
        return result

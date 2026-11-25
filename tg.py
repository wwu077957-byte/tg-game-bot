"""Telegram Bot API 的极简封装（仅使用标准库）。

测试时可以替换本模块的 ``http_request`` 与 ``sleep`` 两个函数，
从而完全不发出真实网络请求。
"""
import json
import logging
import socket
import time
import urllib.error
import urllib.request

log = logging.getLogger("gamebot.tg")

API_BASE = "https://api.telegram.org"


def http_request(url, data, timeout):
    """发送 POST 请求，返回 (状态码, 响应字节)。HTTP 错误也返回其响应体。"""
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # pragma: no cover
            body = b""
        return e.code, body


def sleep(seconds):
    time.sleep(seconds)


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"[{code}] {description}")
        self.code = code
        self.description = description or ""
        self.retry_after = retry_after


class NetworkError(Exception):
    pass


class Api:
    def __init__(self, token, max_429_retries=3):
        self.token = token
        self.max_429_retries = max_429_retries

    def _mask(self, text):
        return str(text).replace(self.token, "***") if self.token else str(text)

    def call(self, method, params=None, timeout=20):
        url = f"{API_BASE}/bot{self.token}/{method}"
        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        attempt = 0
        while True:
            try:
                status, raw = http_request(url, body, timeout)
            except (urllib.error.URLError, OSError, socket.timeout, TimeoutError) as e:
                raise NetworkError(self._mask(e)) from None
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                if status >= 500:
                    raise NetworkError(f"HTTP {status}")
                raise TelegramError(status, "无法解析的响应")
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code", status)
            params_ = data.get("parameters") or {}
            retry_after = params_.get("retry_after")
            if code == 429 and attempt < self.max_429_retries:
                wait = int(retry_after or 1)
                log.warning("%s 触发限流，%s 秒后重试", method, wait)
                sleep(wait)
                attempt += 1
                continue
            if code >= 500:
                raise NetworkError(f"HTTP {code}: {data.get('description')}")
            raise TelegramError(code, data.get("description", ""), retry_after)

    # 常用方法
    def get_me(self):
        return self.call("getMe")

    def get_updates(self, offset=None, timeout=30):
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 15) or []

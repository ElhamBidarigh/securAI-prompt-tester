"""
Target: how the tester talks to the app being tested.

Only the standard library is used (urllib), so the tool runs on a stock Python.

The request body is a JSON template that must contain the placeholder {{prompt}}.
The default is {"message": "{{prompt}}"}. Placeholders are replaced inside the
parsed JSON structure, so quotes and newlines in payloads never break the JSON.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

PLACEHOLDER = "{{prompt}}"
DEFAULT_TEMPLATE = {"message": PLACEHOLDER}

# Tried in order when --response-path is not given.
AUTO_PATHS = [
    "reply", "response", "answer", "output", "text", "message", "content", "result",
    "choices.0.message.content", "choices.0.text",
    "content.0.text",
    "data.reply", "data.response", "data.answer", "data.text",
]

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}
MAX_RETRY_WAIT = 10.0


class TargetError(Exception):
    pass


@dataclass
class TargetResponse:
    text: str | None = None
    http_status: int | None = None
    error: str | None = None


def is_local_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS


def template_has_placeholder(obj: Any) -> bool:
    if isinstance(obj, str):
        return PLACEHOLDER in obj
    if isinstance(obj, dict):
        return any(template_has_placeholder(v) for v in obj.values())
    if isinstance(obj, list):
        return any(template_has_placeholder(v) for v in obj)
    return False


def fill_template(obj: Any, prompt: str) -> Any:
    if isinstance(obj, str):
        return obj.replace(PLACEHOLDER, prompt)
    if isinstance(obj, dict):
        return {k: fill_template(v, prompt) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill_template(v, prompt) for v in obj]
    return obj


def _walk(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                raise KeyError(path)
        elif isinstance(cur, dict):
            if part not in cur:
                raise KeyError(path)
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


def extract_text(body: str, response_path: str | None = None) -> str:
    """Pull the model's text out of the HTTP response body."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        if response_path:
            raise TargetError("response is not JSON but --response-path was given")
        return body
    if response_path:
        try:
            value = _walk(data, response_path)
        except KeyError:
            raise TargetError(f"response path '{response_path}' was not found in the JSON response")
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    for path in AUTO_PATHS:
        try:
            value = _walk(data, path)
        except KeyError:
            continue
        if isinstance(value, str):
            return value
    return body


class Target:
    def __init__(self, url: str, method: str = "POST", headers: dict | None = None,
                 body_template: Any = None, response_path: str | None = None,
                 query_param: str | None = None, timeout: float = 30.0,
                 retries: int = 2, retry_wait: float = 1.5):
        self.retries = max(0, retries)
        self.retry_wait = retry_wait
        self.url = url
        self.method = method.upper()
        self.headers = dict(headers or {})
        self.body_template = DEFAULT_TEMPLATE if body_template is None else body_template
        self.response_path = response_path
        self.query_param = query_param
        self.timeout = timeout
        if self.method not in ("POST", "GET"):
            raise TargetError("only POST and GET are supported")
        if self.method == "POST" and not template_has_placeholder(self.body_template):
            raise TargetError("the body template must contain the placeholder {{prompt}}")
        if self.method == "GET" and not self.query_param:
            raise TargetError("GET targets need --query-param (the name of the query parameter that carries the prompt)")

    def _build_request(self, prompt: str) -> urllib.request.Request:
        headers = {"User-Agent": "securAI-prompt-tester/0.2", **self.headers}
        if self.method == "POST":
            payload = json.dumps(fill_template(self.body_template, prompt), ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
            return urllib.request.Request(self.url, data=payload, headers=headers, method="POST")
        sep = "&" if "?" in self.url else "?"
        url = self.url + sep + urllib.parse.urlencode({self.query_param: prompt})
        return urllib.request.Request(url, headers=headers, method="GET")

    def _wait_before_retry(self, attempt: int, retry_after: str | None) -> None:
        wait = self.retry_wait * (attempt + 1)
        if retry_after:
            try:
                wait = max(wait, float(retry_after))
            except ValueError:
                pass
        time.sleep(min(wait, MAX_RETRY_WAIT))

    def send(self, prompt: str) -> TargetResponse:
        """Send one prompt. Rate limits and temporary server errors are retried a few times."""
        attempt = 0
        while True:
            req = self._build_request(prompt)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    status = resp.status
                break
            except urllib.error.HTTPError as e:
                if e.code in RETRY_STATUSES and attempt < self.retries:
                    self._wait_before_retry(attempt, e.headers.get("Retry-After") if e.headers else None)
                    attempt += 1
                    continue
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace").strip().replace("\n", " ")[:300]
                except Exception:
                    pass
                return TargetResponse(http_status=e.code, error=f"HTTP {e.code}" + (f": {detail}" if detail else ""))
            except urllib.error.URLError as e:
                return TargetResponse(error=f"connection failed: {e.reason}")
            except (socket.timeout, TimeoutError):
                return TargetResponse(error=f"timed out after {self.timeout}s")
            except OSError as e:
                return TargetResponse(error=f"network error: {e}")
        try:
            text = extract_text(body, self.response_path)
        except TargetError as e:
            return TargetResponse(http_status=status, error=str(e))
        return TargetResponse(text=text, http_status=status)

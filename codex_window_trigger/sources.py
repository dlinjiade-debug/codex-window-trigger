import json
import math
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


SOURCE_URLS = {
    "forecast": "https://codex-reset.com/api/forecast",
    "feed": "https://codex-reset.com/api/feed",
    "timeline": "https://codex-reset.com/api/timeline",
}


class _RejectRedirects(HTTPRedirectHandler):
    """Reject redirects before urllib can contact a redirect destination."""

    def _reject(self, request, fp, code, msg, headers):
        raise HTTPError(request.full_url, code, "redirect refused", headers, fp)

    http_error_301 = _reject
    http_error_302 = _reject
    http_error_303 = _reject
    http_error_307 = _reject
    http_error_308 = _reject


def _default_opener(request: Request, *, timeout: float):
    return build_opener(_RejectRedirects()).open(request, timeout=timeout)


def _parse_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def _reject_json_constant(_value: str) -> None:
    raise ValueError("JSON numbers must be finite")


def fetch_json(
    url: str,
    *,
    opener: Callable[..., Any] | None = None,
    timeout: float = 8.0,
    max_bytes: int = 1_048_576,
) -> dict[str, Any]:
    if url not in SOURCE_URLS.values():
        raise ValueError("URL is outside the source allowlist")

    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "codex-window-trigger/1"},
    )
    open_request = _default_opener if opener is None else opener
    with open_request(request, timeout=timeout) as response:
        if response.geturl() != url:
            raise ValueError("redirect left the source allowlist")
        status = response.status
        if not 200 <= status < 300:
            raise ValueError("response returned non-success status")
        if response.headers.get_content_type() != "application/json":
            raise ValueError("response is not JSON")
        body = response.read(max_bytes + 1)

    if len(body) > max_bytes:
        raise ValueError("response exceeds size limit")
    value = json.loads(
        body.decode("utf-8"),
        parse_constant=_reject_json_constant,
        parse_float=_parse_float,
    )
    if not isinstance(value, dict):
        raise ValueError("top-level JSON must be an object")
    return value


def fetch_sources(
    *, fetcher: Callable[[str], dict[str, Any]] = fetch_json
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (
        fetcher(SOURCE_URLS["forecast"]),
        fetcher(SOURCE_URLS["feed"]),
        fetcher(SOURCE_URLS["timeline"]),
    )

"""LETHE supplemental write / search v2 の最小 HTTP クライアント。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from vsm.lethe_bridge.models import SearchResponse, SupplementalRecord

SUPPLEMENTAL_WRITE_PATH = "/api/supplemental"
SEARCH_V2_PATH = "/api/v2/search"


class LetheRequestError(RuntimeError):
    """LETHE の通信または応答契約が不正な場合のエラー。"""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class LetheTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> HttpResponse: ...


class UrllibTransport:
    """標準ライブラリだけを使う同期 HTTP transport。"""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request) as response:  # noqa: S310 - endpoint is validated config
                return HttpResponse(status=response.status, body=response.read())
        except HTTPError as exc:
            return HttpResponse(status=exc.code, body=exc.read())
        except URLError as exc:
            raise LetheRequestError(f"LETHE request failed: {exc.reason}") from exc


class LetheClient:
    """承認済み live 設定でだけ構築する LETHE API クライアント。"""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        transport: LetheTransport | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("lethe.endpoint must be an absolute http(s) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("lethe.endpoint must not contain a query or fragment")
        if not token.strip():
            raise ValueError("lethe.token must not be empty")
        self._endpoint = endpoint.rstrip("/")
        self._token = token
        self._transport = transport if transport is not None else UrllibTransport()

    def write_supplemental(self, record: SupplementalRecord) -> None:
        body = record.model_dump_json().encode("utf-8")
        response = self._transport.request(
            method="POST",
            url=f"{self._endpoint}{SUPPLEMENTAL_WRITE_PATH}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Idempotency-Key": record.record_id,
            },
            body=body,
        )
        self._require_success(response, operation="supplemental write")

    def search(self, query: str) -> list[SupplementalRecord]:
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("LETHE search query must not be empty")
        response = self._transport.request(
            method="GET",
            url=(
                f"{self._endpoint}{SEARCH_V2_PATH}?"
                + urlencode({"query": cleaned})
            ),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
            body=None,
        )
        self._require_success(response, operation="search v2")
        try:
            raw = json.loads(response.body.decode("utf-8"))
            return SearchResponse.model_validate(raw).records
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise LetheRequestError("LETHE search v2 returned an invalid response") from exc

    @staticmethod
    def _require_success(response: HttpResponse, *, operation: str) -> None:
        if 200 <= response.status < 300:
            return
        raise LetheRequestError(
            f"LETHE {operation} failed with HTTP {response.status}"
        )


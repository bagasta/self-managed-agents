from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest


@dataclass
class _MockRoute:
    return_value: httpx.Response | None = None
    side_effect: Callable[[httpx.Request], httpx.Response] | BaseException | None = None

    def mock(
        self,
        *,
        return_value: httpx.Response | None = None,
        side_effect: Callable[[httpx.Request], httpx.Response] | BaseException | None = None,
    ) -> "_MockRoute":
        self.return_value = return_value
        self.side_effect = side_effect
        return self


class _SimpleRespxMock:
    def __init__(self) -> None:
        self._post_routes: dict[str, _MockRoute] = {}

    def post(self, url: str) -> _MockRoute:
        route = self._post_routes.setdefault(url, _MockRoute())
        return route

    async def dispatch_post(
        self,
        _client: httpx.AsyncClient,
        url: str,
        *args: Any,
        **kwargs: Any,
    ) -> httpx.Response:
        route = self._post_routes.get(str(url))
        if route is None:
            raise AssertionError(f"Unexpected POST request: {url}")

        request = httpx.Request(
            "POST",
            str(url),
            json=kwargs.get("json"),
            headers=kwargs.get("headers"),
        )
        if route.side_effect is not None:
            if isinstance(route.side_effect, BaseException):
                raise route.side_effect
            return self._with_request(route.side_effect(request), request)
        if route.return_value is None:
            raise AssertionError(f"No mock response configured for POST {url}")
        return self._with_request(route.return_value, request)

    @staticmethod
    def _with_request(response: httpx.Response, request: httpx.Request) -> httpx.Response:
        try:
            response.request
        except RuntimeError:
            response._request = request
        return response


@pytest.fixture
def respx_mock(monkeypatch: pytest.MonkeyPatch) -> _SimpleRespxMock:
    mock = _SimpleRespxMock()
    monkeypatch.setattr(
        httpx.AsyncClient,
        "post",
        lambda self, url, *args, **kwargs: mock.dispatch_post(self, url, *args, **kwargs),
    )
    return mock

from __future__ import annotations

from typing import Any, Protocol, cast

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from fantasy_war_room.errors import NotFoundError, ProviderError


class SleeperProvider(Protocol):
    def get_user(self, username_or_id: str) -> dict[str, Any]: ...
    def get_user_leagues(self, user_id: str, season: str) -> list[dict[str, Any]]: ...
    def get_league(self, league_id: str) -> dict[str, Any]: ...
    def get_league_drafts(self, league_id: str) -> list[dict[str, Any]]: ...
    def get_draft(self, draft_id: str) -> dict[str, Any]: ...
    def get_draft_picks(self, draft_id: str) -> list[dict[str, Any]]: ...


class SleeperPlayerProvider(Protocol):
    def get_nfl_players(self) -> dict[str, dict[str, Any]]: ...


class SleeperClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": "fantasy-war-room/0.1 (+local fantasy draft tool)"},
        )

    def close(self) -> None:
        self.client.close()

    @retry(
        retry=retry_if_exception(lambda exc: _is_transient(exc)),
        wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _get_with_retries(self, path: str) -> Any:
        response = self.client.get(path)
        response.raise_for_status()
        return response.json()

    def _get(self, path: str) -> Any:
        try:
            return self._get_with_retries(path)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise NotFoundError(f"Sleeper resource was not found: {path}") from exc
            raise ProviderError(f"Sleeper returned HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Sleeper request failed: {exc}") from exc

    def get_user(self, username_or_id: str) -> dict[str, Any]:
        value = self._get(f"/user/{username_or_id}")
        if not value:
            raise NotFoundError(f"Sleeper user {username_or_id!r} was not found")
        return cast(dict[str, Any], value)

    def get_user_leagues(self, user_id: str, season: str) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._get(f"/user/{user_id}/leagues/nfl/{season}"))

    def get_league(self, league_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._get(f"/league/{league_id}"))

    def get_league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._get(f"/league/{league_id}/drafts"))

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._get(f"/draft/{draft_id}"))

    def get_draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._get(f"/draft/{draft_id}/picks"))

    def get_nfl_players(self) -> dict[str, dict[str, Any]]:
        return cast(dict[str, dict[str, Any]], self._get("/players/nfl"))


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or 500 <= status < 600
    return False

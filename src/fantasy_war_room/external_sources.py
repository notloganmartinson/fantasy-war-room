from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
import polars as pl
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from fantasy_war_room.errors import InputError, ProviderError

FFC_BASE_URL = "https://fantasyfootballcalculator.com"
NFLVERSE_SCHEDULE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
)
FFC_SOURCE = "fantasy-football-calculator"
NFLVERSE_SOURCE = "nflverse"
ADP_TRANSFORMATION_VERSION = "ffc-adp-1.0"
SCHEDULE_TRANSFORMATION_VERSION = "nflverse-byes-1.0"
CACHE_MAX_AGE = timedelta(hours=24)
FFC_OFFENSIVE_DEFAULTS = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "pass_int": -2.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "fum_lost": -2.0,
    "pass_2pt": 2.0,
    "rush_2pt": 2.0,
    "rec_2pt": 2.0,
}
FFC_UNSUPPORTED_OFFENSIVE_KEYS = frozenset(
    {"pass_cmp", "pass_att", "pass_fd", "rush_att", "rush_fd", "rec_fd"}
)

NFL_TEAMS = frozenset(
    {
        "ARI",
        "ATL",
        "BAL",
        "BUF",
        "CAR",
        "CHI",
        "CIN",
        "CLE",
        "DAL",
        "DEN",
        "DET",
        "GB",
        "HOU",
        "IND",
        "JAX",
        "KC",
        "LV",
        "LAC",
        "LAR",
        "MIA",
        "MIN",
        "NE",
        "NO",
        "NYG",
        "NYJ",
        "PHI",
        "PIT",
        "SEA",
        "SF",
        "TB",
        "TEN",
        "WAS",
    }
)
TEAM_ALIASES = {"LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


@dataclass(frozen=True)
class SourcePayload:
    content: bytes
    source_uri: str
    fetched_at: datetime
    payload_hash: str
    from_cache: bool
    version_hint: str | None


@dataclass(frozen=True)
class NormalizedSourceData:
    frame: pl.DataFrame
    source_version: str
    source_uri: str
    fetched_at: datetime
    payload_hash: str
    transformation_version: str
    from_cache: bool


class PortableIntelligenceProvider(Protocol):
    def acquire_adp(
        self,
        *,
        season: str,
        league_size: int,
        scoring_format: str,
        draft_type: str,
        force: bool = False,
    ) -> NormalizedSourceData: ...

    def acquire_byes(self, *, season: str, force: bool = False) -> NormalizedSourceData: ...

    def close(self) -> None: ...


def scoring_to_ffc(scoring_format: str) -> str:
    mapping = {"full_ppr": "ppr", "half_ppr": "half-ppr", "standard": "standard"}
    try:
        return mapping[scoring_format]
    except KeyError as exc:
        raise InputError(
            "unsupported_adp_scoring_format",
            "Fantasy Football Calculator has no exact mapping for this league's scoring format",
            {"scoring_format": scoring_format, "supported": sorted(mapping)},
        ) from exc


def classify_ffc_scoring(scoring: dict[str, float]) -> str:
    """Return an FWR generic scoring class only for an exact FFC-compatible offense."""
    conflicts = {
        key: {"configured": scoring[key], "required": expected}
        for key, expected in FFC_OFFENSIVE_DEFAULTS.items()
        if key in scoring and scoring[key] != expected
    }
    unsupported = sorted(
        key
        for key, value in scoring.items()
        if value != 0 and (key.startswith("bonus_") or key in FFC_UNSUPPORTED_OFFENSIVE_KEYS)
    )
    receptions = scoring.get("rec", 0.0)
    formats = {1.0: "full_ppr", 0.5: "half_ppr", 0.0: "standard"}
    if conflicts or unsupported or receptions not in formats:
        raise InputError(
            "unsupported_adp_scoring_format",
            "Fantasy Football Calculator has no exact mapping for this league's scoring settings",
            {
                "receptions": receptions,
                "conflicts": conflicts,
                "unsupported_scoring_keys": unsupported,
            },
        )
    return formats[receptions]


class PortableSourceClient:
    def __init__(self, timeout: float, cache_dir: Path) -> None:
        self.client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "fantasy-war-room/0.1 (+local fantasy draft tool)"},
            follow_redirects=True,
        )
        self.cache_dir = cache_dir.expanduser().resolve() / "portable-intelligence"

    def close(self) -> None:
        self.client.close()

    @retry(
        retry=retry_if_exception(lambda exc: _is_transient(exc)),
        wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _download(self, url: str) -> httpx.Response:
        response = self.client.get(url)
        response.raise_for_status()
        return response

    def fetch(self, url: str, cache_name: str, *, force: bool = False) -> SourcePayload:
        path = self.cache_dir / cache_name
        if not force and path.is_file():
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if datetime.now(UTC) - modified <= CACHE_MAX_AGE:
                content = path.read_bytes()
                return SourcePayload(content, url, modified, _sha256(content), True, None)
        try:
            response = self._download(url)
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Portable data provider returned HTTP {exc.response.status_code}",
                {"status_code": exc.response.status_code, "source_uri": url},
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "Portable data provider request failed",
                {"error_type": type(exc).__name__, "source_uri": url},
            ) from exc
        fetched_at = datetime.now(UTC)
        content = response.content
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        version = response.headers.get("etag") or response.headers.get("last-modified")
        return SourcePayload(content, url, fetched_at, _sha256(content), False, version)


class PublicIntelligenceAdapter:
    """Documented public-source adapter used only by the explicit bootstrap workflow."""

    def __init__(self, timeout: float, cache_dir: Path) -> None:
        self.client = PortableSourceClient(timeout, cache_dir)

    def close(self) -> None:
        self.client.close()

    def acquire_adp(
        self,
        *,
        season: str,
        league_size: int,
        scoring_format: str,
        draft_type: str,
        force: bool = False,
    ) -> NormalizedSourceData:
        return acquire_ffc_adp(
            self.client,
            season=season,
            league_size=league_size,
            scoring_format=scoring_format,
            draft_type=draft_type,
            force=force,
        )

    def acquire_byes(self, *, season: str, force: bool = False) -> NormalizedSourceData:
        return acquire_nflverse_byes(self.client, season=season, force=force)


def acquire_ffc_adp(
    client: PortableSourceClient,
    *,
    season: str,
    league_size: int,
    scoring_format: str,
    draft_type: str,
    force: bool = False,
) -> NormalizedSourceData:
    if draft_type != "snake":
        raise InputError(
            "unsupported_adp_draft_type",
            "Fantasy Football Calculator bootstrap supports snake drafts only",
            {"draft_type": draft_type},
        )
    if league_size <= 0:
        raise InputError("invalid_league_size", "League size must be greater than zero")
    endpoint = scoring_to_ffc(scoring_format)
    url = f"{FFC_BASE_URL}/api/v1/adp/{endpoint}?teams={league_size}&year={season}"
    payload = client.fetch(
        url,
        f"ffc-adp-{season}-{league_size}-{endpoint}.json",
        force=force,
    )
    value = _ffc_json(payload)
    _validate_ffc_meta(value.get("meta"), endpoint=endpoint, league_size=league_size)
    players = cast(list[Any], value["players"])
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(players, start=1):
        if not isinstance(raw, dict):
            raise ProviderError(
                "Fantasy Football Calculator returned an invalid player row", {"row": index}
            )
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProviderError(
                "Fantasy Football Calculator returned an invalid player row", {"row": index}
            )
        adp = _finite_number(raw.get("adp"), index, "adp", required=True)
        adp_sd = _finite_number(raw.get("stdev"), index, "stdev", required=False)
        sample_size = _nonnegative_integer(raw.get("times_drafted"), index, "times_drafted")
        if adp is None or adp <= 0 or (adp_sd is not None and adp_sd < 0):
            raise ProviderError(
                "Fantasy Football Calculator returned invalid numeric player data",
                {"row": index},
            )
        rows.append(
            {
                "player_name": name.strip(),
                "position": str(raw.get("position") or ""),
                "team": str(raw.get("team") or ""),
                "overall_adp": adp,
                "adp_sd": adp_sd,
                "sample_size": sample_size,
                "provider_player_id": raw.get("player_id"),
                "provider_payload": json.dumps(raw, sort_keys=True, separators=(",", ":")),
            }
        )
    if not rows:
        raise ProviderError("Fantasy Football Calculator returned no ADP players")
    return NormalizedSourceData(
        pl.DataFrame(rows),
        payload.payload_hash,
        url,
        payload.fetched_at,
        payload.payload_hash,
        ADP_TRANSFORMATION_VERSION,
        payload.from_cache,
    )


def acquire_nflverse_byes(
    client: PortableSourceClient, *, season: str, force: bool = False
) -> NormalizedSourceData:
    payload = client.fetch(NFLVERSE_SCHEDULE_URL, "nflverse-games.csv", force=force)
    try:
        frame = pl.read_csv(io.BytesIO(payload.content), infer_schema=False)
    except Exception as exc:
        raise ProviderError(f"nflverse returned malformed schedule CSV: {exc}") from exc
    required = {"season", "week", "game_type", "home_team", "away_team"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ProviderError("nflverse schedule is missing required columns", {"columns": missing})
    games = frame.filter(
        (pl.col("season") == season) & (pl.col("game_type").str.to_uppercase() == "REG")
    )
    if games.is_empty():
        raise InputError(
            "schedule_season_unavailable",
            "nflverse has no complete regular-season schedule for the active season",
            {"season": season},
        )
    weeks_by_team: dict[str, set[int]] = {team: set() for team in NFL_TEAMS}
    game_keys: set[tuple[int, str, str]] = set()
    for row_number, raw in enumerate(games.to_dicts(), start=2):
        try:
            week = int(str(raw["week"]))
        except (TypeError, ValueError) as exc:
            raise InputError(
                "invalid_schedule_week", "nflverse schedule has an invalid week"
            ) from exc
        home = _normalize_team(raw["home_team"])
        away = _normalize_team(raw["away_team"])
        if home == away or not 1 <= week <= 18:
            raise InputError(
                "invalid_schedule_game",
                "nflverse schedule contains an invalid game",
                {"row": row_number, "week": week, "home": home, "away": away},
            )
        key = (week, home, away)
        if key in game_keys or week in weeks_by_team[home] or week in weeks_by_team[away]:
            raise InputError(
                "ambiguous_schedule",
                "nflverse schedule contains duplicate team-week data",
                {"row": row_number, "week": week, "home": home, "away": away},
            )
        game_keys.add(key)
        weeks_by_team[home].add(week)
        weeks_by_team[away].add(week)
    all_weeks = set(range(1, 19))
    rows = []
    invalid: dict[str, list[int]] = {}
    for team in sorted(NFL_TEAMS):
        missing_weeks = sorted(all_weeks - weeks_by_team[team])
        if len(weeks_by_team[team]) != 17 or len(missing_weeks) != 1:
            invalid[team] = missing_weeks
        else:
            rows.append({"team": team, "bye_week": missing_weeks[0]})
    if invalid:
        raise InputError(
            "incomplete_schedule",
            "nflverse schedule does not yield exactly one bye for every NFL team",
            {"season": season, "teams": invalid},
        )
    # The release has no semantic season version; its immutable content hash is the
    # reproducible dataset version and remains stable across cache/network reads.
    version = payload.payload_hash
    return NormalizedSourceData(
        pl.DataFrame(rows),
        version,
        payload.source_uri,
        payload.fetched_at,
        payload.payload_hash,
        SCHEDULE_TRANSFORMATION_VERSION,
        payload.from_cache,
    )


def _ffc_json(payload: SourcePayload) -> dict[str, Any]:
    try:
        value = json.loads(payload.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("Fantasy Football Calculator returned malformed JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("players"), list):
        raise ProviderError("Fantasy Football Calculator response has no players array")
    if value.get("status") not in (None, "Success"):
        raise ProviderError(
            "Fantasy Football Calculator did not return a successful response",
            {"provider_status": str(value.get("status"))},
        )
    return value


def _validate_ffc_meta(meta: Any, *, endpoint: str, league_size: int) -> None:
    if not isinstance(meta, dict):
        raise ProviderError("Fantasy Football Calculator response has no format metadata")
    try:
        teams = int(meta["teams"])
        response_type = str(meta["type"]).strip().casefold()
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError("Fantasy Football Calculator format metadata is malformed") from exc
    expected_type = {"ppr": "ppr", "half-ppr": "half-ppr", "standard": "standard"}[endpoint]
    if teams != league_size or response_type != expected_type:
        raise ProviderError(
            "Fantasy Football Calculator returned incompatible ADP data",
            {
                "expected_teams": league_size,
                "received_teams": teams,
                "expected_type": expected_type,
                "received_type": response_type,
            },
        )


def _normalize_team(value: Any) -> str:
    team = str(value or "").strip().upper()
    team = TEAM_ALIASES.get(team, team)
    if team not in NFL_TEAMS:
        raise InputError(
            "unknown_nfl_team", "nflverse schedule contains an unknown NFL team", {"team": team}
        )
    return team


def _finite_number(value: Any, row: int, field: str, *, required: bool) -> float | None:
    if value in (None, ""):
        if not required:
            return None
        raise ProviderError(
            "Fantasy Football Calculator returned invalid numeric player data",
            {"row": row, "field": field},
        )
    if isinstance(value, bool):
        number = math.nan
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = math.nan
    if not math.isfinite(number):
        raise ProviderError(
            "Fantasy Football Calculator returned invalid numeric player data",
            {"row": row, "field": field},
        )
    return number


def _nonnegative_integer(value: Any, row: int, field: str) -> int | None:
    if value in (None, ""):
        return None
    number = _finite_number(value, row, field, required=True)
    assert number is not None
    if number < 0 or not number.is_integer():
        raise ProviderError(
            "Fantasy Football Calculator returned invalid numeric player data",
            {"row": row, "field": field},
        )
    return int(number)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or 500 <= exc.response.status_code < 600
    )

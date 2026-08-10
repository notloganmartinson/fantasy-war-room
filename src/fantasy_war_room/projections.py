from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fantasy_war_room.errors import InputError
from fantasy_war_room.identity import normalize_name
from fantasy_war_room.models import ProjectionIssue, ProjectionSnapshot
from fantasy_war_room.repository import IntelligenceRepository
from fantasy_war_room.services import canonical_hash

CBS_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")
SCORING_CALCULATOR_VERSION = "1.1"
TEAM_ALIASES = {"JAC": "JAX"}


def cbs_files(season: str) -> dict[str, str]:
    if re.fullmatch(r"\d{4}", season) is None:
        raise InputError("invalid_projection_season", "CBS projection season must be four digits")
    return {position: f"{position.lower()}-{season}-ppr.html" for position in CBS_POSITIONS}


CBS_FILES = cbs_files("2026")

COMMON_SCHEMAS = {
    "QB": (
        "games",
        "passing_attempts",
        "passing_completions",
        "passing_yards",
        "passing_yards_per_game",
        "passing_touchdowns",
        "interceptions",
        "passer_rating",
        "rushing_attempts",
        "rushing_yards",
        "rushing_yards_per_attempt",
        "rushing_touchdowns",
        "fumbles_lost",
        "cbs_projected_points",
        "cbs_projected_points_per_game",
    ),
    "RB": (
        "games",
        "rushing_attempts",
        "rushing_yards",
        "rushing_yards_per_attempt",
        "rushing_touchdowns",
        "targets",
        "receptions",
        "receiving_yards",
        "receiving_yards_per_game",
        "receiving_yards_per_reception",
        "receiving_touchdowns",
        "fumbles_lost",
        "cbs_projected_points",
        "cbs_projected_points_per_game",
    ),
    "WR": (
        "games",
        "targets",
        "receptions",
        "receiving_yards",
        "receiving_yards_per_game",
        "receiving_yards_per_reception",
        "receiving_touchdowns",
        "rushing_attempts",
        "rushing_yards",
        "rushing_yards_per_attempt",
        "rushing_touchdowns",
        "fumbles_lost",
        "cbs_projected_points",
        "cbs_projected_points_per_game",
    ),
    "TE": (
        "games",
        "targets",
        "receptions",
        "receiving_yards",
        "receiving_yards_per_game",
        "receiving_yards_per_reception",
        "receiving_touchdowns",
        "fumbles_lost",
        "cbs_projected_points",
        "cbs_projected_points_per_game",
    ),
}
KICKER_SCHEMA = (
    "games",
    "field_goals_made",
    "field_goals_attempted",
    "longest_field_goal",
    "field_goals_made_1_19",
    "field_goals_attempted_1_19",
    "field_goals_made_20_29",
    "field_goals_attempted_20_29",
    "field_goals_made_30_39",
    "field_goals_attempted_30_39",
    "field_goals_made_40_49",
    "field_goals_attempted_40_49",
    "field_goals_made_50_plus",
    "field_goals_attempted_50_plus",
    "extra_points_made",
    "extra_points_attempted",
    "cbs_projected_points",
    "cbs_projected_points_per_game",
)
DST_SCHEMA = (
    "defensive_interceptions",
    "safeties",
    "sacks",
    "tackles",
    "defensive_fumbles_recovered",
    "forced_fumbles",
    "defensive_touchdowns",
    "points_allowed",
    "points_allowed_per_game",
    "passing_yards_allowed",
    "rushing_yards_allowed",
    "total_yards_allowed",
    "yards_allowed_per_game",
    "cbs_projected_points",
    "cbs_projected_points_per_game",
)


def import_cbs_projections(
    directory: Path,
    repository: IntelligenceRepository,
    source_version: str,
    league_id: str,
    observed_at: datetime | None = None,
    season: str = "2026",
) -> tuple[ProjectionSnapshot, bool, list[dict[str, Any]]]:
    at = observed_at or datetime.now(UTC)
    player_snapshot_id, league_snapshot_id, scoring = repository.projection_context(at, league_id)
    parsed_sources: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    for position, filename in cbs_files(season).items():
        source, rows = parse_cbs_projection_page(directory / filename, position, season)
        parsed_sources.append(source)
        all_rows.extend(rows)
    snapshot_id = str(uuid4())
    entries: list[dict[str, Any]] = []
    kicker_rows: list[dict[str, Any]] = []
    dst_rows: list[dict[str, Any]] = []
    issues: list[ProjectionIssue] = []
    for row in all_rows:
        resolution = {
            "player_name": row["source_player_name"],
            "normalized_name": normalize_name(row["source_player_name"]),
            "position": "DEF" if row["source_position"] == "DST" else row["position"],
            "team": row["team"],
        }
        if row["source_position"] == "DST":
            resolution["sleeper_id"] = row["team"]
        canonical_id, status, reason, candidates, method = repository.resolve_player(resolution, at)
        known, exact, completeness, missing = calculate_league_points(row, scoring)
        entry = {
            **{field: row.get(field) for field in _common_entry_fields()},
            "canonical_player_id": canonical_id,
            "league_known_component_points": known,
            "league_projected_points": exact,
            "scoring_completeness": completeness,
            "unprojected_scoring_keys": missing,
            "match_status": status,
            "match_method": method,
            "raw_payload": row["raw_payload"],
            "schema_version": "1.0",
        }
        entries.append(entry)
        if row["source_position"] == "K":
            kicker_rows.append(row)
        elif row["source_position"] == "DST":
            dst_rows.append(row)
        if status != "matched":
            issues.append(
                ProjectionIssue(
                    projection_snapshot_id=snapshot_id,
                    source_position=row["source_position"],
                    source_row_number=row["source_row_number"],
                    source_player_name=row["source_player_name"],
                    source_team=row["team"],
                    match_status=status,
                    reason=reason,
                    candidate_player_ids=candidates,
                    raw_payload=row["raw_payload"],
                )
            )
    content = {
        "source_version": source_version,
        "sources": [
            {"position": source["position"], "hash": source["source_page_hash"]}
            for source in parsed_sources
        ],
        "rows": [row["raw_payload"] for row in all_rows],
    }
    snapshot = ProjectionSnapshot(
        projection_snapshot_id=snapshot_id,
        source="cbs",
        source_version=source_version,
        season=season,
        horizon="full_season",
        source_scoring_format="ppr",
        observed_at=at,
        imported_at=datetime.now(UTC),
        payload_hash=canonical_hash(content),
        total_row_count=len(entries),
        matched_row_count=sum(row["match_status"] == "matched" for row in entries),
        unresolved_row_count=sum(row["match_status"] == "unresolved" for row in entries),
        ambiguous_row_count=sum(row["match_status"] == "ambiguous" for row in entries),
        player_snapshot_id=player_snapshot_id,
        league_snapshot_id=league_snapshot_id,
        scoring_settings_hash=canonical_hash(scoring),
        scoring_settings=scoring,
        scoring_calculator_version=SCORING_CALCULATOR_VERSION,
    )
    persisted_snapshot, created = repository.insert_projection_snapshot(
        snapshot, parsed_sources, entries, kicker_rows, dst_rows, issues
    )
    return (
        persisted_snapshot,
        created,
        repository.projection_summary_by_position(persisted_snapshot.projection_snapshot_id),
    )


def parse_cbs_projection_page(
    path: Path, expected_position: str, expected_season: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise InputError(
            "projection_file_not_found", f"CBS projection page does not exist: {resolved}"
        )
    raw_bytes = resolved.read_bytes()
    text = raw_bytes.decode("utf-8")
    title_match = re.search(r"<title>(.*?)</title>", text, re.DOTALL | re.IGNORECASE)
    title = html.unescape(title_match.group(1).strip()) if title_match else ""
    selected = [
        _clean_html(value)
        for value in re.findall(r"<option[^>]*selected[^>]*>(.*?)</option>", text, re.DOTALL)
    ]
    if expected_season not in title or f"{expected_season} Projections" not in selected:
        raise InputError(
            "invalid_projection_season",
            f"{resolved.name} is not a {expected_season} projection page",
        )
    if expected_position not in selected or "PPR" not in selected:
        raise InputError(
            "invalid_projection_page",
            f"{resolved.name} has unexpected position or scoring controls",
        )
    table_match = re.search(
        r'<table class="TableBase-table".*?</table>', text, re.DOTALL | re.IGNORECASE
    )
    if table_match is None:
        raise InputError(
            "projection_table_not_found", f"No CBS projection table in {resolved.name}"
        )
    table = table_match.group(0)
    columns = _published_columns(table)
    schema = (
        DST_SCHEMA
        if expected_position == "DST"
        else KICKER_SCHEMA
        if expected_position == "K"
        else COMMON_SCHEMAS[expected_position]
    )
    if len(columns) != len(schema) + 1:
        raise InputError(
            "invalid_projection_columns",
            f"{resolved.name} has {len(columns)} projection columns; "
            f"expected {len(schema) + 1} for {expected_position}",
        )
    body_match = re.search(r"<tbody>(.*?)</tbody>", table, re.DOTALL | re.IGNORECASE)
    if body_match is None:
        raise InputError("projection_rows_not_found", f"No CBS projection rows in {resolved.name}")
    row_html = re.findall(r"<tr\b.*?</tr>", body_match.group(1), re.DOTALL | re.IGNORECASE)
    rows = [
        _parse_cbs_row(value, expected_position, index)
        for index, value in enumerate(row_html, start=1)
    ]
    return (
        {
            "position": expected_position,
            "original_filename": resolved.name,
            "source_page_hash": hashlib.sha256(raw_bytes).hexdigest(),
            "row_count": len(rows),
            "published_columns": columns,
        },
        rows,
    )


def calculate_league_points(
    row: dict[str, Any], scoring: dict[str, float]
) -> tuple[float, float | None, str, list[str]]:
    position = row["source_position"]
    if position in {"QB", "RB", "WR", "TE"}:
        mappings = {
            "pass_yd": "passing_yards",
            "pass_td": "passing_touchdowns",
            "pass_int": "interceptions",
            "rush_yd": "rushing_yards",
            "rush_td": "rushing_touchdowns",
            "rec": "receptions",
            "rec_yd": "receiving_yards",
            "rec_td": "receiving_touchdowns",
            "fum_lost": "fumbles_lost",
        }
        relevant = {
            key
            for key, value in scoring.items()
            if value and key.startswith(("pass", "rush", "rec", "fum", "bonus_"))
        }
    elif position == "K":
        mappings = {
            "fgm_0_19": "field_goals_made_1_19",
            "fgm_20_29": "field_goals_made_20_29",
            "fgm_30_39": "field_goals_made_30_39",
            "fgm_40_49": "field_goals_made_40_49",
            "xpm": "extra_points_made",
        }
        relevant = {key for key, value in scoring.items() if value and key.startswith(("fg", "xp"))}
    else:
        mappings = {
            "int": "defensive_interceptions",
            "safe": "safeties",
            "sack": "sacks",
            "fum_rec": "defensive_fumbles_recovered",
            "ff": "forced_fumbles",
            "def_td": "defensive_touchdowns",
        }
        relevant = {
            key
            for key, value in scoring.items()
            if value
            and (
                key in mappings
                or key.startswith(("pts_allow", "def_st", "st_"))
                or key == "blk_kick"
            )
        }
    known = 0.0
    applied: set[str] = set()
    for scoring_key, statistic in mappings.items():
        value = row.get(statistic)
        if value is not None and scoring.get(scoring_key):
            known += float(value) * scoring[scoring_key]
            applied.add(scoring_key)
    if position == "K":
        made = row.get("field_goals_made")
        attempted = row.get("field_goals_attempted")
        if made is not None and attempted is not None and scoring.get("fgmiss"):
            known += (float(attempted) - float(made)) * scoring["fgmiss"]
            applied.add("fgmiss")
        xpm, xpa = row.get("extra_points_made"), row.get("extra_points_attempted")
        if xpm is not None and xpa is not None and scoring.get("xpmiss"):
            known += (float(xpa) - float(xpm)) * scoring["xpmiss"]
            applied.add("xpmiss")
    missing = sorted(relevant - applied)
    known = round(known, 6)
    completeness = "complete" if not missing else "partial"
    return known, known if completeness == "complete" else None, completeness, missing


def _parse_cbs_row(row_html: str, position: str, row_number: int) -> dict[str, Any]:
    cells = re.findall(r"<td\b.*?</td>", row_html, re.DOTALL | re.IGNORECASE)
    schema = (
        DST_SCHEMA
        if position == "DST"
        else KICKER_SCHEMA
        if position == "K"
        else COMMON_SCHEMAS[position]
    )
    if len(cells) != len(schema) + 1:
        raise InputError(
            "invalid_projection_row",
            f"CBS {position} row {row_number} has {len(cells)} cells; expected {len(schema) + 1}",
        )
    team: str | None
    if position == "DST":
        code_match = re.search(r"/nfl/teams/([^/]+)/", cells[0])
        if code_match is None:
            raise InputError(
                "invalid_projection_team", f"CBS DST row {row_number} has no team code"
            )
        team = _normalize_team(code_match.group(1))
        name_match = re.search(r'<span class="TeamName">.*?<a[^>]*>(.*?)</a>', cells[0], re.DOTALL)
        source_name = _clean_html(name_match.group(1)) if name_match else team
        canonical_position = "DEF"
    else:
        long_match = re.search(r"CellPlayerName--long.*?<a[^>]*>(.*?)</a>", cells[0], re.DOTALL)
        team_match = re.search(
            r'CellPlayerName--long.*?CellPlayerName-team">\s*([^<]+)', cells[0], re.DOTALL
        )
        if long_match is None:
            raise InputError(
                "invalid_projection_player", f"CBS {position} row {row_number} has no player name"
            )
        source_name = _clean_html(long_match.group(1))
        team = _normalize_team(team_match.group(1).strip()) if team_match else None
        canonical_position = position
    values = {
        _field: _number(_clean_html(cell)) for _field, cell in zip(schema, cells[1:], strict=True)
    }
    raw_payload = {
        "source_player_name": source_name,
        "position": canonical_position,
        "team": team,
        **values,
    }
    return {
        "source_position": position,
        "source_row_number": row_number,
        "source_player_name": source_name,
        "position": canonical_position,
        "team": team,
        **values,
        "raw_payload": raw_payload,
    }


def _published_columns(table: str) -> list[dict[str, str | None]]:
    head = re.search(r'<tr class="TableBase-headTr">(.*?)</tr>', table, re.DOTALL)
    if head is None:
        return []
    result: list[dict[str, str | None]] = []
    for chunk in re.findall(r"<th\b.*?</th>", head.group(1), re.DOTALL):
        link = re.search(r"<a[^>]*>(.*?)</a>", chunk, re.DOTALL)
        tooltip = re.search(r'Tablebase-tooltipInner">\s*(.*?)\s*</div>', chunk, re.DOTALL)
        result.append(
            {
                "label": _clean_html(link.group(1) if link else chunk),
                "meaning": _clean_html(tooltip.group(1)) if tooltip else None,
            }
        )
    return result


def _common_entry_fields() -> tuple[str, ...]:
    return (
        "source_position",
        "source_row_number",
        "source_player_name",
        "position",
        "team",
        "games",
        "passing_attempts",
        "passing_completions",
        "passing_yards",
        "passing_yards_per_game",
        "passing_touchdowns",
        "interceptions",
        "passer_rating",
        "rushing_attempts",
        "rushing_yards",
        "rushing_yards_per_attempt",
        "rushing_touchdowns",
        "targets",
        "receptions",
        "receiving_yards",
        "receiving_yards_per_game",
        "receiving_yards_per_reception",
        "receiving_touchdowns",
        "fumbles_lost",
        "cbs_projected_points",
        "cbs_projected_points_per_game",
    )


def _clean_html(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", value)).split())


def _number(value: str) -> float | None:
    cleaned = value.replace(",", "").strip()
    if cleaned in {"", "-", "--", "–", "—"}:
        return None
    try:
        return float(cleaned)
    except ValueError as exc:
        raise InputError(
            "invalid_projection_number", f"Invalid CBS projection number: {value!r}"
        ) from exc


def _normalize_team(value: str) -> str:
    return TEAM_ALIASES.get(value.upper(), value.upper())

from __future__ import annotations

from collections import Counter
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from fantasy_war_room.decision.models import RecommendationInputs, RecommendationResult


class MarketModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class MarketWindow(MarketModel):
    earliest_pick: int | None = None
    latest_pick: int | None = None
    basis: str


class PlayerMarketContext(MarketModel):
    canonical_player_id: str
    player_name: str
    position: str
    current_overall_pick: int
    current_round: int
    user_next_selection: int
    user_following_selection: int | None
    picks_until_next_user_selection: int
    overall_adp: float | None
    adp_minus_current_pick: float | None
    adp_relative_to_next_user_pick: float | None
    manual_strategy_window: MarketWindow | None
    market_derived_window: MarketWindow | None
    effective_window: MarketWindow | None
    classification: Literal[
        "too_early",
        "market_reach",
        "market_aligned",
        "market_fall",
        "in_effective_window",
        "no_compatible_market_context",
    ]
    bye_week: int | None
    increases_concentrated_starter_bye_exposure: bool | None
    limitations: tuple[str, ...]


class MarketContext(MarketModel):
    schema_version: Literal["1.0"] = "1.0"
    model_version: Literal["market-context-1.0"] = "market-context-1.0"
    draft_snapshot_id: str
    adp_snapshot_id: str | None
    schedule_snapshot_id: str | None
    adp_provenance: dict[str, Any] | None
    schedule_provenance: dict[str, Any] | None
    roster_bye_counts: dict[int, int]
    starter_bye_counts: dict[int, int]
    players: tuple[PlayerMarketContext, ...]
    limitations: tuple[str, ...]


class OpponentDemand(MarketModel):
    schema_version: Literal["1.0"] = "1.0"
    model_version: Literal["opponent-demand-1.0"] = "opponent-demand-1.0"
    draft_snapshot_id: str
    adp_snapshot_id: str | None
    intervening_picks: int
    position_pressure: dict[str, int]
    opponent_details: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...] = (
        "Deterministic roster-need pressure; this does not predict manager intent",
    )


def build_market_context(
    result: RecommendationResult,
    inputs: RecommendationInputs,
    *,
    draft_snapshot_id: str,
    adp: dict[str, Any] | None,
    schedule: dict[str, Any] | None,
    manual_windows: dict[str, tuple[int | None, int | None]] | None = None,
) -> MarketContext:
    adp_by_id = adp["entries"] if adp else {}
    bye_by_team = schedule["entries"] if schedule else {}
    players = {p.canonical_player_id: p for p in inputs.projected_players}
    user_ids = {
        p.canonical_player_id
        for p in inputs.completed_picks
        if p.draft_slot == inputs.draft_slot and p.canonical_player_id
    }
    starter_ids = {row.canonical_player_id for row in result.current_roster.starters}
    roster_byes = Counter(bye_by_team.get(players[i].team or "") for i in user_ids if i in players)
    starter_byes = Counter(
        bye_by_team.get(players[i].team or "") for i in starter_ids if i in players
    )
    roster_byes.pop(None, None)
    starter_byes.pop(None, None)
    contexts: list[PlayerMarketContext] = []
    for candidate in sorted(
        result.candidates, key=lambda row: (row.recommendation_rank, row.canonical_player_id)
    ):
        value = adp_by_id.get(candidate.canonical_player_id)
        overall_adp = float(value["overall_adp"]) if value else None
        manual = (manual_windows or {}).get(candidate.canonical_player_id)
        manual_window = (
            MarketWindow(earliest_pick=manual[0], latest_pick=manual[1], basis="strategy_profile")
            if manual
            else None
        )
        market_window = (
            MarketWindow(
                earliest_pick=max(1, int(overall_adp) - inputs.team_count),
                latest_pick=int(overall_adp) + inputs.team_count,
                basis="compatible_adp_plus_or_minus_one_round",
            )
            if overall_adp is not None
            else None
        )
        effective = _effective_window(manual_window, market_window)
        classification = _classification(
            result.turn_context.next_overall_pick,
            result.turn_context.user_next_scheduled_pick,
            overall_adp,
            effective,
        )
        bye = bye_by_team.get(candidate.team or "")
        contexts.append(
            PlayerMarketContext(
                canonical_player_id=candidate.canonical_player_id,
                player_name=candidate.player_name,
                position=candidate.position,
                current_overall_pick=result.turn_context.next_overall_pick,
                current_round=result.turn_context.current_round,
                user_next_selection=result.turn_context.user_next_scheduled_pick,
                user_following_selection=result.turn_context.user_following_scheduled_pick,
                picks_until_next_user_selection=result.turn_context.opponent_picks_before_next_user_pick,
                overall_adp=overall_adp,
                adp_minus_current_pick=round(overall_adp - result.turn_context.next_overall_pick, 3)
                if overall_adp is not None
                else None,
                adp_relative_to_next_user_pick=round(
                    overall_adp - result.turn_context.user_next_scheduled_pick, 3
                )
                if overall_adp is not None
                else None,
                manual_strategy_window=manual_window,
                market_derived_window=market_window,
                effective_window=effective,
                classification=classification,
                bye_week=bye,
                increases_concentrated_starter_bye_exposure=(starter_byes.get(bye, 0) > 0)
                if bye is not None
                else None,
                limitations=(
                    "No compatible ADP snapshot; manual window is the only timing evidence",
                )
                if adp is None
                else (),
            )
        )
    return MarketContext(
        draft_snapshot_id=draft_snapshot_id,
        adp_snapshot_id=adp["snapshot"]["adp_snapshot_id"] if adp else None,
        schedule_snapshot_id=schedule["snapshot"]["schedule_snapshot_id"] if schedule else None,
        adp_provenance=adp["snapshot"] if adp else None,
        schedule_provenance=schedule["snapshot"] if schedule else None,
        roster_bye_counts=dict(sorted(cast(Counter[int], roster_byes).items())),
        starter_bye_counts=dict(sorted(cast(Counter[int], starter_byes).items())),
        players=tuple(contexts),
        limitations=("No compatible ADP snapshot",) if adp is None else (),
    )


def build_opponent_demand(
    result: RecommendationResult,
    inputs: RecommendationInputs,
    *,
    draft_snapshot_id: str,
    adp: dict[str, Any] | None,
) -> OpponentDemand:
    start, end = result.turn_context.next_overall_pick, result.turn_context.user_next_scheduled_pick
    picks = list(range(start, end))
    existing: dict[int, Counter[str]] = {
        slot: Counter() for slot in range(1, inputs.team_count + 1)
    }
    for pick in inputs.completed_picks:
        if pick.position and pick.draft_slot is not None:
            existing[pick.draft_slot][pick.position.upper()] += 1
    trusted = {r.canonical_player_id: r.overall_rank for r in inputs.expert_rankings}
    adp_rows = adp["entries"] if adp else {}
    available = sorted(
        result.candidates,
        key=lambda c: (
            adp_rows.get(c.canonical_player_id, {}).get("overall_adp", float("inf")),
            trusted.get(c.canonical_player_id, float("inf")),
            c.canonical_player_id,
        ),
    )
    used: set[str] = set()
    pressure: Counter[str] = Counter()
    details: list[dict[str, Any]] = []
    fixed = {
        "QB": inputs.roster.qb,
        "RB": inputs.roster.rb,
        "WR": inputs.roster.wr,
        "TE": inputs.roster.te,
    }
    for pick_no in picks:
        round_no = (pick_no - 1) // inputs.team_count + 1
        within = (pick_no - 1) % inputs.team_count + 1
        slot = within if round_no % 2 else inputs.team_count - within + 1
        needs = {p: max(0, required - existing[slot][p]) for p, required in fixed.items()}
        candidate = next(
            (c for c in available if c.canonical_player_id not in used and needs[c.position] > 0),
            None,
        )
        allocation = "fixed_starter_vacancy"
        if candidate is None:
            flex_positions = {"RB", "WR", "TE"}
            flex_used = max(
                0,
                sum(existing[slot][p] for p in flex_positions)
                - sum(fixed[p] for p in flex_positions),
            )
            if flex_used < inputs.roster.flex:
                candidate = next(
                    (
                        c
                        for c in available
                        if c.canonical_player_id not in used and c.position in flex_positions
                    ),
                    None,
                )
                allocation = "flex_vacancy"
        if candidate is None:
            candidate = next((c for c in available if c.canonical_player_id not in used), None)
            allocation = "bench_depth"
        if candidate is None:
            break
        used.add(candidate.canonical_player_id)
        existing[slot][candidate.position] += 1
        pressure[candidate.position] += 1
        details.append(
            {
                "pick_no": pick_no,
                "opponent_slot": slot,
                "position": candidate.position,
                "ordering_player_id": candidate.canonical_player_id,
                "allocation_basis": allocation,
            }
        )
    return OpponentDemand(
        draft_snapshot_id=draft_snapshot_id,
        adp_snapshot_id=adp["snapshot"]["adp_snapshot_id"] if adp else None,
        intervening_picks=len(picks),
        position_pressure={p: pressure[p] for p in ("QB", "RB", "WR", "TE")},
        opponent_details=tuple(details),
    )


def _effective_window(
    manual: MarketWindow | None, market: MarketWindow | None
) -> MarketWindow | None:
    if manual and market:
        starts = [x for x in (manual.earliest_pick, market.earliest_pick) if x is not None]
        ends = [x for x in (manual.latest_pick, market.latest_pick) if x is not None]
        return MarketWindow(
            earliest_pick=max(starts) if starts else None,
            latest_pick=min(ends) if ends else None,
            basis="hard_manual_gate_intersected_with_market",
        )
    return manual or market


def _classification(
    current: int, next_pick: int, adp: float | None, effective: MarketWindow | None
) -> Literal[
    "too_early",
    "market_reach",
    "market_aligned",
    "market_fall",
    "in_effective_window",
    "no_compatible_market_context",
]:
    if adp is None:
        return "no_compatible_market_context"
    if effective and effective.earliest_pick is not None and current < effective.earliest_pick:
        return "too_early"
    if (
        effective
        and (effective.latest_pick is None or current <= effective.latest_pick)
        and (effective.earliest_pick is None or current >= effective.earliest_pick)
    ):
        return "in_effective_window"
    if current < adp - 1:
        return "market_reach"
    if current > adp + 1:
        return "market_fall"
    return "market_aligned"

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from fantasy_war_room.decision.models import (
    CandidateExplanation,
    OffensivePosition,
    PortableMarketRecommendationInputs,
    PortableMarketRecommendationResult,
    RecommendationModelVersion,
    RecommendationPlayerInput,
    RecommendationResult,
)
from fantasy_war_room.decision.recommend import recommend, recommend_portable_market
from fantasy_war_room.decision.survival_models import SurvivalModelVersion
from fantasy_war_room.errors import InputError, NotFoundError
from fantasy_war_room.identity import alias_targets, normalize_name, strict_name
from fantasy_war_room.market import build_market_context, build_opponent_demand
from fantasy_war_room.mcp.repository import McpReadRepository
from fantasy_war_room.models import Snapshot
from fantasy_war_room.strategy.adjust import apply_strategy, validate_strategy_compatibility
from fantasy_war_room.strategy.load import profile_hash
from fantasy_war_room.strategy.models import StrategyProfile
from fantasy_war_room.strategy.presentation import limit_strategy_result
from fantasy_war_room.survival import survival_response

POSITIONS: tuple[OffensivePosition, ...] = ("QB", "RB", "WR", "TE")


class DraftCopilotService:
    def __init__(
        self,
        repository: McpReadRepository,
        *,
        draft_id: str,
        sleeper_user_id: str | None,
        draft_slot: int | None,
        default_source: str = "parlay-play-hybrid",
        default_model: RecommendationModelVersion = "trusted-board-1.1",
        strategy_profile: StrategyProfile | None = None,
        default_adp_source: str = "local-adp",
        default_schedule_source: str = "local-schedule",
    ) -> None:
        self.repository = repository
        self.draft_id = draft_id
        self.sleeper_user_id = sleeper_user_id
        self.draft_slot = draft_slot
        self.default_source = default_source
        self.default_model = default_model
        self.strategy_profile = strategy_profile
        self.default_adp_source = default_adp_source
        self.default_schedule_source = default_schedule_source

    def recommend_pick(
        self,
        *,
        model: RecommendationModelVersion | None,
        source: str | None,
        limit: int,
        as_of: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        selected_model = model or self.default_model
        if selected_model == "portable-market-1.0":
            if self.strategy_profile is not None:
                raise InputError(
                    "strategy_model_incompatible",
                    "Configured strategy requires a projection-backed recommendation model",
                )
            portable_result, snapshot, _ = self._portable_context(as_of)
            if not portable_result.candidates:
                raise InputError(
                    "insufficient_market_depth",
                    "No available players have resolved compatible FFC market data",
                )
            data = portable_result.model_dump(mode="json")
            data["candidates"] = data["candidates"][:limit]
            data["draft"] = _draft_identity(snapshot)
            return data, _portable_provenance(portable_result)
        result, snapshot, inputs, market, demand = self._market_context(
            model=model, source=source, as_of=as_of
        )
        if self.strategy_profile is not None:
            adjusted = limit_strategy_result(
                apply_strategy(
                    result,
                    inputs,
                    self.strategy_profile,
                    market_context=market.model_dump(mode="json")
                    if market.adp_snapshot_id
                    else None,
                ),
                limit,
            )
            data = adjusted.model_dump(mode="json")
        else:
            data = result.model_dump(mode="json")
            data["candidates"] = data["candidates"][:limit]
        data["draft"] = _draft_identity(snapshot)
        data["market_context"] = market.model_dump(mode="json")
        data["opponent_demand"] = demand.model_dump(mode="json")
        return data, _market_provenance(result, market)

    def get_market_context(self, *, as_of: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
        result, _, _, market, _ = self._market_context(model=None, source=None, as_of=as_of)
        return market.model_dump(mode="json"), _market_provenance(result, market)

    def get_opponent_demand(self, *, as_of: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
        result, _, _, market, demand = self._market_context(model=None, source=None, as_of=as_of)
        return demand.model_dump(mode="json"), _market_provenance(result, market)

    def simulate_next_pick_survival(
        self,
        *,
        canonical_player_ids: list[str],
        simulation_count: int,
        seed: int,
        model: SurvivalModelVersion,
        as_of: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        at = _decision_time(as_of)
        inputs, provenance = self.repository.read_survival(
            at,
            draft_id=self.draft_id,
            sleeper_user_id=self.sleeper_user_id,
            draft_slot=self.draft_slot,
            candidate_player_ids=tuple(canonical_player_ids),
            simulation_count=simulation_count,
            seed=seed,
            model_version=model,
            adp_source=self.default_adp_source,
        )
        response = survival_response(inputs, cast(dict[str, Any], provenance))
        return response.model_dump(mode="json"), response.provenance

    def get_draft_strategy(self, *, as_of: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.strategy_profile is None:
            raise InputError("strategy_not_configured", "No MCP strategy profile is configured")
        result, _, inputs, market, demand = self._market_context(
            model=self.strategy_profile.required_raw_model,
            source=self.strategy_profile.required_ranking_source,
            as_of=as_of,
        )
        adjusted = apply_strategy(
            result,
            inputs,
            self.strategy_profile,
            market_context=market.model_dump(mode="json") if market.adp_snapshot_id else None,
        )
        return {
            "profile": self.strategy_profile.model_dump(mode="json"),
            "profile_hash": profile_hash(self.strategy_profile),
            "profile_temporal_status": "current_explicit_profile",
            "targets": [target.model_dump(mode="json") for target in adjusted.targets],
            "reserved_position_targets": [
                target.model_dump(mode="json") for target in adjusted.reserved_position_targets
            ],
            "value_summary": adjusted.value_summary.model_dump(mode="json"),
            "market_context": market.model_dump(mode="json"),
            "opponent_demand": demand.model_dump(mode="json"),
            "roster_completion_required": adjusted.roster_completion_required,
            "actionable": adjusted.actionable,
            "directive": adjusted.directive.model_dump(mode="json") if adjusted.directive else None,
            "remaining_user_selections": adjusted.remaining_user_selections,
            "unfilled_required_positions": adjusted.unfilled_required_positions,
        }, _market_provenance(result, market)

    def get_draft_state(self, *, as_of: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.default_model == "portable-market-1.0":
            portable_result, snapshot, portable_inputs = self._portable_context(as_of)
            player_names = {
                player.canonical_player_id: player.player_name
                for player in portable_inputs.market_players
            }
            return _portable_draft_state(
                portable_result, snapshot, portable_inputs, player_names
            ), (_portable_provenance(portable_result))
        result, snapshot, inputs = self._context(model=None, source=None, as_of=as_of)
        player_names = {
            player.canonical_player_id: player.player_name for player in inputs.projected_players
        }
        recent: list[dict[str, Any]] = []
        canonical_by_sleeper = {
            pick.sleeper_player_id: pick.canonical_player_id
            for pick in inputs.completed_picks
            if pick.sleeper_player_id is not None
        }
        for pick in sorted(snapshot.picks, key=lambda row: int(row.get("pick_no") or 0))[-10:]:
            sleeper_id = str(pick.get("player_id")) if pick.get("player_id") is not None else None
            canonical_id = canonical_by_sleeper.get(sleeper_id)
            metadata = cast(
                dict[str, Any],
                pick.get("metadata") if isinstance(pick.get("metadata"), dict) else {},
            )
            source_name = " ".join(
                str(metadata.get(key) or "") for key in ("first_name", "last_name")
            ).strip()
            recent.append(
                {
                    "pick_no": pick.get("pick_no"),
                    "round": pick.get("round"),
                    "draft_slot": pick.get("draft_slot"),
                    "sleeper_player_id": sleeper_id,
                    "canonical_player_id": canonical_id,
                    "player_name": player_names.get(canonical_id or "") or source_name or None,
                }
            )
        turn = result.turn_context.model_dump(mode="json")
        data = {
            **_draft_identity(snapshot),
            **turn,
            "user_slot": turn["draft_slot"],
            "picks_until_next_user_selection": turn["opponent_picks_before_next_user_pick"],
            "recent_completed_picks": recent,
            "unresolved_pick_count": len(
                [pick for pick in result.current_roster.unmodeled_player_ids]
            ),
        }
        return data, _provenance(result)

    def get_my_roster(self, *, as_of: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
        result, _, inputs = self._context(model=None, source=None, as_of=as_of)
        players = {player.canonical_player_id: player for player in inputs.projected_players}
        drafted_ids = {
            pick.canonical_player_id
            for pick in inputs.completed_picks
            if pick.draft_slot == inputs.draft_slot and pick.canonical_player_id is not None
        }
        starters = result.current_roster.starters
        starter_ids = {assignment.canonical_player_id for assignment in starters}
        bench = [
            _player_summary(players[player_id])
            for player_id in sorted(drafted_ids - starter_ids)
            if player_id in players
        ]
        counts = {position: 0 for position in POSITIONS}
        for player_id in drafted_ids:
            player = players.get(player_id)
            if player is not None:
                counts[player.position] += 1
        unresolved_items = [
            {
                "pick_no": pick.pick_no,
                "sleeper_player_id": pick.sleeper_player_id,
                "canonical_player_id": pick.canonical_player_id,
            }
            for pick in inputs.completed_picks
            if pick.draft_slot == inputs.draft_slot
            and (
                pick.canonical_player_id is None
                or pick.canonical_player_id in result.current_roster.unmodeled_player_ids
            )
        ]
        data = {
            "allocator_version": result.current_roster.allocator_version,
            "starters": [assignment.model_dump(mode="json") for assignment in starters],
            "flex_assignments": [
                assignment.model_dump(mode="json")
                for assignment in starters
                if assignment.slot.startswith("FLEX")
            ],
            "bench": bench,
            "vacancies": result.current_roster.vacancies,
            "position_counts": counts,
            "projected_starting_lineup": result.current_roster.starting_lineup_projection,
            "unresolved_roster_items": unresolved_items,
        }
        return data, _provenance(result)

    def get_available_players(
        self,
        *,
        position: OffensivePosition | None,
        limit: int,
        as_of: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.default_model == "portable-market-1.0":
            portable_result, _, _ = self._portable_context(as_of)
            portable_candidates = [
                candidate
                for candidate in portable_result.candidates
                if position is None or candidate.position == position
            ]
            return {
                "position": position,
                "projection_backed": False,
                "recommendation_model_version": portable_result.recommendation_model_version,
                "players": [
                    {**candidate.model_dump(mode="json"), "availability": "available"}
                    for candidate in portable_candidates[:limit]
                ],
                "excluded_candidate_counts": portable_result.excluded_candidate_counts,
                "limitations": portable_result.limitations,
            }, _portable_provenance(portable_result)
        result, _, _ = self._context(model=None, source=None, as_of=as_of)
        candidates = [
            candidate
            for candidate in result.candidates
            if position is None or candidate.position == position
        ]
        data = {
            "position": position,
            "players": [_available_candidate(candidate) for candidate in candidates[:limit]],
            "excluded_candidate_counts": result.excluded_candidate_counts,
            "model_specification": result.model_specification.model_dump(mode="json"),
        }
        return data, _provenance(result)

    def compare_players(
        self, *, players: list[str], as_of: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if len(players) != 2:
            raise InputError("invalid_player_count", "compare_players requires exactly two players")
        result, _, inputs = self._context(model=None, source=None, as_of=as_of)
        resolved = [_resolve_player(selector, inputs.projected_players) for selector in players]
        candidate_by_id = {
            candidate.canonical_player_id: candidate for candidate in result.candidates
        }
        drafted = {
            pick.canonical_player_id: pick
            for pick in inputs.completed_picks
            if pick.canonical_player_id is not None
        }
        ranking_by_id = {row.canonical_player_id: row for row in inputs.expert_rankings}
        replacement = {row.position: row for row in result.replacement_levels}
        compared: list[dict[str, Any]] = []
        for player in resolved:
            candidate = candidate_by_id.get(player.canonical_player_id)
            drafted_pick = drafted.get(player.canonical_player_id)
            ranking = ranking_by_id.get(player.canonical_player_id)
            projection = _projection(player)
            replacement_value = replacement[player.position].replacement_projection
            compared.append(
                {
                    **_player_summary(player),
                    "availability": "drafted" if drafted_pick else "available",
                    "drafted_pick": drafted_pick.model_dump(mode="json") if drafted_pick else None,
                    "trusted_overall_rank": ranking.overall_rank if ranking else None,
                    "trusted_positional_rank": ranking.positional_rank if ranking else None,
                    "analyst_tier": ranking.tier if ranking else None,
                    "vorp": round(projection - replacement_value, 6)
                    if projection is not None and replacement_value is not None
                    else None,
                    "scarcity": candidate.scarcity.model_dump(mode="json") if candidate else None,
                    "roster_effect": candidate.roster_effect.model_dump(mode="json")
                    if candidate
                    else None,
                    "recommendation_score": candidate.recommendation_score if candidate else None,
                    "recommendation_not_applicable_reason": (
                        "player_already_drafted" if drafted_pick else "player_not_scored"
                    )
                    if candidate is None
                    else None,
                }
            )
        return {"players": compared}, _provenance(result)

    def get_position_outlook(
        self, *, position: OffensivePosition | None, as_of: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        result, _, inputs = self._context(model=None, source=None, as_of=as_of)
        positions = (position,) if position is not None else POSITIONS
        outlooks: list[dict[str, Any]] = []
        for selected in positions:
            candidates = [row for row in result.candidates if row.position == selected]
            starter_count = sum(row.vorp >= 0 for row in candidates)
            flags: list[dict[str, Any]] = []
            if starter_count == 0:
                flags.append({"flag": "exhausted", "evidence": {"starter_caliber_count": 0}})
            elif starter_count <= inputs.team_count:
                flags.append(
                    {"flag": "thinning", "evidence": {"starter_caliber_count": starter_count}}
                )
            elif starter_count >= 2 * inputs.team_count:
                flags.append({"flag": "deep", "evidence": {"starter_caliber_count": starter_count}})
            top_tier = getattr(candidates[0], "trusted_tier", None) if candidates else None
            tier_window = [
                getattr(row, "trusted_tier", None) for row in candidates[1 : 1 + inputs.team_count]
            ]
            if top_tier is None:
                sharp_tier: dict[str, Any] = {
                    "status": "unavailable",
                    "reason": "top_player_has_no_trusted_tier",
                }
            else:
                sharp_tier = {
                    "status": "available",
                    "value": top_tier not in [tier for tier in tier_window if tier is not None],
                }
                if sharp_tier["value"]:
                    flags.append(
                        {
                            "flag": "sharp_tier_drop",
                            "evidence": {"top_tier": top_tier, "following_tiers": tier_window},
                        }
                    )
            if candidates and candidates[0].scarcity_component.normalized_value >= 0.75:
                flags.append(
                    {
                        "flag": "projection_cliff",
                        "evidence": {
                            "scarcity_percentile": candidates[
                                0
                            ].scarcity_component.normalized_value,
                            "threshold": 0.75,
                        },
                    }
                )
            outlooks.append(
                {
                    "position": selected,
                    "top_available_players": [_available_candidate(row) for row in candidates[:10]],
                    "starter_caliber_count": starter_count,
                    "projected_depth_count": len(candidates),
                    "nearby_trusted_tiers": [top_tier, *tier_window],
                    "sharp_tier_drop": sharp_tier,
                    "roster_vacancies": result.current_roster.vacancies,
                    "flags": flags,
                }
            )
        return {"outlooks": outlooks, "outlook_model_version": "position-outlook-1.0"}, _provenance(
            result
        )

    def _context(
        self,
        *,
        model: RecommendationModelVersion | None,
        source: str | None,
        as_of: str | None,
    ) -> tuple[RecommendationResult, Snapshot, Any]:
        inputs, snapshot = self._inputs(as_of, source)
        selected_model = model or self.default_model
        if self.strategy_profile is not None:
            validate_strategy_compatibility(inputs, self.strategy_profile, raw_model=selected_model)
        return recommend(inputs, selected_model), snapshot, inputs

    def _market_context(
        self, *, model: RecommendationModelVersion | None, source: str | None, as_of: str | None
    ) -> tuple[RecommendationResult, Snapshot, Any, Any, Any]:
        at = _decision_time(as_of)
        inputs, snapshot, adp, schedule = self.repository.read_with_market(
            at,
            draft_id=self.draft_id,
            sleeper_user_id=self.sleeper_user_id,
            draft_slot=self.draft_slot,
            ranking_source=source or self.default_source,
            adp_source=self.default_adp_source,
            schedule_source=self.default_schedule_source,
        )
        selected_model = model or self.default_model
        if self.strategy_profile is not None:
            validate_strategy_compatibility(inputs, self.strategy_profile, raw_model=selected_model)
        result = recommend(inputs, selected_model)
        player_by_name = {
            normalize_name(player.player_name): player.canonical_player_id
            for player in inputs.projected_players
        }
        windows: dict[str, tuple[int | None, int | None]] = {}
        if self.strategy_profile:
            for target in self.strategy_profile.targets:
                player_id = player_by_name.get(normalize_name(target.player_name))
                if player_id:
                    earliest = (
                        (target.earliest_round - 1) * inputs.team_count + 1
                        if target.earliest_round
                        else None
                    )
                    latest = (
                        target.latest_round * inputs.team_count if target.latest_round else None
                    )
                    windows[player_id] = (earliest, latest)
        market = build_market_context(
            result,
            inputs,
            draft_snapshot_id=snapshot.snapshot_id,
            adp=cast(Any, adp),
            schedule=cast(Any, schedule),
            manual_windows=windows,
        )
        demand = build_opponent_demand(
            result, inputs, draft_snapshot_id=snapshot.snapshot_id, adp=cast(Any, adp)
        )
        return result, snapshot, inputs, market, demand

    def _inputs(self, as_of: str | None, source: str | None) -> tuple[Any, Snapshot]:
        at = _decision_time(as_of)
        return self.repository.read(
            at,
            draft_id=self.draft_id,
            sleeper_user_id=self.sleeper_user_id,
            draft_slot=self.draft_slot,
            ranking_source=source or self.default_source,
        )

    def _portable_context(
        self, as_of: str | None
    ) -> tuple[
        PortableMarketRecommendationResult,
        Snapshot,
        PortableMarketRecommendationInputs,
    ]:
        inputs, snapshot = self.repository.read_portable(
            _decision_time(as_of),
            draft_id=self.draft_id,
            sleeper_user_id=self.sleeper_user_id,
            draft_slot=self.draft_slot,
        )
        return recommend_portable_market(inputs), snapshot, inputs


def _decision_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InputError("invalid_timestamp", "as_of must be a valid ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise InputError("timezone_required", "as_of must include a timezone")
    return parsed.astimezone(UTC)


def _draft_identity(snapshot: Snapshot) -> dict[str, Any]:
    settings = snapshot.draft.get("settings")
    return {
        "draft_id": snapshot.draft_id,
        "status": snapshot.draft.get("status"),
        "draft_type": snapshot.draft.get("type"),
        "season": snapshot.draft.get("season"),
        "team_count": settings.get("teams") if isinstance(settings, dict) else None,
        "draft_snapshot_id": snapshot.snapshot_id,
        "draft_observed_at": snapshot.observed_at.isoformat(),
    }


def _provenance(result: RecommendationResult) -> dict[str, Any]:
    return {
        **result.provenance.model_dump(mode="json"),
        "decision_at": result.decision_at.isoformat(),
        "model_specification": result.model_specification.model_dump(mode="json"),
    }


def _portable_provenance(result: PortableMarketRecommendationResult) -> dict[str, Any]:
    return {
        **result.provenance.model_dump(mode="json"),
        "decision_at": result.decision_at.isoformat(),
        "recommendation_model_version": result.recommendation_model_version,
        "projection_backed": False,
    }


def _portable_draft_state(
    result: PortableMarketRecommendationResult,
    snapshot: Snapshot,
    inputs: PortableMarketRecommendationInputs,
    player_names: dict[str, str],
) -> dict[str, Any]:
    canonical_by_sleeper = {
        pick.sleeper_player_id: pick.canonical_player_id
        for pick in inputs.completed_picks
        if pick.sleeper_player_id is not None
    }
    recent: list[dict[str, Any]] = []
    for pick in sorted(snapshot.picks, key=lambda row: int(row.get("pick_no") or 0))[-10:]:
        sleeper_id = str(pick.get("player_id")) if pick.get("player_id") is not None else None
        canonical_id = canonical_by_sleeper.get(sleeper_id) if sleeper_id is not None else None
        metadata = cast(
            dict[str, Any],
            pick.get("metadata") if isinstance(pick.get("metadata"), dict) else {},
        )
        source_name = " ".join(
            str(metadata.get(key) or "") for key in ("first_name", "last_name")
        ).strip()
        recent.append(
            {
                "pick_no": pick.get("pick_no"),
                "round": pick.get("round"),
                "draft_slot": pick.get("draft_slot"),
                "sleeper_player_id": sleeper_id,
                "canonical_player_id": canonical_id,
                "player_name": player_names.get(canonical_id or "") or source_name or None,
            }
        )
    turn = result.turn_context.model_dump(mode="json")
    return {
        **_draft_identity(snapshot),
        **turn,
        "user_slot": turn["draft_slot"],
        "picks_until_next_user_selection": turn["opponent_picks_before_next_user_pick"],
        "recent_completed_picks": recent,
        "unresolved_pick_count": len(inputs.unresolved_roster_player_ids),
        "projection_backed": False,
    }


def _market_provenance(result: RecommendationResult, market: Any) -> dict[str, Any]:
    return {
        **_provenance(result),
        "adp_snapshot_id": market.adp_snapshot_id,
        "schedule_snapshot_id": market.schedule_snapshot_id,
        "market_context_model_version": market.model_version,
        "opponent_demand_model_version": "opponent-demand-1.0",
    }


def _projection(player: RecommendationPlayerInput) -> float | None:
    if player.league_projected_points is not None:
        return player.league_projected_points
    return player.league_known_component_points


def _player_summary(player: RecommendationPlayerInput) -> dict[str, Any]:
    return {
        **player.model_dump(mode="json"),
        "projection_baseline": _projection(player),
        "projection_value_kind": "exact"
        if player.league_projected_points is not None
        else "known_component",
    }


def _available_candidate(candidate: CandidateExplanation) -> dict[str, Any]:
    value = candidate.model_dump(mode="json")
    value["availability"] = "available"
    value["trusted_overall_rank"] = candidate.expert_overall_rank
    value["trusted_positional_rank"] = candidate.expert_positional_rank
    value["analyst_tier"] = getattr(candidate, "trusted_tier", None)
    return value


def _resolve_player(
    selector: str, players: tuple[RecommendationPlayerInput, ...]
) -> RecommendationPlayerInput:
    by_id = {player.canonical_player_id: player for player in players}
    if selector in by_id:
        return by_id[selector]
    strict = strict_name(selector)
    normalized = normalize_name(selector)
    matches = [
        player
        for player in players
        if strict_name(player.player_name) == strict
        or normalize_name(player.player_name) == normalized
    ]
    if not matches:
        matches = [
            player
            for player in players
            if normalize_name(player.player_name) in set(alias_targets(selector, player.position))
        ]
    unique = {player.canonical_player_id: player for player in matches}
    if not unique:
        raise NotFoundError(
            "Player was not found in the selected player universe",
            {"selector": selector},
            code="player_not_found",
        )
    if len(unique) > 1:
        raise InputError(
            "player_ambiguous",
            "Player selector matches more than one canonical identity",
            {"selector": selector, "candidate_player_ids": sorted(unique)},
        )
    return cast(RecommendationPlayerInput, next(iter(unique.values())))

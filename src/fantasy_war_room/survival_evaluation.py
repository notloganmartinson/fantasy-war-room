from __future__ import annotations

from collections import Counter
from typing import Any

from fantasy_war_room.decision.models import RecommendationModelVersion
from fantasy_war_room.decision.recommend import recommend
from fantasy_war_room.decision.survival import (
    simulate_next_pick_survival,
    survival_model_specification,
)
from fantasy_war_room.decision.survival_evaluation import (
    evaluate_model,
    select_evaluation_candidates,
)
from fantasy_war_room.decision.survival_evaluation_models import (
    EvaluationCandidatePolicy,
    EvaluationCandidatePopulationSummary,
    HistoricalCaseProvenance,
    LabeledHistoricalPrediction,
    SurvivalEvaluationReport,
)
from fantasy_war_room.decision.survival_models import (
    SurvivalCandidateInput,
    SurvivalModelVersion,
)
from fantasy_war_room.errors import FwrError
from fantasy_war_room.repository import IntelligenceRepository

MODELS: tuple[SurvivalModelVersion, ...] = (
    "adp-only-1.0",
    "adp-dispersion-1.0",
    "adp-dispersion-roster-1.0",
)


def evaluate_historical_survival(
    repository: IntelligenceRepository,
    *,
    draft_id: str,
    draft_slot: int,
    seed: int = 0,
    simulation_count: int = 5_000,
    adp_source: str | None = None,
    ranking_source: str | None = None,
    recommendation_model: RecommendationModelVersion = "baseline-1.0",
) -> SurvivalEvaluationReport:
    policy = EvaluationCandidatePolicy()
    snapshots = repository.historical_draft_snapshots(draft_id)
    latest_by_pick_count = {snapshot.pick_count: snapshot for snapshot in snapshots}
    feature_snapshots = tuple(latest_by_pick_count[key] for key in sorted(latest_by_pick_count))
    exclusions: Counter[str] = Counter()
    for reason in (
        "current_user_selection",
        "missing_compatible_adp",
        "insufficient_modeled_pool",
        "incomplete_future_interval",
        "unavailable_starting_candidate",
        "other",
    ):
        exclusions[reason] = 0
    labeled: dict[SurvivalModelVersion, list[LabeledHistoricalPrediction]] = {
        model: [] for model in MODELS
    }
    cases: list[HistoricalCaseProvenance] = []
    population: Counter[str] = Counter()
    evaluated_points = 0

    for feature in feature_snapshots:
        try:
            base_inputs, _ = repository.survival_inputs(
                feature.observed_at,
                draft_id=draft_id,
                league_id=None,
                sleeper_user_id=None,
                draft_slot=draft_slot,
                candidate_player_ids=(),
                simulation_count=simulation_count,
                seed=seed,
                model_version="adp-only-1.0",
                adp_source=adp_source,
            )
        except FwrError as error:
            exclusions[error.code] += 1
            continue
        if base_inputs.draft.draft_snapshot_id != feature.snapshot_id:
            exclusions["feature_snapshot_not_exact_at_cutoff"] += 1
            continue

        recommendation_ids: tuple[str, ...] = ()
        recommendation_snapshot_id: str | None = None
        try:
            recommendation_inputs = repository.recommendation_inputs(
                feature.observed_at,
                draft_id=draft_id,
                league_id=None,
                sleeper_user_id=None,
                draft_slot=draft_slot,
                ranking_source=ranking_source,
            )
            if recommendation_inputs.provenance.draft_snapshot_id != feature.snapshot_id:
                raise ValueError("recommendation feature snapshot mismatch")
            recommendation = recommend(recommendation_inputs, recommendation_model)
            recommendation_ids = tuple(
                row.canonical_player_id
                for row in recommendation.candidates[: policy.recommendation_limit]
            )
            recommendation_snapshot_id = recommendation.provenance.ranking_snapshot_id
        except (FwrError, ValueError):
            exclusions["recommendation_intelligence_unavailable"] += 1

        candidate_ids, recommendation_selected, adp_selected = select_evaluation_candidates(
            base_inputs, recommendation_ids, policy
        )
        population["total_available_players"] += len(base_inputs.available_players)
        population["adp_covered_available_players"] += sum(
            player.overall_adp is not None for player in base_inputs.available_players
        )
        population["eligible_evaluation_candidates"] += len(candidate_ids)
        population["recommendation_selected_count"] += len(recommendation_selected)
        population["adp_window_selected_count"] += len(adp_selected)
        population["overlap_count"] += len(set(recommendation_selected) & set(adp_selected))
        available = {player.canonical_player_id: player for player in base_inputs.available_players}
        population["missing_adp_eligible_count"] += sum(
            available[player_id].overall_adp is None for player_id in candidate_ids
        )
        if not candidate_ids:
            exclusions["no_eligible_candidates"] += 1
            continue

        results: dict[SurvivalModelVersion, dict[str, float]] = {}
        statuses: dict[SurvivalModelVersion, dict[str, str]] = {}
        for model in MODELS:
            model_inputs = base_inputs.model_copy(
                update={
                    "candidates": tuple(
                        SurvivalCandidateInput(canonical_player_id=player_id)
                        for player_id in candidate_ids
                    ),
                    "model_specification": survival_model_specification(model),
                }
            )
            result = simulate_next_pick_survival(model_inputs)
            statuses[model] = {row.canonical_player_id: row.status for row in result.candidates}
            results[model] = {
                row.canonical_player_id: row.simulated_availability_rate
                for row in result.candidates
                if row.status == "modeled" and row.simulated_availability_rate is not None
            }

        common_ids = set(candidate_ids).intersection(*(results[model] for model in MODELS))
        for player_id in candidate_ids:
            if player_id not in common_ids:
                reason = statuses["adp-only-1.0"].get(player_id, "other")
                exclusions[reason] += 1

        # Label access deliberately occurs only after every model prediction above is frozen.
        label_snapshot = repository.first_label_snapshot(
            draft_id,
            after=feature.observed_at,
            required_pick_count=base_inputs.draft.target_user_pick - 1,
        )
        if label_snapshot is None:
            exclusions["incomplete_future_interval"] += len(common_ids) or 1
            cases.append(
                _case_provenance(
                    feature, base_inputs, recommendation_snapshot_id, candidate_ids, (), None
                )
            )
            continue
        if not _label_extends_feature(feature.picks, label_snapshot.picks):
            exclusions["inconsistent_label_snapshot"] += len(common_ids) or 1
            continue
        picks = {
            int(row.get("pick_no", index)): row for index, row in enumerate(label_snapshot.picks, 1)
        }
        sleeper_to_canonical = {
            player.sleeper_player_id: player.canonical_player_id
            for player in base_inputs.available_players
            if player.sleeper_player_id is not None
        }
        canonical_to_sleeper = {
            canonical_id: sleeper_id for sleeper_id, canonical_id in sleeper_to_canonical.items()
        }
        actual_current = (
            sleeper_to_canonical.get(
                str(picks[base_inputs.draft.current_overall_pick].get("player_id"))
            )
            if base_inputs.draft.user_is_on_the_clock
            and base_inputs.draft.current_overall_pick in picks
            else None
        )
        if actual_current in common_ids:
            common_ids.remove(actual_current)
            exclusions["current_user_selection"] += 1
        opponent_selected_sleeper_ids = {
            str(picks[pick_no].get("player_id"))
            for pick_no in range(
                base_inputs.draft.simulation_start_pick, base_inputs.draft.target_user_pick
            )
            if pick_no in picks
        }
        opponent_selected = {
            player_id
            for player_id in common_ids
            if canonical_to_sleeper.get(player_id) in opponent_selected_sleeper_ids
        }
        for model in MODELS:
            for player_id in sorted(common_ids):
                labeled[model].append(
                    LabeledHistoricalPrediction(
                        feature_snapshot_id=feature.snapshot_id,
                        feature_cutoff=feature.observed_at,
                        current_overall_pick=base_inputs.draft.current_overall_pick,
                        target_user_pick=base_inputs.draft.target_user_pick,
                        canonical_player_id=player_id,
                        model_version=model,
                        simulated_availability_rate=results[model][player_id],
                        label_snapshot_id=label_snapshot.snapshot_id,
                        survived=player_id not in opponent_selected,
                    )
                )
        population["modeled_evaluation_candidates"] += len(common_ids)
        evaluated_points += 1
        cases.append(
            _case_provenance(
                feature,
                base_inputs,
                recommendation_snapshot_id,
                candidate_ids,
                tuple(sorted(common_ids)),
                label_snapshot,
                tuple(
                    sorted(
                        player_id for player_id in common_ids if player_id not in opponent_selected
                    )
                ),
                tuple(
                    sorted(player_id for player_id in common_ids if player_id in opponent_selected)
                ),
                actual_current,
            )
        )

    metrics = tuple(evaluate_model(model, labeled[model]) for model in MODELS)
    sufficient, recommended, reason = _recommend_default(
        metrics, evaluated_points, simulation_count
    )
    return SurvivalEvaluationReport(
        evaluation_policy=policy,
        draft_id=draft_id,
        draft_slot=draft_slot,
        seed=seed,
        simulation_count=simulation_count,
        eligible_decision_point_count=len(feature_snapshots),
        evaluated_decision_point_count=evaluated_points,
        candidate_population=EvaluationCandidatePopulationSummary(
            total_available_players=population["total_available_players"],
            adp_covered_available_players=population["adp_covered_available_players"],
            eligible_evaluation_candidates=population["eligible_evaluation_candidates"],
            modeled_evaluation_candidates=population["modeled_evaluation_candidates"],
            recommendation_selected_count=population["recommendation_selected_count"],
            adp_window_selected_count=population["adp_window_selected_count"],
            overlap_count=population["overlap_count"],
            missing_adp_eligible_count=population["missing_adp_eligible_count"],
            candidate_policy_version=policy.policy_version,
        ),
        models=metrics,
        exclusions=dict(sorted(exclusions.items())),
        cases=tuple(cases),
        evidence_sufficient_for_default_change=sufficient,
        recommended_default=recommended,
        recommendation_reason=reason,
        limitations=(
            "Historical labels are observational outcomes, not ground-truth probabilities.",
            "Candidate calibration applies only to decision-candidates-1.0.",
            "Recommendation intelligence is optional and omitted when unavailable at a cutoff.",
            "No coefficients or model variants are tuned by this evaluation.",
            "The default-change heuristic is descriptive and does not claim statistical "
            "significance.",
        ),
    )


def _case_provenance(
    feature: Any,
    inputs: Any,
    recommendation_snapshot_id: str | None,
    candidate_ids: tuple[str, ...],
    labeled_ids: tuple[str, ...],
    label: Any | None,
    survivor_ids: tuple[str, ...] = (),
    opponent_selected_ids: tuple[str, ...] = (),
    current_user_selection_id: str | None = None,
) -> HistoricalCaseProvenance:
    return HistoricalCaseProvenance(
        feature_snapshot_id=feature.snapshot_id,
        feature_cutoff=feature.observed_at,
        label_snapshot_id=label.snapshot_id if label else None,
        label_observed_at=label.observed_at if label else None,
        current_overall_pick=inputs.draft.current_overall_pick,
        simulation_start_pick=inputs.draft.simulation_start_pick,
        target_user_pick=inputs.draft.target_user_pick,
        adp_snapshot_id=inputs.adp.adp_snapshot_id,
        recommendation_snapshot_id=recommendation_snapshot_id,
        eligible_candidate_ids=candidate_ids,
        labeled_candidate_ids=labeled_ids,
        observed_survivor_ids=survivor_ids,
        observed_opponent_selected_ids=opponent_selected_ids,
        excluded_current_user_selection_id=current_user_selection_id,
    )


def _label_extends_feature(
    feature_picks: list[dict[str, Any]], label_picks: list[dict[str, Any]]
) -> bool:
    if len(label_picks) < len(feature_picks):
        return False
    for index, feature_pick in enumerate(feature_picks, 1):
        label_pick = label_picks[index - 1]
        if int(feature_pick.get("pick_no", index)) != int(label_pick.get("pick_no", index)):
            return False
        if str(feature_pick.get("player_id")) != str(label_pick.get("player_id")):
            return False
    return True


def _recommend_default(
    metrics: tuple[Any, ...], points: int, simulation_count: int
) -> tuple[bool, SurvivalModelVersion, str]:
    count = metrics[0].evaluated_candidate_count
    if simulation_count < 1_000:
        return (
            False,
            "adp-only-1.0",
            "Simulation count is too small for a default decision; retain the baseline.",
        )
    if points < 20 or count < 200:
        return (
            False,
            "adp-only-1.0",
            "Historical evidence is too small for a default change (requires at least 20 decision "
            "points and 200 common candidate cases).",
        )
    return (
        False,
        "adp-only-1.0",
        "This report covers one draft; correlated cases from one draft are not sufficient "
        "independent evidence for a production-default change.",
    )

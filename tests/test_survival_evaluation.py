from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from test_recommend_integration import BASE, _draft, _pick
from test_survival import _inputs
from test_survival_integration import _adp, _context, _survival_fixture
from typer.testing import CliRunner

from fantasy_war_room.cli import app
from fantasy_war_room.decision.survival_evaluation import (
    brier_score,
    calibration_buckets,
    log_loss,
    select_evaluation_candidates,
)
from fantasy_war_room.repository import IntelligenceRepository
from fantasy_war_room.survival_evaluation import evaluate_historical_survival


def test_metrics_are_hand_computable_and_bucket_boundaries_are_stable() -> None:
    values = ((0.25, False), (0.75, True))
    assert brier_score(values) == 0.0625
    assert log_loss(values) == -math.log(0.75)

    buckets = calibration_buckets(
        ((0.0, False), (0.2, True), (0.4, False), (0.8, True), (1.0, True))
    )
    assert [bucket.count for bucket in buckets] == [1, 1, 1, 0, 2]
    assert buckets[-1].upper_bound_inclusive is True


def test_candidate_policy_is_predecision_union_and_excludes_far_late_players() -> None:
    inputs = _inputs(target=44)
    selected, recommendations, adp_window = select_evaluation_candidates(inputs, ("p-59", "p-00"))
    assert "p-59" in recommendations
    assert "p-59" in selected  # recommendation-selected despite distant ADP
    assert "p-10" in adp_window
    assert "p-59" not in adp_window
    assert set(selected) == set(recommendations) | set(adp_window)


def test_historical_evaluation_freezes_features_then_labels_actual_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _evaluation_fixture(tmp_path)

    def forbid_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("historical survival evaluation must not perform network I/O")

    monkeypatch.setattr(httpx.Client, "send", forbid_network)
    report = evaluate_historical_survival(
        repository,
        draft_id="evaluation-draft",
        draft_slot=1,
        seed=42,
        simulation_count=100,
        adp_source="local-adp",
    )

    assert report.evaluated_decision_point_count >= 1
    assert {model.evaluated_candidate_count for model in report.models} == {
        report.candidate_population.modeled_evaluation_candidates
    }
    assert all(model.actual_survival_rate is not None for model in report.models)
    assert all(0 < model.actual_survival_rate < 1 for model in report.models)
    assert report.evidence_sufficient_for_default_change is False
    assert report.recommended_default == "adp-only-1.0"
    assert all(
        case.label_observed_at is None or case.feature_cutoff < case.label_observed_at
        for case in report.cases
    )
    feature_case = next(
        case for case in report.cases if case.feature_snapshot_id == "evaluation-feature"
    )
    assert feature_case.adp_snapshot_id == "early-adp"
    assert "c-qb-2" in feature_case.eligible_candidate_ids
    assert "c-qb-2" in feature_case.observed_opponent_selected_ids
    assert feature_case.observed_survivor_ids
    assert report.exclusions["current_user_selection"] == 1
    assert report.exclusions["incomplete_future_interval"] > 0
    on_clock_case = next(case for case in report.cases if case.current_overall_pick == 4)
    assert on_clock_case.excluded_current_user_selection_id == "c-qb-3"


def test_future_adp_and_future_draft_do_not_change_frozen_predictions(tmp_path: Path) -> None:
    repository = _evaluation_fixture(tmp_path)
    first = evaluate_historical_survival(
        repository,
        draft_id="evaluation-draft",
        draft_slot=1,
        seed=9,
        simulation_count=50,
        adp_source="local-adp",
    )
    _adp(
        repository, "later-leaking-adp", BASE + timedelta(hours=20), BASE + timedelta(hours=20), 50
    )
    repository.insert(
        _draft(
            "much-later-label",
            "evaluation-draft",
            BASE + timedelta(hours=20),
            _context(),
            [
                _pick(1, 1, "s-qb-1", "user-1"),
                _pick(2, 2, "s-rb-1", "user-2"),
                _pick(3, 2, "s-te-3", "user-2"),
                _pick(4, 1, "s-wr-4", "user-1"),
            ],
        )
    )
    repeated = evaluate_historical_survival(
        repository,
        draft_id="evaluation-draft",
        draft_slot=1,
        seed=9,
        simulation_count=50,
        adp_source="local-adp",
    )
    assert first.models == repeated.models
    first_frozen = next(
        case for case in first.cases if case.feature_snapshot_id == "evaluation-feature"
    )
    repeated_frozen = next(
        case for case in repeated.cases if case.feature_snapshot_id == "evaluation-feature"
    )
    assert first_frozen == repeated_frozen


def test_empty_history_is_conservative_and_cli_json_is_stable(tmp_path: Path) -> None:
    repository = IntelligenceRepository(tmp_path / "empty.duckdb")
    report = evaluate_historical_survival(
        repository, draft_id="missing", draft_slot=1, simulation_count=10
    )
    assert report.evaluated_decision_point_count == 0
    assert report.recommended_default == "adp-only-1.0"
    assert all(model.brier_score is None for model in report.models)

    populated = _evaluation_fixture(tmp_path / "populated")
    arguments = [
        "survival-evaluate",
        "--draft-id",
        "evaluation-draft",
        "--draft-slot",
        "1",
        "--simulations",
        "20",
        "--seed",
        "3",
        "--adp-source",
        "local-adp",
        "--db-path",
        str(populated.path),
        "--json",
    ]
    runner = CliRunner()
    first = runner.invoke(app, arguments)
    second = runner.invoke(app, arguments)
    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout


def _evaluation_fixture(tmp_path: Path) -> IntelligenceRepository:
    repository = _survival_fixture(tmp_path)
    _adp(repository, "early-adp", BASE + timedelta(minutes=30), BASE + timedelta(minutes=30), 0)
    context = _context()
    feature_picks = [
        _pick(1, 1, "s-qb-1", "user-1"),
        _pick(2, 2, "s-rb-1", "user-2"),
    ]
    repository.insert(
        _draft(
            "evaluation-feature",
            "evaluation-draft",
            BASE + timedelta(hours=2),
            context,
            feature_picks,
        )
    )
    repository.insert(
        _draft(
            "evaluation-label",
            "evaluation-draft",
            BASE + timedelta(hours=4),
            context,
            [*feature_picks, _pick(3, 2, "s-qb-2", "user-2")],
        )
    )
    repository.insert(
        _draft(
            "evaluation-on-clock-label",
            "evaluation-draft",
            BASE + timedelta(hours=5),
            context,
            [
                *feature_picks,
                _pick(3, 2, "s-qb-2", "user-2"),
                _pick(4, 1, "s-qb-3", "user-1"),
            ],
        )
    )
    return repository

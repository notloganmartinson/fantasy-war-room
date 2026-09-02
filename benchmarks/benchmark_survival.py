from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from fantasy_war_room.decision.models import RosterConfiguration
from fantasy_war_room.decision.survival import (
    owner_slot_for_pick,
    simulate_next_pick_survival,
    survival_model_specification,
)
from fantasy_war_room.decision.survival_models import (
    NextPickSurvivalInputs,
    OpponentRosterState,
    SurvivalAdpSnapshotInput,
    SurvivalCandidateInput,
    SurvivalCompletedPick,
    SurvivalDraftContext,
    SurvivalPlayerInput,
)


def fixture(simulation_count: int) -> NextPickSurvivalInputs:
    now = datetime(2026, 8, 17, 20, tzinfo=UTC)
    return NextPickSurvivalInputs(
        decision_at=now,
        draft=SurvivalDraftContext(
            draft_snapshot_id="benchmark-draft",
            draft_id="benchmark",
            observed_at=now,
            payload_hash="benchmark-draft-hash",
            team_count=10,
            round_count=15,
            user_draft_slot=4,
            current_overall_pick=37,
            user_is_on_the_clock=True,
            simulation_start_pick=38,
            target_user_pick=44,
            intervening_opponent_pick_count=6,
        ),
        adp=SurvivalAdpSnapshotInput(
            adp_snapshot_id="benchmark-adp",
            source="synthetic",
            source_version="1",
            observed_at=now,
            imported_at=now,
            payload_hash="benchmark-adp-hash",
            season="2026",
            league_size=10,
            scoring_format="ppr",
        ),
        available_players=tuple(
            SurvivalPlayerInput(
                canonical_player_id=f"player-{index:03d}",
                position=("QB", "RB", "WR", "TE")[index % 4],
                overall_adp=float(index + 20),
                adp_sd=float(5 + index % 8),
                sample_size=100 + index,
            )
            for index in range(120)
        ),
        candidates=tuple(
            SurvivalCandidateInput(canonical_player_id=player_id)
            for player_id in ("player-020", "player-025", "player-030")
        ),
        completed_picks=tuple(
            SurvivalCompletedPick(
                pick_no=pick_no,
                draft_slot=owner_slot_for_pick(pick_no, 10),
                canonical_player_id=f"drafted-{pick_no:03d}",
                position=("QB", "RB", "WR", "TE")[pick_no % 4],
            )
            for pick_no in range(1, 37)
        ),
        opponent_rosters=tuple(
            OpponentRosterState(draft_slot=slot) for slot in range(1, 11) if slot != 4
        ),
        roster_configuration=RosterConfiguration(
            qb=1, rb=2, wr=2, te=1, flex=2, bench=6, k=1, defense=1
        ),
        simulation_count=simulation_count,
        seed=20260817,
        model_specification=survival_model_specification("adp-only-1.0"),
    )


def main() -> None:
    rows: list[dict[str, object]] = []
    for count in (1_000, 5_000, 10_000, 25_000):
        inputs = fixture(count)
        started = time.perf_counter()
        result = simulate_next_pick_survival(inputs)
        elapsed = time.perf_counter() - started
        rows.append(
            {
                "simulation_count": count,
                "intervening_pick_count": result.intervening_opponent_pick_count,
                "modeled_player_count": result.pool_coverage.modeled_available_players,
                "candidate_count": len(result.candidates),
                "elapsed_seconds": round(elapsed, 6),
                "simulated_availability_rates": {
                    row.canonical_player_id: row.simulated_availability_rate
                    for row in result.candidates
                },
            }
        )
    print(json.dumps(rows, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

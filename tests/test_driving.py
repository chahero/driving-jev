import json

import pytest

from driving_jev.app import Session
from driving_jev.engine import HZ, Drive
from driving_jev.policy import assess, choose, heuristic


def test_initial_state_is_serializable_and_seeded():
    a, b = Drive(42, 5), Drive(42, 5)
    try:
        assert json.dumps(a.state()) == json.dumps(b.state())
        for _ in range(HZ):
            a.tick("keep")
            b.tick("keep")
        assert a.state() == b.state()
        assert a.elapsed == 1
        assert 20 < a.distance < 30
    finally:
        a.close()
        b.close()


def test_lane_gaps_and_rear_closing_time():
    d = Drive()
    try:
        front, rear = d.traffic[:2]
        d.env.road.vehicles = [d.ego, front, rear]
        lane = int(d.ego.target_lane_index[2])
        front.position[:] = [d.ego.position[0] + 25, lane * 4]
        rear.position[:] = [d.ego.position[0] - 35, lane * 4]
        front.speed = 20
        rear.speed = 30
        front.heading = rear.heading = 0
        s = d.lanes()[lane]
        assert s["front_gap_m"] == 20
        assert s["rear_gap_m"] == 30
        assert s["front_ttc_s"] == 4
        assert s["rear_ttc_s"] == 6
    finally:
        d.close()


def test_invalid_action_has_no_physics_effect():
    d = Drive()
    try:
        state = d.state()
        with pytest.raises(ValueError):
            d.tick("teleport")
        assert state == d.state()
    finally:
        d.close()


def test_lane_changes_are_not_repeated_during_a_macro():
    d = Drive()
    try:
        d.tick("right")
        for _ in range(HZ - 1):
            d.tick("keep")
        assert int(d.ego.target_lane_index[2]) == 2
        assert d.lane_changes == 1
        assert d.elapsed == 1
    finally:
        d.close()


def test_macro_logs_actual_result_and_rejects_stale_state(tmp_path):
    s = Session(42, 1, "heuristic", "balanced", tmp_path)
    try:
        before = s.drive.state()
        decision = choose(before, "heuristic")
        s.begin(decision, before)
        while s.active:
            s.tick()
        assert s.drive.end_reason() == "Drive complete"
        assert s.decisions == 1
        with pytest.raises(ValueError):
            s.begin(decision, before)
    finally:
        s.close()
    rows = [json.loads(line) for line in s.path.read_text().splitlines()]
    assert rows[1]["before"]["elapsed_s"] == 0
    assert rows[1]["after"]["elapsed_s"] == 1
    assert rows[-1]["reason"] == "Drive complete"


def test_heuristic_brakes_when_surrounded():
    d = Drive()
    try:
        state = d.state()
        for lane in state["lanes"]:
            lane.update(front_gap_m=10, rear_gap_m=5, front_speed_kmh=54)
        assert heuristic(state) == "slower"
    finally:
        d.close()


@pytest.mark.parametrize("seed", [1, 42, 123])
def test_offline_policy_emits_only_legal_actions(seed):
    d = Drive(seed, 5)
    try:
        while not d.end_reason():
            decision = choose(d.state(), "heuristic")
            assert decision.action in d.available()
            for i in range(HZ):
                d.tick(decision.action if i == 0 else "keep")
                if d.end_reason():
                    break
        assert d.elapsed <= 5
        json.dumps(d.state())
    finally:
        d.close()


def test_assessment_distinguishes_nonclosing_from_adequate_headway():
    d = Drive()
    try:
        state = d.state()
        state["ego"].update(speed_kmh=72, target_speed_kmh=72)
        state["lanes"][1].update(front_gap_m=14.9, front_speed_kmh=73.7, front_ttc_s=None)
        state["lanes"][2].update(
            front_gap_m=52.1,
            front_speed_kmh=78,
            front_ttc_s=None,
            rear_gap_m=None,
            rear_ttc_s=None,
        )
        original = json.dumps(state)
        info = assess(state)["actions"]
        assert info["keep"]["front_headway_s"] == 0.74
        assert info["keep"]["warnings"]
        assert info["right"]["front_headway_s"] == 2.6
        assert info["right"]["front_gap_gain_m"] == 37.2
        assert info["right"]["warnings"] == []
        assert not info["faster"]["acceleration_margin_ok"]
        assert json.dumps(state) == original
    finally:
        d.close()


def test_assessment_rejects_close_or_fast_rear_and_anticipates_acceleration():
    d = Drive()
    try:
        state = d.state()
        state["lanes"][2].update(rear_gap_m=10, rear_ttc_s=None)
        assert "rear clearance below 15m" in assess(state)["actions"]["right"]["warnings"]
        state["lanes"][2].update(rear_gap_m=30, rear_ttc_s=2)
        assert "rear vehicle approaching within 3s" in assess(state)["actions"]["right"]["warnings"]
        state["lanes"][1].update(front_gap_m=60, front_speed_kmh=60)
        assert not assess(state)["actions"]["faster"]["acceleration_margin_ok"]
        state["lanes"][1].update(front_gap_m=80, front_speed_kmh=72)
        assert not assess(state)["actions"]["faster"]["acceleration_margin_ok"]
        state["lanes"][1].update(front_gap_m=None, front_speed_kmh=None)
        assert assess(state)["actions"]["faster"]["acceleration_margin_ok"]
        assert not assess(state, "cautious")["actions"]["faster"]["acceleration_margin_ok"]
        json.dumps(assess(state))
    finally:
        d.close()

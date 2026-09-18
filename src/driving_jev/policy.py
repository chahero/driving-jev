import math
import time
from dataclasses import asdict, dataclass


@dataclass
class Decision:
    action: str
    probabilities: dict
    confidence: float
    latency_ms: float
    source: str
    assessment: dict | None = None

    def to_dict(self):
        return asdict(self)


def heuristic(state, style="balanced"):
    ego = state["ego"]
    allowed = state["available_actions"]
    current = state["lanes"][ego["target_lane"] - 1]
    gap = current["front_gap_m"] if current["front_gap_m"] is not None else 150
    speed_mps = ego["speed_kmh"] / 3.6
    lead_slow = (
        current["front_speed_kmh"] is not None and current["front_speed_kmh"] < ego["speed_kmh"] + 3
    )
    candidates = []
    for action, delta in (("left", -1), ("right", 1)):
        if action not in allowed:
            continue
        lane = state["lanes"][ego["target_lane"] - 1 + delta]
        front = lane["front_gap_m"] if lane["front_gap_m"] is not None else 150
        rear = lane["rear_gap_m"] if lane["rear_gap_m"] is not None else 150
        if front > max(25, speed_mps * 1.5) and rear > max(15, lane["rear_closing_speed_mps"] * 3):
            candidates.append((front, action))
    if gap < max(40, speed_mps * 2.5) and lead_slow:
        if candidates and max(candidates)[0] > gap + 12:
            return max(candidates)[1]
        if "slower" in allowed and gap < max(25, speed_mps * 1.8):
            return "slower"
    desired = 90 if style == "cautious" else 108
    if ego["target_speed_kmh"] < desired and gap > max(40, speed_mps * 2.5) and "faster" in allowed:
        return "faster"
    return "keep"


def assess(state, style="balanced"):
    """Expose measured tradeoffs, without replacing or filtering the model's choice."""
    ego = state["ego"]
    speed = max(ego["speed_kmh"] / 3.6, 0.1)
    desired = 90 if style == "cautious" else 108
    headway = 2.5 if style == "cautious" else 2.0
    current = state["lanes"][ego["target_lane"] - 1]
    lanes = []
    for lane in state["lanes"]:
        gap, rear = lane["front_gap_m"], lane["rear_gap_m"]
        front_speed = lane["front_speed_kmh"]
        closing = max(0, speed - front_speed / 3.6) if front_speed is not None else 0
        warnings = []
        if gap is not None and gap < speed * headway:
            warnings.append("front headway below preferred margin")
        if lane["front_ttc_s"] is not None and lane["front_ttc_s"] < 6:
            warnings.append("closing on front vehicle: act early")
        if rear is not None and rear < 15:
            warnings.append("rear clearance below 15m")
        if lane["rear_ttc_s"] is not None and lane["rear_ttc_s"] < 3:
            warnings.append("rear vehicle approaching within 3s")
        lanes.append(
            {
                "lane": lane["lane"],
                "front_headway_s": round(gap / speed, 2) if gap is not None else None,
                "front_gap_in_3s_at_current_speeds_m": round(gap - closing * 3, 1)
                if gap is not None
                else None,
                "front_gap_gain_m": round(gap - current["front_gap_m"], 1)
                if gap is not None and current["front_gap_m"] is not None
                else None,
                "lead_speed_gain_kmh": round(front_speed - current["front_speed_kmh"], 1)
                if front_speed is not None and current["front_speed_kmh"] is not None
                else None,
                "warnings": warnings,
            }
        )
    actions = {}
    for action in state["available_actions"]:
        target = ego["target_lane"] + {"left": -1, "right": 1}.get(action, 0)
        observation = state["lanes"][target - 1]
        details = dict(lanes[target - 1])
        if action == "faster":
            # HighwayEnv chooses a speed index from actual speed, not previous target.
            next_speed = min(108, (round((ego["speed_kmh"] - 54) / 18) + 1) * 18 + 54)
            gap = observation["front_gap_m"]
            lead = observation["front_speed_kmh"]
            details["next_target_speed_kmh"] = next_speed
            details["front_headway_at_next_speed_s"] = (
                round(gap / (next_speed / 3.6), 2) if gap is not None else None
            )
            closing = (next_speed - lead) / 3.6 if lead is not None else 0
            details["front_ttc_at_next_speed_s"] = (
                round(gap / closing, 2) if gap is not None and closing > 0.1 else None
            )
            details["acceleration_margin_ok"] = (
                next_speed <= desired
                and (gap is None or gap - max(0, closing) * 3 >= next_speed / 3.6 * (headway + 0.5))
                and (closing <= 0.1 or gap / closing >= 8)
            )
        actions[action] = details
    return {
        "version": "headway-v3",
        "preferred_front_headway_s": headway,
        "desired_speed_kmh": desired,
        "current_speed_below_desired_kmh": round(desired - ego["speed_kmh"], 1),
        "actions": actions,
        "limits": "Measured margins and constant-speed estimates, not safety guarantees. "
        "Null gaps mean no car observed within 150m. Other cars can change lanes. "
        "Warnings describe the destination lane before taking the action; slowing can improve them. "
        "Acceleration requires preferred headway plus 0.5s after 3s at the next speed, "
        "and at least 8s TTC, to avoid alternating acceleration and braking.",
    }


def choose(state, policy="jev", style="balanced"):
    if policy == "heuristic":
        return Decision(heuristic(state, style), {}, 0, 0, "heuristic")
    from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient

    descriptions = {
        "left": "Set target lane one lane left. Check the FRONT and REAR gap and TTC there.",
        "right": "Set target lane one lane right. Check the FRONT and REAR gap and TTC there.",
        "keep": "Maintain current target speed and target lane. Continue any ongoing lane change.",
        "faster": "Raise target speed by 18 km/h, up to 108 km/h. No lane change.",
        "slower": "Lower target speed by 18 km/h, down to 54 km/h. No lane change.",
    }
    assessment = assess(state, style)
    started = time.perf_counter()
    with TypeSafeClient(timeout=15, retry=RetryPolicy(max_retries=0)) as client:
        response = client.system_one(
            state={**state, "driving_style": style, "assessment": assessment},
            questions={
                "maneuver": Choice(
                    instructions=(
                        "Choose a highway maneuver using assessment.actions and raw lane observations. "
                        "First preserve clearance, then make useful progress toward desired_speed_kmh. "
                        "KEEP maintains speed; it does NOT follow or brake for the lead car. "
                        "Do not wait for TTC <3s: if front headway is below preferred and closing, "
                        "slow down now unless an adjacent lane has clear front/rear margins. "
                        "A null TTC means not closing, NOT sufficient following distance. "
                        "When current headway is short, prefer an adjacent lane with no warnings "
                        "and substantially more front space (at least 15m gain). "
                        "Also consider changing lanes to pass a slower lead car when the adjacent "
                        "lane has no warnings and a higher lead speed. Compare BOTH directions. "
                        "If no better lane is available and clearance is shrinking, choose slower. "
                        "If below desired speed and acceleration_margin_ok is true, prefer faster "
                        "over keep. Do not accelerate into inadequate clearance. "
                        "During a lane change consider both occupied and target lanes; keep continues it. "
                        "Avoid pointless lane switching, but do not treat staying in lane as always safer. "
                        "The engine only steers and tracks speed; you choose every maneuver."
                    ),
                    criteria={
                        a: descriptions[a]
                        + " Measured assessment: "
                        + str(assessment["actions"][a])
                        for a in state["available_actions"]
                    },
                )
            },
        )
    a = response.choices["maneuver"]
    action = str(a.choice)
    probabilities = {str(k): float(v) for k, v in a.probabilities.items()}
    confidence = float(a.confidence)
    if (
        action not in state["available_actions"]
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ValueError("Invalid action response")
    if any(
        k not in state["available_actions"] or not math.isfinite(v) or not 0 <= v <= 1
        for k, v in probabilities.items()
    ):
        raise ValueError("Invalid action probabilities")
    return Decision(
        action, probabilities, confidence, (time.perf_counter() - started) * 1000, "jev", assessment
    )

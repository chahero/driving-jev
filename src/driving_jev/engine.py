from __future__ import annotations

from highway_env.envs.highway_env import HighwayEnv

HZ = 30
ACTIONS = {"left": 0, "keep": 1, "right": 2, "faster": 3, "slower": 4}
LABELS = {
    "left": "Change left",
    "keep": "Keep course",
    "right": "Change right",
    "faster": "Speed up",
    "slower": "Slow down",
}


class Drive:
    def __init__(self, seed=42, duration=40):
        self.seed, self.duration = seed, duration
        self.config = {
            "lanes_count": 4,
            "vehicles_count": 35,
            "vehicles_density": 1.0,
            "initial_lane_id": 1,
            "duration": duration,
            "offroad_terminal": True,
            "simulation_frequency": HZ,
            "policy_frequency": HZ,
            "action": {"type": "DiscreteMetaAction", "target_speeds": [15, 20, 25, 30]},
        }
        self.env = HighwayEnv(config=self.config)
        self.env.reset(seed=seed)
        self.start_x = float(self.ego.position[0])
        self.initial_ahead = {id(v) for v in self.traffic if v.position[0] > self.start_x + 5}
        self.passed = set()
        self.ticks = self.hard_brakes = self.lane_changes = 0
        self.braking = False
        self.terminated = self.truncated = False

    @property
    def ego(self):
        return self.env.vehicle

    @property
    def traffic(self):
        return self.env.road.vehicles[1:]

    @property
    def elapsed(self):
        return self.ticks / HZ

    @property
    def distance(self):
        return max(0, float(self.ego.position[0]) - self.start_x)

    @property
    def changing_lane(self):
        return bool(abs(float(self.ego.position[1]) - self.ego.target_lane_index[2] * 4) > 0.35)

    def available(self):
        indices = set(self.env.action_type.get_available_actions())
        return [
            name
            for name, index in ACTIONS.items()
            if index in indices and not (self.changing_lane and name in ("left", "right"))
        ]

    def lanes(self):
        ego = self.ego
        speed = float(ego.velocity[0])
        result = []
        for lane in range(4):
            neighbors = [
                v
                for v in self.traffic
                if abs(float(v.position[1]) - lane * 4) < 2 + v.WIDTH / 2
                and abs(float(v.position[0] - ego.position[0])) <= 150
            ]
            front = [v for v in neighbors if v.position[0] >= ego.position[0]]
            rear = [v for v in neighbors if v.position[0] < ego.position[0]]
            front = min(front, key=lambda v: v.position[0]) if front else None
            rear = max(rear, key=lambda v: v.position[0]) if rear else None
            fg = (
                max(0, float(front.position[0] - ego.position[0]) - (front.LENGTH + ego.LENGTH) / 2)
                if front
                else None
            )
            rg = (
                max(0, float(ego.position[0] - rear.position[0]) - (rear.LENGTH + ego.LENGTH) / 2)
                if rear
                else None
            )
            fc = speed - float(front.velocity[0]) if front else 0
            rc = float(rear.velocity[0]) - speed if rear else 0
            result.append(
                {
                    "lane": lane + 1,
                    "front_gap_m": round(fg, 1) if fg is not None else None,
                    "front_speed_kmh": round(float(front.speed) * 3.6, 1) if front else None,
                    "front_ttc_s": round(fg / fc, 2) if fg is not None and fc > 0.1 else None,
                    "rear_gap_m": round(rg, 1) if rg is not None else None,
                    "rear_closing_speed_mps": round(rc, 1),
                    "rear_ttc_s": round(rg / rc, 2) if rg is not None and rc > 0.1 else None,
                }
            )
        return result

    def state(self):
        return {
            "rules": "Four straight highway lanes numbered 1 (left) to 4 (right). "
            "One decision controls the next 1 second of simulation. "
            "Physics is frozen during API calls. Gaps are bumper-to-bumper. "
            "Null gap means no vehicle observed within 150m, not infinite safety. "
            "Null TTC means no closing collision predicted at current speed. "
            "Lane changes may take more than one decision; keep preserves target lane/speed. "
            "Other vehicles can change lanes. Lane snapshots are not safety guarantees.",
            "ego": {
                "speed_kmh": round(float(self.ego.speed) * 3.6, 1),
                "target_speed_kmh": round(float(self.ego.target_speed) * 3.6, 1),
                "lane": int(self.ego.lane_index[2]) + 1,
                "target_lane": int(self.ego.target_lane_index[2]) + 1,
                "changing_lane": self.changing_lane,
            },
            "lanes": self.lanes(),
            "available_actions": self.available(),
            "elapsed_s": round(self.elapsed, 2),
            "remaining_s": round(max(0, self.duration - self.elapsed), 2),
            "distance_m": round(self.distance, 1),
            "overtakes": len(self.passed),
            "hard_brakes": self.hard_brakes,
            "lane_changes": self.lane_changes,
            "crashed": bool(self.ego.crashed),
        }

    def tick(self, action="keep"):
        if self.end_reason():
            raise ValueError("Cannot advance an ended drive")
        if action not in self.available():
            raise ValueError(f"Unavailable action: {action}")
        old_target = self.ego.target_lane_index
        _, reward, self.terminated, self.truncated, _ = self.env.step(ACTIONS[action])
        self.ticks += 1
        if old_target != self.ego.target_lane_index:
            self.lane_changes += 1
        for v in self.traffic:
            if id(v) in self.initial_ahead and v.position[0] + 5 < self.ego.position[0]:
                self.passed.add(id(v))
        braking = float(self.ego.action.get("acceleration", 0)) < -4
        if braking and not self.braking:
            self.hard_brakes += 1
        self.braking = braking
        return float(reward)

    def end_reason(self):
        if self.ego.crashed:
            return "Collision"
        if self.terminated:
            return "Left the road"
        if self.truncated or self.ticks >= round(self.duration * HZ):
            return "Drive complete"
        return None

    def close(self):
        self.env.close()

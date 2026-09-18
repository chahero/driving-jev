from __future__ import annotations

import argparse
import json
import math
import os
import queue
import threading
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from .engine import ACTIONS, HZ, LABELS, Drive
from .policy import choose

ROOT = Path(__file__).resolve().parents[2]
BG, INK, MUTED = (12, 18, 23), (233, 239, 242), (134, 155, 164)
TEAL, AMBER, RED = (88, 223, 185), (250, 181, 90), (243, 102, 111)


class Session:
    def __init__(self, seed, duration, policy, style, artifacts):
        self.seed, self.duration, self.policy, self.style = seed, duration, policy, style
        self.drive = Drive(seed, duration)
        artifacts.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        self.path = artifacts / f"run-{stamp}.jsonl"
        self.log = self.path.open("w", encoding="utf-8")
        self.episode = 1
        self.decisions = 0
        self.active = None
        self.remaining = 0
        self.last = None
        self.event(
            "start",
            seed=seed,
            policy=policy,
            style=style,
            duration_s=duration,
            environment=self.drive.config,
        )

    def event(self, kind, **data):
        self.log.write(json.dumps({"event": kind, "episode": self.episode, **data}) + "\n")
        self.log.flush()

    def begin(self, decision, before):
        if self.active is not None or before != self.drive.state():
            raise ValueError("Cannot apply an outdated or overlapping decision")
        if decision.action not in self.drive.available():
            raise ValueError("Unavailable action")
        self.active = (decision, before)
        self.remaining = HZ
        self.last = decision

    def tick(self):
        if self.active is None:
            return
        decision, before = self.active
        action = decision.action if self.remaining == HZ else "keep"
        self.drive.tick(action)
        self.remaining -= 1
        if self.remaining == 0 or self.drive.end_reason():
            self.decisions += 1
            self.event(
                "decision",
                index=self.decisions,
                before=before,
                decision=decision.to_dict(),
                after=self.drive.state(),
                end_reason=self.drive.end_reason(),
            )
            self.active = None
            self.remaining = 0

    def restart(self):
        self.event("restart", state=self.drive.state())
        self.drive.close()
        self.drive = Drive(self.seed, self.duration)
        self.episode += 1
        self.decisions = 0
        self.active = self.last = None
        self.remaining = 0
        self.event(
            "start",
            seed=self.seed,
            policy=self.policy,
            style=self.style,
            duration_s=self.duration,
            environment=self.drive.config,
        )

    def close(self):
        self.event("end", reason=self.drive.end_reason() or "Closed", state=self.drive.state())
        self.log.close()
        self.drive.close()


class Window:
    RX, RY, RW, RH, EGO_Y = 340, 122, 320, 604, 594

    def __init__(self, session, speed=1, paused=False):
        import pygame as pg

        self.pg, self.session = pg, session
        pg.init()
        self.screen = pg.display.set_mode((1040, 800))
        pg.display.set_caption("Driving / Jev")
        self.clock = pg.time.Clock()
        self.fonts = {n: pg.font.SysFont("Segoe UI", n) for n in (13, 15, 18, 24, 34, 66)}
        self.bold = pg.font.SysFont("Segoe UI", 18, bold=True)
        self.speed, self.paused = speed, paused
        self.pending = False
        self.results = queue.Queue()
        self.error = ""
        self.accumulator = 0.0
        self.buttons = {}
        self.before = None

    def text(self, value, x, y, size=18, color=INK):
        self.screen.blit(self.fonts[size].render(str(value), True, color), (x, y))

    def button(self, name, label, x, width=100):
        pg = self.pg
        rect = pg.Rect(x, 33, width, 44)
        pg.draw.rect(
            self.screen,
            (38, 55, 63) if rect.collidepoint(pg.mouse.get_pos()) else (24, 36, 44),
            rect,
            border_radius=8,
        )
        self.text(label, x + 14, 44, 15)
        self.buttons[name] = rect

    def request(self):
        self.before = self.session.drive.state()
        state, policy, style = self.before, self.session.policy, self.session.style
        self.pending = True

        def worker():
            try:
                self.results.put((choose(state, policy, style), None))
            except Exception as exc:  # noqa: BLE001 - contain failures at thread boundary
                self.results.put((None, type(exc).__name__))

        threading.Thread(target=worker, daemon=True, name="jev-driver").start()

    def control(self, name):
        if name == "pause":
            if self.error:
                self.error = ""
                self.paused = False
            else:
                self.paused = not self.paused
        elif name == "restart" and not self.pending:
            self.session.restart()
            self.error = ""
            self.paused = False
            self.accumulator = 0
        elif name == "speed":
            self.speed = {1: 2, 2: 4, 4: 1}[self.speed]

    def update(self, dt):
        if self.pending:
            try:
                decision, error = self.results.get_nowait()
            except queue.Empty:
                pass
            else:
                self.pending = False
                if error:
                    self.error = error
                    self.paused = True
                    self.session.event("error", error_type=error, state=self.before)
                else:
                    self.session.begin(decision, self.before)
                self.accumulator = 0
        if self.paused or self.pending or self.session.drive.end_reason():
            return
        if self.session.active is None:
            self.request()
            return
        self.accumulator += min(dt, 0.1) * self.speed
        while self.accumulator >= 1 / HZ and self.session.active is not None:
            self.session.tick()
            self.accumulator -= 1 / HZ
        if self.session.active is None:
            self.accumulator = 0

    def car(self, vehicle, ego=False, index=0):
        pg = self.pg
        d = self.session.drive
        x = self.RX + (float(vehicle.position[1]) / 4 + 0.5) * 80
        y = self.EGO_Y - float(vehicle.position[0] - d.ego.position[0]) * 12
        if y < self.RY - 80 or y > self.RY + self.RH + 80:
            return
        palette = [
            (107, 143, 186),
            (206, 180, 133),
            (187, 115, 120),
            (168, 163, 190),
            (187, 204, 207),
        ]
        color = RED if vehicle.crashed else TEAL if ego else palette[index % len(palette)]
        sprite = pg.Surface((54, 78), pg.SRCALPHA)
        pg.draw.rect(sprite, (0, 0, 0, 80), (9, 9, 42, 65), border_radius=10)
        pg.draw.rect(sprite, (10, 15, 20), (3, 16, 48, 44), border_radius=4)
        pg.draw.rect(sprite, color, (7, 4, 40, 64), border_radius=9)
        pg.draw.rect(sprite, (27, 46, 55), (11, 20, 32, 14), border_radius=4)
        pg.draw.rect(sprite, (27, 46, 55), (12, 47, 30, 11), border_radius=3)
        pg.draw.line(sprite, tuple(min(c + 35, 255) for c in color), (12, 10), (41, 10), 2)
        pg.draw.rect(sprite, (251, 245, 207), (10, 7, 7, 4), border_radius=2)
        pg.draw.rect(sprite, (251, 245, 207), (37, 7, 7, 4), border_radius=2)
        braking = float(vehicle.action.get("acceleration", 0)) < -1
        for sx in (10, 37):
            pg.draw.rect(sprite, RED if braking else (150, 65, 69), (sx, 61, 7, 4), border_radius=2)
        sprite = pg.transform.rotate(sprite, -math.degrees(float(vehicle.heading)))
        self.screen.blit(sprite, sprite.get_rect(center=(x, y)))
        if ego:
            label = self.bold.render("YOU", True, TEAL)
            self.screen.blit(label, label.get_rect(center=(x, y + 56)))

    def draw_road(self):
        pg, d = self.pg, self.session.drive
        self.screen.set_clip((298, self.RY, 404, self.RH))
        pg.draw.rect(self.screen, (22, 42, 37), (298, self.RY, 404, self.RH))
        pg.draw.rect(self.screen, (42, 50, 55), (self.RX, self.RY, self.RW, self.RH))
        for x in (self.RX + 5, self.RX + self.RW - 6):
            pg.draw.line(self.screen, (209, 181, 117), (x, self.RY), (x, self.RY + self.RH), 2)
        for lane in range(1, 4):
            x = self.RX + lane * 80
            for world in range(int(d.ego.position[0] // 10) - 5, int(d.ego.position[0] // 10) + 8):
                y = self.EGO_Y - (world * 10 - float(d.ego.position[0])) * 12
                pg.draw.rect(self.screen, (149, 163, 166), (x - 1, y, 3, 44), border_radius=1)
        for world in range(int(d.ego.position[0] // 8) - 5, int(d.ego.position[0] // 8) + 8):
            y = self.EGO_Y - (world * 8 - float(d.ego.position[0])) * 12
            for x in (317, 682):
                pg.draw.rect(self.screen, (83, 110, 99), (x, y, 4, 40), border_radius=2)
                pg.draw.rect(self.screen, (212, 221, 196), (x - 1, y, 6, 5), border_radius=1)
        target_x = self.RX + (d.ego.target_lane_index[2] + 0.5) * 80
        pg.draw.line(
            self.screen,
            (71, 122, 108),
            (target_x, self.EGO_Y - 175),
            (target_x, self.EGO_Y - 66),
            2,
        )
        pg.draw.polygon(
            self.screen,
            TEAL,
            [
                (target_x, self.EGO_Y - 180),
                (target_x - 5, self.EGO_Y - 170),
                (target_x + 5, self.EGO_Y - 170),
            ],
        )
        for i, vehicle in enumerate(d.traffic):
            self.car(vehicle, index=i)
        self.car(d.ego, ego=True)
        self.screen.set_clip(None)
        for lane in range(4):
            self.text(f"0{lane + 1}", self.RX + lane * 80 + 30, 98, 13, MUTED)

    def draw(self):
        pg, s = self.pg, self.session
        d = s.drive
        self.screen.fill(BG)
        self.text("DRIVING", 32, 25, 34)
        self.text("/ JEV", 193, 32, 24, TEAL)
        self.text("A DECISION AT EVERY TURN", 34, 74, 13, MUTED)
        self.button("pause", "Retry" if self.error else "Resume" if self.paused else "Pause", 676)
        self.button("restart", "Wait..." if self.pending else "Restart", 787)
        self.button("speed", f"{self.speed}x speed", 898, 110)
        self.draw_road()
        self.text("JEV AUTOPILOT" if s.policy == "jev" else "OFFLINE AUTOPILOT", 34, 133, 15, TEAL)
        self.text(f"{float(d.ego.speed) * 3.6:.0f}", 30, 166, 66)
        self.text("km/h", 178, 208, 18, MUTED)
        self.text(f"TARGET  {float(d.ego.target_speed) * 3.6:.0f} km/h", 36, 255, 15, MUTED)
        pg.draw.line(self.screen, (41, 55, 63), (34, 299), (260, 299))
        self.text("DISTANCE", 34, 322, 13, MUTED)
        self.text(f"{d.distance:.0f} m", 32, 345, 34)
        self.text("OVERTAKES", 34, 415, 13, MUTED)
        self.text(len(d.passed), 34, 440, 34)
        self.text("HARD BRAKES", 155, 415, 13, MUTED)
        self.text(d.hard_brakes, 155, 440, 34)
        self.text("SIMULATION TIME", 34, 523, 13, MUTED)
        self.text(f"{d.elapsed:04.1f} / {d.duration:g} s", 33, 548, 24)
        pg.draw.rect(self.screen, (34, 49, 56), (34, 590, 226, 5), border_radius=2)
        pg.draw.rect(
            self.screen,
            TEAL,
            (34, 590, round(226 * min(1, d.elapsed / d.duration)), 5),
            border_radius=2,
        )
        self.text(f"SEED {s.seed}  /  {s.style.upper()}", 34, 653, 13, MUTED)
        self.text(f"{s.decisions} decisions", 34, 679, 15, MUTED)
        self.text("CURRENT MANEUVER", 732, 133, 13, MUTED)
        decision = s.last
        self.text(LABELS[decision.action] if decision else "Ready to drive", 730, 167, 24)
        self.text("CONFIDENCE", 732, 226, 13, MUTED)
        self.text("LATENCY", 892, 226, 13, MUTED)
        self.text(
            f"{decision.confidence:.0%}" if decision and decision.source == "jev" else "--",
            732,
            252,
            24,
        )
        self.text(
            f"{decision.latency_ms:.0f} ms" if decision and decision.source == "jev" else "--",
            892,
            252,
            24,
        )
        self.text("ACTION PROBABILITIES", 732, 314, 13, MUTED)
        for i, action in enumerate(ACTIONS):
            y = 346 + i * 48
            p = decision.probabilities.get(action, 0) if decision else 0
            color = TEAL if decision and decision.action == action else MUTED
            self.text(LABELS[action], 732, y, 15, color)
            self.text(
                f"{p:.0%}" if decision and decision.source == "jev" else "--", 963, y, 15, MUTED
            )
            pg.draw.rect(self.screen, (34, 49, 56), (732, y + 26, 271, 4), border_radius=2)
            if p:
                pg.draw.rect(self.screen, color, (732, y + 26, round(271 * p), 4), border_radius=2)
        lead = d.lanes()[d.ego.target_lane_index[2]]
        gap = lead["front_gap_m"]
        self.text("GAP IN TARGET LANE", 732, 618, 13, MUTED)
        self.text(
            f"{gap:.1f} m" if gap is not None else "> 150 m",
            731,
            642,
            24,
            RED if gap is not None and gap < 15 else INK,
        )
        self.text("4 lanes / real vehicle physics", 732, 699, 13, MUTED)
        reason = d.end_reason()
        if self.paused or reason:
            pg.draw.rect(self.screen, (15, 24, 30), (312, 330, 376, 127), border_radius=10)
            self.text(
                reason.upper() if reason else "PAUSED", 332, 353, 24, RED if d.ego.crashed else INK
            )
            self.text(
                "Press R for the same traffic" if reason else "Press Space to start / resume",
                332,
                397,
                15,
                MUTED,
            )
        if self.error:
            status, color = f"{self.error} / check .env or connection, then Retry", RED
        elif reason:
            status, color = (
                f"{reason} / {d.distance:.0f} m / {len(d.passed)} overtakes",
                RED if d.ego.crashed else TEAL,
            )
        elif self.paused:
            status, color = "Paused / no new API requests", MUTED
        elif self.pending:
            status, color = (
                "Jev is thinking / road paused" if s.policy == "jev" else "Evaluating traffic",
                AMBER,
            )
        else:
            status, color = "Driving / steering handled by the physics engine", TEAL
        pg.draw.line(self.screen, (41, 55, 63), (32, 752), (1008, 752))
        self.text(status, 34, 769, 13, color)
        self.text("SPACE pause   R restart   +/- speed   ESC quit", 690, 769, 13, MUTED)
        pg.display.flip()

    def run(self, screenshot=None, auto_exit=False):
        pg = self.pg
        running, saved = True, False
        try:
            while running:
                dt = self.clock.tick(60) / 1000
                for e in pg.event.get():
                    if e.type == pg.QUIT:
                        running = False
                    elif e.type == pg.KEYDOWN:
                        if e.key in (pg.K_ESCAPE, pg.K_q):
                            running = False
                        elif e.key == pg.K_SPACE:
                            self.control("pause")
                        elif e.key == pg.K_r:
                            self.control("restart")
                        elif e.key in (pg.K_MINUS, pg.K_EQUALS, pg.K_PLUS):
                            self.control("speed")
                    elif e.type == pg.MOUSEBUTTONDOWN and e.button == 1:
                        for name, rect in self.buttons.items():
                            if rect.collidepoint(e.pos):
                                self.control(name)
                if not running:
                    break
                self.update(dt)
                self.draw()
                if (
                    screenshot
                    and not saved
                    and (
                        self.session.drive.elapsed >= 5
                        or self.error
                        or self.session.drive.end_reason()
                    )
                ):
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    pg.image.save(self.screen, str(screenshot))
                    saved = True
                if auto_exit and (self.error or self.session.drive.end_reason()):
                    break
        finally:
            pg.quit()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Watch Jev drive through highway traffic.")
    parser.add_argument("--policy", choices=("jev", "heuristic"), default="jev")
    parser.add_argument("--style", choices=("balanced", "cautious"), default="balanced")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--duration",
        type=int,
        default=40,
        help="Simulation seconds; at most one API call per second",
    )
    parser.add_argument("--speed", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument("--paused", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--exit-after", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--artifacts-dir", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args(argv)
    if not 1 <= args.duration <= 300:
        parser.error("--duration must be between 1 and 300 seconds")
    load_dotenv(ROOT / ".env", override=False)
    if args.policy == "jev" and not os.getenv("TYPESAFE_API_KEY", "").strip():
        parser.error(f"Set TYPESAFE_API_KEY in {ROOT / '.env'} or use --policy heuristic")
    s = Session(args.seed, args.duration, args.policy, args.style, args.artifacts_dir)
    code = 0
    try:
        if args.headless:
            while not s.drive.end_reason():
                before = s.drive.state()
                s.begin(choose(before, args.policy, args.style), before)
                while s.active:
                    s.tick()
            print(
                f"{s.drive.end_reason()}: {s.drive.distance:.0f} m / {len(s.drive.passed)} overtakes"
            )
        else:
            window = Window(s, args.speed, args.paused)
            window.run(args.screenshot, args.exit_after)
            code = 1 if window.error else 0
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 - application boundary, no secret-bearing SDK bodies
        s.event("error", error_type=type(exc).__name__)
        print(f"Stopped: {type(exc).__name__}. Check configuration and connectivity.")
        code = 1
    finally:
        s.close()
    print(f"Log: {s.path}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pytest

from driving_jev.app import Session, Window
from driving_jev.policy import choose


@pytest.fixture
def window(tmp_path):
    session = Session(42, 5, "heuristic", "balanced", tmp_path)
    view = Window(session, paused=True)
    yield view
    view.pg.quit()
    session.close()


def test_finished_request_does_not_move_road_while_paused(window):
    window.before = window.session.drive.state()
    decision = choose(window.before, "heuristic")
    window.pending = True
    window.results.put((decision, None))
    window.update(1)
    assert window.session.drive.state() == window.before
    assert window.session.active is not None
    window.control("pause")
    window.update(0.04)
    assert window.session.drive.ticks == 1


def test_waiting_does_not_advance_physics_or_restart(window):
    before = window.session.drive.state()
    window.pending = True
    window.paused = False
    window.update(10)
    window.control("restart")
    assert window.session.drive.state() == before
    assert window.session.episode == 1


def test_error_pauses_and_restart_resets_collision(window):
    window.pending = True
    window.before = window.session.drive.state()
    window.results.put((None, "TypeSafeAPITimeoutError"))
    window.update(1)
    assert window.error and window.paused
    assert window.session.drive.ticks == 0
    window.draw()
    window.session.drive.ego.crashed = True
    window.control("restart")
    assert not window.session.drive.ego.crashed
    assert window.session.episode == 2
    assert not window.error

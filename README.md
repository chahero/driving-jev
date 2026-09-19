# Driving / Jev

[한국어](README.ko.md)

Watch TypeSafe Jev drive through highway traffic. A desktop game shows the agent
choosing when to follow a lead car, change lanes, accelerate, or slow down.

## Watch the two drivers

Both runs start with the same traffic layout (seed 42) and last 40 simulation seconds.
The game speed is set to 2x in both videos. The Jev video includes time spent waiting
for real API responses. Click a GIF preview to open the full MP4 video.

| Jev / live API decisions | Offline / rule-based decisions |
| --- | --- |
| [![Jev driving preview](media/jev-preview.gif)](media/jev.mp4) | [![Offline driving preview](media/offline-preview.gif)](media/offline.mp4) |
| [Full video](media/jev.mp4) · [Screenshot](media/jev.png) | [Full video](media/offline.mp4) · [Screenshot](media/offline.png) |

## Run on Windows

Python 3.13+ and uv are required. Clone the repository, then create the environment
and install the dependencies:

```powershell
git clone https://github.com/chahero/driving-jev.git
cd driving-jev
uv venv --python 3.13 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
Copy-Item .env.example .env # First setup only; do not overwrite an existing key.
```

Set `TYPESAFE_API_KEY=your-key` in `.env`. The app reads this file from the project
directory. Existing environment variables take precedence.

- **play.cmd**: start the Jev API driver.
- **play-offline.cmd**: start the rule-based driver without an API.
- **Space**: pause or resume. **R**: restart with the same initial traffic.
- **+/-**: cycle through 1x, 2x, and 4x game speed. **Esc/Q**: quit.

```powershell
.\.venv\Scripts\driving-jev.exe
.\.venv\Scripts\driving-jev.exe --policy heuristic
.\.venv\Scripts\driving-jev.exe --style cautious
.\.venv\Scripts\driving-jev.exe --seed 123 --duration 60
```

Add `--paused` to open the window without starting API requests.
A default run lasts **40 simulation seconds, with at most 40 decisions**. It ends
early on a collision or when the car leaves the road. The window stays open to show
the result. Restarting begins a new run with a new request budget.
API calls incur the provider's normal usage charges. Requests are not retried automatically.

## What the dashboard shows

- Current speed and target speed.
- Distance traveled, vehicles overtaken, and hard braking events.
- Selected action, action probabilities, model confidence, and response latency.
- Distance to the lead vehicle in the target lane.
- Remaining simulation time and decision count.

The green car is controlled by Jev and travels toward the top of the screen.
Lanes are numbered 1–4 from left to right. Offline mode does not display model
probabilities, confidence, or API latency.

## How Jev and the game work together

[HighwayEnv](https://highway-env.farama.org/) handles vehicle physics and other
vehicles' behavior. Pygame draws the dashboard and road. The vertical road view
maps the simulation's coordinates with different horizontal and vertical scales.

1. The app extracts the ego car's speed and lane, plus front and rear vehicle gaps,
   speeds, and time-to-collision estimates for each lane.
2. It calculates front headway, a conservative three-second front-gap estimate,
   and differences in clearance and lead-vehicle speed to help compare actions.
3. Jev's `Choice` selects **change left / keep course / change right / speed up / slow down**.
4. HighwayEnv's controller steers toward the target lane and adjusts the speed.
5. After one simulation second, the app requests the next decision.

**The road freezes while an API request is pending.** The UI continues to render,
but network latency does not advance the vehicles. A request already in flight can
finish while paused; no new requests are sent and physics stays paused. On an error,
the app pauses and offers a Retry button.

The model receives structured observations rather than screenshots and does not
control the steering angle every frame. The app excludes actions that are unavailable
at lane or speed limits, plus repeated lane-change commands during an ongoing change.
It does not replace the model's risky choices with a separate safety controller.

Traffic is observed within 150m ahead and behind. Time-to-collision estimates use
current relative speeds and cannot fully predict other vehicles' lane changes or
acceleration. Collision-free driving is not guaranteed. This is a game and decision
visualization demo, not a controller for real vehicles.

## Offline comparison

The offline driver checks adjacent front and rear gaps when it encounters a slower
lead car, then changes lanes or slows down. It accelerates toward the target speed
when the road is clear. The `balanced` style aims for up to 108 km/h; `cautious` aims
for 90 km/h.

The same seed produces the same initial traffic. Other vehicles then react to the
ego car, so the two policies do not necessarily encounter identical later traffic.

```powershell
.\.venv\Scripts\driving-jev.exe --headless --policy heuristic --seed 42 --duration 40
.\.venv\Scripts\driving-jev.exe --headless --policy jev --seed 42 --duration 40
```

Overtakes count vehicles that started ahead and have been fully passed, each counted
once. A hard braking event is counted when acceleration crosses below -4 m/s².
Repeated runs do not train or update the model's weights.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
```

- `engine.py`: vehicle physics integration, lane observations, and driving metrics.
- `policy.py`: Jev requests and the offline driver.
- `app.py`: game UI, asynchronous decisions, pause/restart controls, and run logs.

JSONL files in `artifacts/` record the seed, environment configuration, state before
each decision, chosen action, state after one simulation second, and end reason.
Jev decisions also include the action assessment sent to the model and its policy
version. `.env`, the virtual environment, and run logs are excluded from Git.

# Reversi Workshop Server

A small Python Reversi/Othello server with:

- a minimal browser UI for local play
- JSON APIs for creating and monitoring games
- built-in sample bots (`human`, `random`, `greedy`)
- support for remote HTTP bots in ad-hoc games
- a tournament mode where workshop bots register, receive a secret, and submit authenticated moves

## What is included

- `reversi/game.py` — core rules engine
- `reversi/manager.py` — in-memory game orchestration and bot turn execution
- `reversi/tournament.py` — connected bot registry, secret validation, and operator-started tournaments
- `reversi/app.py` — Flask app and JSON endpoints
- `reversi/web/` — browser UI for local play and tournament operations
- `examples/remote_random_bot.py` — standalone example bot that registers, polls for turns, and plays moves
- `tests/` — unit and API tests

## Quick start

From the project root:

```zsh
. .venv/bin/activate
python -m pip install -e .
python -m reversi --host 127.0.0.1 --port 8000 --operator-password 'choose-a-strong-password'
```

Then open <http://127.0.0.1:8000>.

For access from other machines on your network, bind to all interfaces and set an operator password:

```zsh
. .venv/bin/activate
python -m reversi --host 0.0.0.0 --port 8000 --operator-password 'choose-a-strong-password'
```

Then connect to `http://<your-machine-ip>:8000`.

## Run with Docker

From the project root, build the image and start the container with the included `Makefile`:

```zsh
make start REVERSI_OPERATOR_PASSWORD='choose-a-strong-password'
```

This will:

- build a local Docker image named `reversi-workshop`
- start a container named `reversi-workshop`
- publish the app on `http://127.0.0.1:8000`

Useful Docker targets:

```zsh
make build
make start REVERSI_OPERATOR_PASSWORD='choose-a-strong-password'
make logs
make stop
```

Optional overrides:

- `HOST_PORT=9000` to expose the app on a different local port
- `IMAGE_NAME=my-reversi` to change the built image tag
- `CONTAINER_NAME=my-reversi` to change the container name

Example with a custom host port:

```zsh
make start REVERSI_OPERATOR_PASSWORD='choose-a-strong-password' HOST_PORT=9000
```

Then open <http://127.0.0.1:9000>.

If you prefer not to place the password in shell history or the process list, use an environment variable instead:

```zsh
. .venv/bin/activate
export REVERSI_OPERATOR_PASSWORD='choose-a-strong-password'
python -m reversi --host 0.0.0.0 --port 8000
```

Hosted bot documentation is available at:

- `http://<your-machine-ip>:8000/swagger` — Swagger UI for the external tournament bot API
- `http://<your-machine-ip>:8000/openapi.json` — raw OpenAPI document
- `http://<your-machine-ip>:8000/instructions` — human-readable bot integration guide

The browser UI and the operator-oriented APIs are reachable from any IP address. Those protected routes require either:

- HTTP Basic auth with username `operator` and the configured password, or
- the `X-Reversi-Operator-Password` request header.

By default, `python -m reversi` now uses `waitress` in normal mode, which is more reliable for external clients than Flask's built-in development server. If you explicitly want the Flask development server, use:

```zsh
. .venv/bin/activate
python -m reversi --host 0.0.0.0 --port 8000 --flask-dev-server
```

If you saw an error like `OSError: [Errno 57] Socket is not connected` inside `werkzeug/serving.py`, that usually comes from the Flask development server when a client disconnects mid-request. It is not a Reversi game-rule bug, but it is a good reason to prefer the default Waitress server for workshop use.

On macOS, Waitress may also sometimes log warnings like `server accept() threw an exception` with `OSError: [Errno 22] Invalid argument` when a remote client disconnects immediately during accept. For this app, Waitress socket-error logging is disabled by default in normal mode because these disconnects are usually harmless. If you want the raw socket warnings back for troubleshooting, start with:

```zsh
. .venv/bin/activate
python -m reversi --host 0.0.0.0 --port 8000 --log-socket-errors
```

## Run tests

```zsh
. .venv/bin/activate
python -m unittest discover -s tests -v
```

## Local play

Use the browser UI to create any of these combinations:

- human vs human
- human vs random
- human vs greedy
- remote vs human
- bot vs bot

The server automatically plays autonomous bot turns until the next human turn or the game finishes.

## Tournament mode

Tournament mode is meant for workshop participants who connect their own bots to this server.

### How it works

1. Each bot registers itself with the server.
2. The server returns a `bot_id` and a secret.
3. The browser UI shows all connected bots.
4. The operator can also spawn local sample tournament bots directly from the UI.
5. The operator can remove connected bots from the UI before a tournament starts.
6. The browser UI shows live board snapshots for all ongoing tournament games in the main viewport.
7. The operator can click any tournament game to focus it in the main board.
8. Tournament matches can run in parallel.
9. The operator chooses a tournament format in the browser UI and starts the tournament manually.
10. Bot names must be unique.
11. Bots poll the server for assignments.
12. Each registered bot may send at most one authenticated request every 250 ms across assignment polling and move submission.
13. When it is their turn, they submit a move together with their secret.
14. The server validates that the move came from the correct bot for the current seat.

The browser UI includes an **External bot poll rate** control with both a slider and an exact millisecond input. It defaults to **250 ms** and can be changed while a tournament is already running.

If a bot exceeds that request rate, the server responds with:

- HTTP `429 Too Many Requests`
- a `Retry-After` response header (rounded up to whole seconds, per the HTTP standard)
- a JSON `retry_after_seconds` field with the precise sub-second wait before the next request
- a JSON `limit_seconds` field with the current configured minimum spacing between requests

Because operators can change the rate during a tournament, external bots should not hard-code a single polling interval forever. They should monitor `limit_seconds` on successful responses and always honor `Retry-After` after a `429` so they can slow down immediately without overloading the server.

### Start two sample bots

`examples/remote_random_bot.py` is a self-contained bot that uses only the Python standard library and the public bot endpoints. It registers once, polls for its turn, plays a legal move from the data the server returns, and paces itself using the `limit_seconds` and `Retry-After` values the server reports.

Run these in separate terminals:

```zsh
. .venv/bin/activate
python examples/remote_random_bot.py --server http://127.0.0.1:8000 --name alpha
```

```zsh
. .venv/bin/activate
python examples/remote_random_bot.py --server http://127.0.0.1:8000 --name beta
```

Copy the file as a starting point for your own bot and replace `ReversiBot.choose_move` with your strategy.

Then open the UI, choose a format such as **Double round robin**, **Single round robin**, or **Single elimination playoff**, and press **Start tournament with connected bots**.

If you just want some local sample bots for testing, you can also use the spawn form in the UI to launch them directly from the server machine.

### Tournament bot API

These endpoints are the ones intended for external workshop bots and remain reachable from other machines on your network without operator authentication.

#### Register a bot

```zsh
curl -s http://127.0.0.1:8000/api/tournament/bots/register \
  -H 'Content-Type: application/json' \
  -d '{"name": "alpha"}'
```

Example response:

```json
{
  "bot_id": "2bca0f14",
  "name": "alpha",
  "secret": "<generated-secret>",
  "created_at": "2026-09-25T12:00:00+00:00",
  "last_seen_at": "2026-09-25T12:00:00+00:00",
  "status": "connected"
}
```

If another bot has already registered the same name, registration is rejected.

#### Poll for assignments

Bots should usually poll about every 250 ms, and they must wait at least 250 ms between authenticated requests for the same `bot_id` unless the operator changes the current limit in the UI.

```zsh
curl -s "http://127.0.0.1:8000/api/tournament/bots/<bot_id>/assignment?secret=<secret>"
```

The same limit also applies right before `POST /api/tournament/matches/<game_id>/move`, so a bot that just received an assignment may need a short pause before sending its move. The successful assignment response includes `limit_seconds`, and overload responses include both `Retry-After` and `retry_after_seconds`, which bots should use to stay aligned with live rate changes.

#### List connected bots

```zsh
curl -s -u operator:choose-a-strong-password http://127.0.0.1:8000/api/tournament/bots
```

Secrets are not included in this list.

#### Spawn local sample bots

```zsh
curl -s -u operator:choose-a-strong-password http://127.0.0.1:8000/api/tournament/spawn-bots \
  -H 'Content-Type: application/json' \
  -d '{"name_prefix": "local-bot", "count": 2, "poll_interval": 0.25}'
```

This launches local random tournament bots on the same machine as the server. The browser UI exposes the same action.

#### Remove a connected bot

```zsh
curl -s -u operator:choose-a-strong-password -X DELETE http://127.0.0.1:8000/api/tournament/bots/<bot_id>
```

Bots that are part of the currently running tournament cannot be removed until that tournament finishes.

#### Start a tournament

With all connected bots in double round-robin mode:

```zsh
curl -s -u operator:choose-a-strong-password http://127.0.0.1:8000/api/tournaments \
  -H 'Content-Type: application/json' \
  -d '{"tournament_format": "double_round_robin"}'
```

With an explicit subset in single round-robin mode:

```zsh
curl -s -u operator:choose-a-strong-password http://127.0.0.1:8000/api/tournaments \
  -H 'Content-Type: application/json' \
  -d '{"bot_ids": ["bot-a", "bot-b"], "tournament_format": "single_round_robin"}'
```

Or as a single-elimination playoff bracket:

```zsh
curl -s -u operator:choose-a-strong-password http://127.0.0.1:8000/api/tournaments \
  -H 'Content-Type: application/json' \
  -d '{"tournament_format": "single_elimination"}'
```

For backward compatibility, the older `double_round_robin: true|false` request field still works.

#### Poll for an assignment

```zsh
curl -s "http://127.0.0.1:8000/api/tournament/bots/<bot_id>/assignment?secret=<secret>"
```

If it is not the bot's turn, the response contains:

```json
{
  "available": false,
  "message": "Waiting for another bot's turn."
}
```

If it *is* the bot's turn, the response contains the game state plus legal moves.

#### Submit a move

```zsh
curl -s http://127.0.0.1:8000/api/tournament/matches/<game_id>/move \
  -H 'Content-Type: application/json' \
  -d '{
    "bot_id": "2bca0f14",
    "secret": "<generated-secret>",
    "row": 2,
    "col": 3
  }'
```

If a bot sends the wrong secret or tries to play out of turn, the move is rejected.

## Standard game API

These endpoints are operator/local-play APIs. They are reachable from any IP but require operator authentication.

### Create a game

```zsh
curl -s -u operator:choose-a-strong-password http://127.0.0.1:8000/api/games \
  -H 'Content-Type: application/json' \
  -d '{
    "black": {"type": "human"},
    "white": {"type": "greedy"}
  }'
```

### Get a game state

```zsh
curl -s -u operator:choose-a-strong-password http://127.0.0.1:8000/api/games/<game_id>
```

### Submit a human move

```zsh
curl -s -u operator:choose-a-strong-password http://127.0.0.1:8000/api/games/<game_id>/move \
  -H 'Content-Type: application/json' \
  -d '{"row": 2, "col": 3}'
```

### List games

```zsh
curl -s -u operator:choose-a-strong-password http://127.0.0.1:8000/api/games
```

## Pull-style remote bot contract

For non-tournament ad-hoc games, you can still create a `remote` player like:

```json
{"type": "remote", "url": "http://localhost:9001"}
```

The server will call:

```text
POST http://localhost:9001/move
```

with a JSON body like:

```json
{
  "game_id": "abc12345",
  "you_are": "B",
  "state": {
    "board": [[".", ".", "."], [".", "B", "W"]],
    "current_player": "B",
    "status": "active",
    "winner": null,
    "termination_reason": null,
    "scores": {"black": 2, "white": 2},
    "legal_moves": [{"row": 2, "col": 3}],
    "move_count": 0,
    "history": []
  },
  "legal_moves": [{"row": 2, "col": 3}]
}
```

The remote bot can respond with any of:

```json
{"row": 2, "col": 3}
```

```json
{"move": {"row": 2, "col": 3}}
```

```json
{"pass": true}
```

If a remote bot errors, times out, or returns an illegal move, the game is ended as a forfeit.

## Notes for workshop use

- State is stored in memory only for now.
- Tournament bots are authenticated with per-registration secrets.
- The browser UI is operator-oriented: it shows connected bots and lets a human decide when to start.
- This is a good base for adding persistence, WebSockets, Swiss pairings, or team tournaments later.

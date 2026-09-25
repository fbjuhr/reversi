"""Standalone example bot for the Reversi workshop server.

The bot connects to the server over plain HTTP, registers once, then repeatedly
fetches its assignment and plays a move whenever it is its turn:

    python examples/remote_random_bot.py --server http://127.0.0.1:8000 --name alpha

It only uses the Python standard library and the public bot endpoints, so it can
be copied out of this repository and used as a starting point for your own bot.

Request pacing
--------------
The server rate limits every authenticated bot request. It reports the current
limit as ``limit_seconds`` on successful responses, and answers requests that
arrive too early with ``429 Too Many Requests`` plus ``retry_after_seconds`` and
a ``Retry-After`` header. The operator can change that limit at any time, even
while a tournament is running, so this bot reads the value from every response
and paces itself accordingly instead of hardcoding an interval.
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_REQUEST_INTERVAL_SECONDS = 0.25
REQUEST_TIMEOUT_SECONDS = 10.0


class RateLimitedError(RuntimeError):
    """Raised internally when the server answers with 429 Too Many Requests."""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limited, retry in {retry_after_seconds:.3f} seconds.")


class ReversiBot:
    """A bot that registers with the server and plays the moves it is offered."""

    def __init__(
        self,
        server_url: str,
        name: str,
        *,
        seed: int | None = None,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
        reservation_token: str | None = None,
        quiet: bool = False,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.name = name
        self.rng = random.Random(seed)
        self.reservation_token = reservation_token
        self.quiet = quiet
        # Pacing the server asked us to use. Updated from every response.
        self.request_interval_seconds = max(request_interval_seconds, 0.001)
        self.bot_id: str | None = None
        self.secret: str | None = None
        self._last_request_monotonic: float | None = None

    def log(self, message: str) -> None:
        if not self.quiet:
            print(f"{self.name}: {message}")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def register(self) -> None:
        """Register once and remember the returned bot_id and secret."""
        body: dict[str, Any] = {"name": self.name}
        if self.reservation_token:
            body["reservation_token"] = self.reservation_token
        payload = self._request("/api/tournament/bots/register", method="POST", body=body)
        self.bot_id = str(payload["bot_id"])
        self.secret = str(payload["secret"])
        self.log(f"registered with bot_id={self.bot_id} and secret={self.secret}")

    def run_forever(self) -> None:
        """Poll for assignments and play a move every time it is our turn."""
        if self.bot_id is None or self.secret is None:
            self.register()

        last_message: str | None = None
        while True:
            assignment = self.fetch_assignment()

            if assignment.get("available") is not True:
                message = str(assignment.get("message", "Waiting..."))
                if message != last_message:
                    self.log(message)
                    last_message = message
                self._sleep_until_next_request()
                continue

            last_message = None
            move = self.choose_move(assignment)
            if move is None:
                self.log("no legal moves supplied, waiting")
                self._sleep_until_next_request()
                continue

            self.submit_move(str(assignment["game_id"]), move)
            self._sleep_until_next_request()

    # ------------------------------------------------------------------ #
    # Bot API calls
    # ------------------------------------------------------------------ #

    def fetch_assignment(self) -> dict[str, Any]:
        """Ask the server whether it is our turn, and for the current board."""
        query = urllib.parse.urlencode({"secret": self.secret or ""})
        return self._request(f"/api/tournament/bots/{self.bot_id}/assignment?{query}")

    def choose_move(self, assignment: dict[str, Any]) -> dict[str, int] | None:
        """Pick one move out of the data the server just sent us.

        Replace the body of this method with your own strategy. Everything you
        need is in the assignment: ``state['board']``, ``state['scores']``,
        ``state['history']``, ``you_are`` and the pre-computed ``legal_moves``.
        """
        legal_moves = assignment.get("legal_moves")
        if not isinstance(legal_moves, list) or not legal_moves:
            return None
        choice = self.rng.choice(legal_moves)
        return {"row": int(choice["row"]), "col": int(choice["col"])}

    def submit_move(self, game_id: str, move: dict[str, int]) -> dict[str, Any]:
        """Send one legal move back to the server and log the resulting score."""
        result = self._request(
            f"/api/tournament/matches/{game_id}/move",
            method="POST",
            body={
                "bot_id": self.bot_id,
                "secret": self.secret,
                "row": move["row"],
                "col": move["col"],
            },
        )
        game = result.get("game", {})
        scores = game.get("scores", {})
        self.log(
            f"played ({move['row']}, {move['col']}) in {game.get('game_id')} "
            f"-> status {game.get('status')} score {scores.get('black')}:{scores.get('white')}"
        )
        return result

    # ------------------------------------------------------------------ #
    # HTTP plumbing and rate limiting
    # ------------------------------------------------------------------ #

    def _request(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform one request, honouring the server's pacing and 429 replies."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        while True:
            self._sleep_until_next_request()
            try:
                payload = self._send(path, method=method, data=data)
            except RateLimitedError as exc:
                self.log(f"rate limited, waiting {exc.retry_after_seconds:.3f} seconds")
                time.sleep(exc.retry_after_seconds)
                continue
            self._adopt_server_pacing(payload)
            return payload

    def _send(self, path: str, *, method: str, data: bytes | None) -> dict[str, Any]:
        request = urllib.request.Request(
            self.server_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return self._decode(response.read())
        except urllib.error.HTTPError as exc:
            payload = self._decode(exc.read())
            if exc.code == 429:
                self._adopt_server_pacing(payload)
                raise RateLimitedError(self._retry_after_seconds(exc, payload)) from exc
            message = payload.get("error", exc.reason)
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {message}") from exc
        finally:
            # Measure from when the reply arrived. The server stamps the request
            # somewhere between our send and this moment, so waiting a full
            # interval from here can never be too early for the server.
            self._last_request_monotonic = time.monotonic()

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _adopt_server_pacing(self, payload: dict[str, Any]) -> None:
        """Follow the limit_seconds value the server reports on every response."""
        limit_seconds = payload.get("limit_seconds")
        if isinstance(limit_seconds, (int, float)) and limit_seconds > 0:
            limit_seconds = float(limit_seconds)
            if limit_seconds != self.request_interval_seconds:
                self.log(f"server request interval is now {limit_seconds:.3f} seconds")
            self.request_interval_seconds = limit_seconds

    def _retry_after_seconds(self, exc: urllib.error.HTTPError, payload: dict[str, Any]) -> float:
        retry_after = payload.get("retry_after_seconds")
        if isinstance(retry_after, (int, float)) and retry_after >= 0:
            # The server rounds this value, so it can arrive as 0.0. Keep a tiny
            # floor instead of falling back to the much coarser Retry-After header.
            return max(float(retry_after), 0.01)

        header = exc.headers.get("Retry-After")
        if header is not None:
            try:
                parsed = float(header)
            except ValueError:
                parsed = 0.0
            if parsed > 0:
                return parsed

        return self.request_interval_seconds

    def _sleep_until_next_request(self) -> None:
        """Keep at least request_interval_seconds between two requests."""
        if self._last_request_monotonic is None:
            return
        remaining = self.request_interval_seconds - (time.monotonic() - self._last_request_monotonic)
        if remaining > 0:
            time.sleep(remaining)


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect an example bot to the Reversi workshop server.")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="Base URL of the Reversi server.")
    parser.add_argument("--name", default="example-bot", help="Unique display name used during registration.")
    parser.add_argument(
        "--request-interval",
        type=float,
        default=DEFAULT_REQUEST_INTERVAL_SECONDS,
        help="Starting seconds between requests. Replaced by the limit_seconds value reported by the server.",
    )
    parser.add_argument("--seed", type=int, default=None, help="Optional random seed for reproducible play.")
    parser.add_argument("--reservation-token", default=None, help="Reserved token used by the operator spawn flow.")
    parser.add_argument("--quiet", action="store_true", help="Do not print progress messages.")
    args = parser.parse_args()

    bot = ReversiBot(
        server_url=args.server,
        name=args.name,
        seed=args.seed,
        request_interval_seconds=args.request_interval,
        reservation_token=args.reservation_token,
        quiet=args.quiet,
    )
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        print(f"\n{args.name}: stopped.")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from .tournament import EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS


class TournamentRandomBot:
    def __init__(
        self,
        server_url: str,
        name: str,
        poll_interval: float,
        seed: int | None = None,
        reservation_token: str | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.name = name
        self.poll_interval = poll_interval
        self.rng = random.Random(seed)
        self.reservation_token = reservation_token
        self.bot_id: str | None = None
        self.secret: str | None = None
        self._last_request_monotonic: float | None = None
        self._minimum_request_interval = max(poll_interval, 0.001)

    def register(self) -> None:
        body: dict[str, object] = {"name": self.name}
        if self.reservation_token:
            body["reservation_token"] = self.reservation_token
        payload = self._request(
            "/api/tournament/bots/register",
            method="POST",
            body=body,
        )
        self.bot_id = str(payload["bot_id"])
        self.secret = str(payload["secret"])
        print(f"Registered {self.name} as {self.bot_id}")
        print(f"Secret: {self.secret}")

    def run_forever(self) -> None:
        if self.bot_id is None or self.secret is None:
            self.register()

        assert self.bot_id is not None
        assert self.secret is not None

        while True:
            query = urllib.parse.urlencode({"secret": self.secret})
            assignment = self._request(f"/api/tournament/bots/{self.bot_id}/assignment?{query}")
            self._update_request_interval_from_payload(assignment)
            if assignment.get("available") is not True:
                print(assignment.get("message", "Waiting..."))
                time.sleep(self.poll_interval)
                continue

            legal_moves = assignment.get("legal_moves", [])
            if not legal_moves:
                print(f"{self.name}: no legal moves supplied, waiting")
                time.sleep(self.poll_interval)
                continue

            move = self.rng.choice(legal_moves)
            result = self._request(
                f"/api/tournament/matches/{assignment['game_id']}/move",
                method="POST",
                body={
                    "bot_id": self.bot_id,
                    "secret": self.secret,
                    "row": move["row"],
                    "col": move["col"],
                },
            )
            self._update_request_interval_from_payload(result)
            game = result["game"]
            print(
                f"{self.name}: played ({move['row']}, {move['col']}) in {game['game_id']} "
                f"-> status {game['status']} score {game['scores']['black']}:{game['scores']['white']}"
            )
            time.sleep(self.poll_interval)

    def _request(self, path: str, *, method: str = "GET", body: dict[str, object] | None = None) -> dict[str, object]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.server_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        while True:
            self._wait_for_request_slot()
            self._last_request_monotonic = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8") or "{}")
                    self._update_request_interval_from_payload(payload)
                    return payload
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                retry_after_seconds = self._retry_after_seconds(exc)
                print(f"{self.name}: rate limited, waiting {retry_after_seconds:.3f} seconds")
                time.sleep(retry_after_seconds)

    def _wait_for_request_slot(self) -> None:
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = self._minimum_request_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _retry_after_seconds(self, exc: urllib.error.HTTPError) -> float:
        raw_body = exc.read().decode("utf-8") or "{}"
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            payload = {}

        self._update_request_interval_from_payload(payload)

        retry_after_seconds = payload.get("retry_after_seconds")
        if isinstance(retry_after_seconds, (int, float)) and retry_after_seconds > 0:
            return float(retry_after_seconds)

        retry_after_header = exc.headers.get("Retry-After")
        if retry_after_header is not None:
            try:
                parsed = float(retry_after_header)
            except ValueError:
                parsed = 0.0
            if parsed > 0:
                return parsed

        return EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS

    def _update_request_interval_from_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        limit_seconds = payload.get("limit_seconds")
        if isinstance(limit_seconds, (int, float)) and limit_seconds > 0:
            self._minimum_request_interval = float(limit_seconds)
            self.poll_interval = float(limit_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect a random tournament bot to the Reversi workshop server.")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="Base URL of the Reversi server.")
    parser.add_argument("--name", default="random-bot", help="Display name used during registration.")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS,
        help="Seconds to wait between assignment polls. The bot also enforces the server's 0.25 second minimum between all requests.",
    )
    parser.add_argument("--seed", type=int, default=None, help="Optional random seed.")
    parser.add_argument("--reservation-token", default=None, help="Reserved token used by the operator spawn flow.")
    args = parser.parse_args()

    TournamentRandomBot(
        server_url=args.server,
        name=args.name,
        poll_interval=args.poll_interval,
        seed=args.seed,
        reservation_token=args.reservation_token,
    ).run_forever()


if __name__ == "__main__":
    main()


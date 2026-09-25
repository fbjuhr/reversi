from __future__ import annotations

import importlib.util
import logging
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

from werkzeug.serving import make_server

from reversi.app import create_app

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "examples" / "remote_random_bot.py"


def load_example_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("remote_random_bot_example", EXAMPLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


example = load_example_module()


class FakeHeaders:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str) -> str | None:
        return self._values.get(key)


class FakeHttpError(Exception):
    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__("fake")
        self.headers = FakeHeaders(headers)


class ExampleBotPacingTests(unittest.TestCase):
    def make_bot(self, **kwargs: Any) -> Any:
        kwargs.setdefault("quiet", True)
        return example.ReversiBot("http://127.0.0.1:8000", "alpha", **kwargs)

    def test_starts_with_the_documented_default_interval(self) -> None:
        self.assertEqual(example.DEFAULT_REQUEST_INTERVAL_SECONDS, 0.25)
        self.assertEqual(self.make_bot().request_interval_seconds, 0.25)

    def test_adopts_limit_seconds_reported_by_the_server(self) -> None:
        bot = self.make_bot()

        bot._adopt_server_pacing({"available": False, "limit_seconds": 1.5})

        self.assertEqual(bot.request_interval_seconds, 1.5)

    def test_ignores_missing_or_invalid_limit_seconds(self) -> None:
        bot = self.make_bot()

        for payload in ({}, {"limit_seconds": 0}, {"limit_seconds": -1}, {"limit_seconds": "fast"}):
            bot._adopt_server_pacing(payload)
            self.assertEqual(bot.request_interval_seconds, 0.25)

    def test_prefers_retry_after_seconds_from_the_body(self) -> None:
        bot = self.make_bot()
        error = FakeHttpError({"Retry-After": "1"})

        wait = bot._retry_after_seconds(error, {"retry_after_seconds": 0.137, "limit_seconds": 0.25})

        self.assertAlmostEqual(wait, 0.137, places=3)

    def test_rounded_zero_retry_after_does_not_fall_back_to_the_coarse_header(self) -> None:
        bot = self.make_bot()
        error = FakeHttpError({"Retry-After": "1"})

        wait = bot._retry_after_seconds(error, {"retry_after_seconds": 0.0})

        self.assertGreater(wait, 0)
        self.assertLess(wait, 1.0)

    def test_falls_back_to_the_retry_after_header(self) -> None:
        bot = self.make_bot()
        error = FakeHttpError({"Retry-After": "3"})

        self.assertAlmostEqual(bot._retry_after_seconds(error, {}), 3.0, places=3)

    def test_falls_back_to_the_current_interval_without_any_hint(self) -> None:
        bot = self.make_bot(request_interval_seconds=0.75)
        error = FakeHttpError({})

        self.assertAlmostEqual(bot._retry_after_seconds(error, {}), 0.75, places=3)

    def test_waits_the_server_interval_between_requests(self) -> None:
        bot = self.make_bot(request_interval_seconds=0.5)
        bot._last_request_monotonic = time.monotonic() - 0.2

        with patch.object(example.time, "sleep") as sleep:
            bot._sleep_until_next_request()

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.3, places=1)

    def test_does_not_wait_before_the_first_request(self) -> None:
        bot = self.make_bot()

        with patch.object(example.time, "sleep") as sleep:
            bot._sleep_until_next_request()

        sleep.assert_not_called()

    def test_chooses_a_move_from_the_fetched_legal_moves(self) -> None:
        bot = self.make_bot(seed=1)
        legal_moves = [{"row": 2, "col": 3}, {"row": 3, "col": 2}]

        move = bot.choose_move({"available": True, "legal_moves": legal_moves})

        self.assertIn(move, legal_moves)

    def test_returns_no_move_when_the_server_sends_none(self) -> None:
        bot = self.make_bot()

        self.assertIsNone(bot.choose_move({"available": True, "legal_moves": []}))
        self.assertIsNone(bot.choose_move({"available": True}))


class ExampleBotLiveServerTests(unittest.TestCase):
    """Run the example bot against a real HTTP server, like a workshop participant would."""

    operator_password = "swordfish"
    bot_request_interval_ms = 10

    def setUp(self) -> None:
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self.app = create_app(operator_password=self.operator_password)
        self.app.config.update(TESTING=True)
        self.game_manager = self.app.config["game_manager"]
        self.tournament_manager = self.app.config["tournament_manager"]
        self.tournament_manager.set_external_bot_request_interval_milliseconds(self.bot_request_interval_ms)

        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.server_url = f"http://127.0.0.1:{self.server.server_port}"
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server_thread.join(timeout=5)
        self.server.server_close()

    def start_bot(self, name: str, seed: int) -> Any:
        bot = example.ReversiBot(self.server_url, name, seed=seed, quiet=True)
        bot.register()
        thread = threading.Thread(target=self.run_until_stopped, args=(bot,), daemon=True)
        bot.stop_event = threading.Event()
        thread.start()
        self.addCleanup(bot.stop_event.set)
        self.addCleanup(thread.join, 5)
        return bot

    @staticmethod
    def run_until_stopped(bot: Any) -> None:
        while not bot.stop_event.is_set():
            assignment = bot.fetch_assignment()
            if assignment.get("available") is not True:
                bot._sleep_until_next_request()
                continue
            move = bot.choose_move(assignment)
            if move is None:
                bot._sleep_until_next_request()
                continue
            bot.submit_move(str(assignment["game_id"]), move)

    def wait_for_tournament(self, tournament_id: str, timeout_seconds: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            tournament = self.tournament_manager.get_tournament(tournament_id)
            if tournament["status"] == "finished":
                return tournament
            time.sleep(0.1)
        self.fail(f"Tournament {tournament_id} did not finish within {timeout_seconds} seconds.")

    def test_registered_example_bots_play_a_tournament_to_completion(self) -> None:
        alpha = self.start_bot("alpha", seed=1)
        self.start_bot("beta", seed=2)

        started = self.tournament_manager.start_tournament(tournament_format="single_round_robin")
        tournament = self.wait_for_tournament(str(started["tournament_id"]))

        self.assertEqual(tournament["completed_match_count"], tournament["match_count"])
        for match in tournament["matches"]:
            self.assertEqual(match["status"], "finished")
            self.assertGreater(match["scores"]["black"] + match["scores"]["white"], 4)
            game = self.game_manager.get_game(match["game_id"]).to_dict()
            self.assertEqual(
                game["termination_reason"],
                "no_legal_moves",
                "The match should end because the board ran out of moves, not because a bot forfeited.",
            )

        # The bots started on the 0.25s default and learned the live limit from the responses.
        self.assertAlmostEqual(alpha.request_interval_seconds, self.bot_request_interval_ms / 1000.0, places=4)

    def test_bot_follows_a_rate_limit_change_made_during_the_tournament(self) -> None:
        alpha = example.ReversiBot(self.server_url, "alpha", seed=1, quiet=True)
        alpha.register()
        self.assertEqual(alpha.request_interval_seconds, example.DEFAULT_REQUEST_INTERVAL_SECONDS)

        alpha.fetch_assignment()
        self.assertAlmostEqual(alpha.request_interval_seconds, self.bot_request_interval_ms / 1000.0, places=4)

        self.tournament_manager.set_external_bot_request_interval_milliseconds(120)
        alpha.fetch_assignment()

        self.assertAlmostEqual(alpha.request_interval_seconds, 0.12, places=4)

    def test_bot_recovers_when_it_polls_faster_than_the_server_allows(self) -> None:
        alpha = example.ReversiBot(self.server_url, "alpha", seed=1, quiet=True)
        alpha.register()
        alpha.fetch_assignment()

        self.tournament_manager.set_external_bot_request_interval_milliseconds(400)

        # Pretend the bot still believes in the old, much faster interval.
        alpha.request_interval_seconds = 0.01
        started = time.monotonic()
        assignment = alpha.fetch_assignment()
        elapsed = time.monotonic() - started

        self.assertFalse(assignment["available"])
        self.assertEqual(assignment["limit_seconds"], 0.4)
        self.assertAlmostEqual(alpha.request_interval_seconds, 0.4, places=4)
        self.assertGreater(elapsed, 0.1)


if __name__ == "__main__":
    unittest.main()

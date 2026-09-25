from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from reversi.manager import BOT_TURN_TIMEOUT_SECONDS, GameManager
from reversi.tournament import (
    EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS,
    ExternalBotRateLimitError,
    ManagedSpawn,
    RegisteredBot,
    TournamentManager,
)


class DummyProcess:
    def __init__(self, pid: int = 9999) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class TournamentManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = TournamentManager(GameManager())

    def register_bot(self, name: str) -> dict[str, object]:
        return self.manager.register_bot(name)

    def age_inactive_matches(self, tournament: dict[str, object]) -> None:
        for match in tournament["matches"]:
            if match["active"]:
                continue
            managed = self.manager._game_manager.get_game(match["game_id"])
            managed.turn_started_monotonic = time.monotonic() - BOT_TURN_TIMEOUT_SECONDS - 1

    def test_reserved_spawn_name_requires_matching_token(self) -> None:
        process = DummyProcess()
        self.manager._pending_spawns["alpha"] = ManagedSpawn(
            name="alpha",
            reservation_token="token-123",
            process=process,
        )

        with self.assertRaises(ValueError):
            self.manager.register_bot("alpha", reservation_token="wrong-token")

        registered = self.manager.register_bot("alpha", reservation_token="token-123")
        self.assertEqual(registered["name"], "alpha")
        self.assertTrue(registered["managed"])
        self.assertEqual(registered["pid"], process.pid)

    def test_spawn_bots_tracks_managed_process(self) -> None:
        process = DummyProcess(pid=4242)

        def fake_wait(name: str, timeout_seconds: float) -> RegisteredBot:
            bot = RegisteredBot(bot_id="bot-1", name=name, secret="secret")
            self.manager._bots[bot.bot_id] = bot
            pending = self.manager._pending_spawns[name.casefold()]
            pending.bot_id = bot.bot_id
            self.manager._managed_bot_processes[bot.bot_id] = pending
            self.manager._pending_spawns.pop(name.casefold(), None)
            return bot

        with patch("reversi.tournament.subprocess.Popen", return_value=process):
            with patch.object(self.manager, "_wait_for_bot_registration", side_effect=fake_wait):
                result = self.manager.spawn_bots(
                    name_prefix="local-bot",
                    count=1,
                    server_url="http://127.0.0.1:8000",
                )

        self.assertEqual(len(result["spawned"]), 1)
        spawned = result["spawned"][0]
        self.assertEqual(spawned["name"], "local-bot")
        self.assertTrue(spawned["managed"])
        self.assertTrue(spawned["process_alive"])
        self.assertEqual(spawned["pid"], 4242)

    def test_authenticated_bot_requests_are_rate_limited(self) -> None:
        alpha = self.register_bot("alpha")

        with patch("reversi.tournament.time.monotonic", side_effect=[100.0, 100.10, 100.30]):
            first = self.manager.get_assignment(bot_id=str(alpha["bot_id"]), secret=str(alpha["secret"]))
            self.assertFalse(first["available"])

            with self.assertRaises(ExternalBotRateLimitError) as context:
                self.manager.get_assignment(bot_id=str(alpha["bot_id"]), secret=str(alpha["secret"]))

            self.assertAlmostEqual(
                context.exception.retry_after_seconds,
                EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS - 0.10,
                places=3,
            )

            second = self.manager.get_assignment(bot_id=str(alpha["bot_id"]), secret=str(alpha["secret"]))
            self.assertFalse(second["available"])

    def test_request_interval_can_change_during_tournament(self) -> None:
        alpha = self.register_bot("alpha")
        settings = self.manager.set_external_bot_request_interval_milliseconds(500)
        self.assertEqual(settings["external_bot_request_interval_ms"], 500)

        with patch("reversi.tournament.time.monotonic", side_effect=[100.0, 100.40, 100.60]):
            first = self.manager.get_assignment(bot_id=str(alpha["bot_id"]), secret=str(alpha["secret"]))
            self.assertEqual(first["limit_seconds"], 0.5)

            with self.assertRaises(ExternalBotRateLimitError) as context:
                self.manager.get_assignment(bot_id=str(alpha["bot_id"]), secret=str(alpha["secret"]))

            self.assertAlmostEqual(context.exception.limit_seconds, 0.5, places=3)
            self.assertAlmostEqual(context.exception.retry_after_seconds, 0.1, places=3)

            second = self.manager.get_assignment(bot_id=str(alpha["bot_id"]), secret=str(alpha["secret"]))
            self.assertEqual(second["limit_seconds"], 0.5)

    def test_tournament_limits_active_matches_to_non_overlapping_bots(self) -> None:
        alpha = self.register_bot("alpha")
        beta = self.register_bot("beta")
        gamma = self.register_bot("gamma")
        delta = self.register_bot("delta")

        tournament = self.manager.start_tournament(
            bot_ids=[alpha["bot_id"], beta["bot_id"], gamma["bot_id"], delta["bot_id"]],
            double_round_robin=False,
        )

        self.assertEqual(tournament["match_count"], 6)
        self.assertEqual(tournament["ongoing_match_count"], 2)
        self.assertEqual(len(tournament["ongoing_matches"]), 2)

        active_bot_ids = {
            match["black"]["bot_id"]
            for match in tournament["ongoing_matches"]
        } | {
            match["white"]["bot_id"]
            for match in tournament["ongoing_matches"]
        }
        self.assertEqual(active_bot_ids, {alpha["bot_id"], beta["bot_id"], gamma["bot_id"], delta["bot_id"]})

    def test_tournament_schedules_next_batch_when_bots_become_free(self) -> None:
        alpha = self.register_bot("alpha")
        beta = self.register_bot("beta")
        gamma = self.register_bot("gamma")
        delta = self.register_bot("delta")

        started = self.manager.start_tournament(
            bot_ids=[alpha["bot_id"], beta["bot_id"], gamma["bot_id"], delta["bot_id"]],
            double_round_robin=False,
        )
        tournament_id = str(started["tournament_id"])
        first_batch_ids = [match["game_id"] for match in started["ongoing_matches"]]
        self.assertEqual(len(first_batch_ids), 2)

        first_active = self.manager._game_manager.get_game(first_batch_ids[0])
        second_active = self.manager._game_manager.get_game(first_batch_ids[1])
        first_active.game.forfeit("B", "test_complete")

        mid_tournament = self.manager.get_tournament(tournament_id)
        self.assertEqual(mid_tournament["ongoing_match_count"], 1)
        self.assertEqual(mid_tournament["ongoing_matches"][0]["game_id"], second_active.game_id)

        second_active.game.forfeit("B", "test_complete")

        next_batch = self.manager.get_tournament(tournament_id)
        self.assertEqual(next_batch["ongoing_match_count"], 2)
        next_batch_ids = [match["game_id"] for match in next_batch["ongoing_matches"]]
        self.assertTrue(set(first_batch_ids).isdisjoint(next_batch_ids))

    def test_round_robin_queued_matches_do_not_timeout_before_activation(self) -> None:
        alpha = self.register_bot("alpha")
        beta = self.register_bot("beta")
        gamma = self.register_bot("gamma")
        delta = self.register_bot("delta")

        started = self.manager.start_tournament(
            bot_ids=[alpha["bot_id"], beta["bot_id"], gamma["bot_id"], delta["bot_id"]],
            double_round_robin=False,
        )
        tournament_id = str(started["tournament_id"])
        self.age_inactive_matches(started)

        for match in started["ongoing_matches"]:
            self.manager._game_manager.get_game(match["game_id"]).game.forfeit("B", "test_complete")

        scheduled = self.manager.get_tournament(tournament_id)
        self.assertEqual(scheduled["completed_match_count"], 2)
        self.assertEqual(scheduled["ongoing_match_count"], 2)
        self.assertTrue(all(match["status"] == "active" for match in scheduled["ongoing_matches"]))

        resynced = self.manager.get_tournament(tournament_id)
        self.assertEqual(resynced["completed_match_count"], 2)
        self.assertEqual(resynced["ongoing_match_count"], 2)
        self.assertTrue(all(match["status"] == "active" for match in resynced["ongoing_matches"]))

    def test_single_elimination_creates_final_after_semifinals_finish(self) -> None:
        alpha = self.register_bot("alpha")
        beta = self.register_bot("beta")
        gamma = self.register_bot("gamma")
        delta = self.register_bot("delta")

        tournament = self.manager.start_tournament(
            bot_ids=[alpha["bot_id"], beta["bot_id"], gamma["bot_id"], delta["bot_id"]],
            tournament_format="single_elimination",
        )

        self.assertEqual(tournament["format"], "single_elimination")
        self.assertEqual(tournament["match_count"], 3)
        self.assertEqual(tournament["ongoing_match_count"], 2)
        self.assertEqual({match["round_name"] for match in tournament["ongoing_matches"]}, {"Semifinal"})
        self.assertIsNotNone(tournament["bracket"])
        self.assertIsNone(tournament["champion"])
        self.assertEqual(len(tournament["bracket"]["rounds"]), 2)
        self.assertEqual(tournament["bracket"]["rounds"][0]["round_name"], "Semifinal")
        self.assertEqual(tournament["bracket"]["rounds"][1]["round_name"], "Final")
        self.assertEqual(tournament["bracket"]["rounds"][1]["matches"][0]["black"]["label"], "Winner of Match #1")
        self.assertEqual(tournament["bracket"]["rounds"][1]["matches"][0]["white"]["label"], "Winner of Match #2")

        semifinal_ids = [match["game_id"] for match in tournament["ongoing_matches"]]
        for game_id in semifinal_ids:
            self.manager._game_manager.get_game(game_id).game.forfeit("W", "test_complete")

        after_semis = self.manager.get_tournament(str(tournament["tournament_id"]))
        self.assertEqual(after_semis["ongoing_match_count"], 1)
        self.assertEqual(after_semis["completed_match_count"], 2)
        self.assertEqual(after_semis["ongoing_matches"][0]["round_name"], "Final")

        final_id = after_semis["ongoing_matches"][0]["game_id"]
        self.manager._game_manager.get_game(final_id).game.forfeit("W", "test_complete")

        finished = self.manager.get_tournament(str(tournament["tournament_id"]))
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(finished["completed_match_count"], 3)
        self.assertEqual(len(finished["matches"]), 3)
        self.assertEqual(finished["matches"][-1]["round_name"], "Final")
        self.assertEqual(finished["champion"]["name"], alpha["name"])
        self.assertEqual(finished["bracket"]["winner"]["bot_id"], alpha["bot_id"])
        self.assertEqual(finished["bracket"]["rounds"][-1]["matches"][0]["winner_bot_id"], alpha["bot_id"])

    def test_single_elimination_with_odd_bot_count_waits_for_previous_winner(self) -> None:
        alpha = self.register_bot("alpha")
        beta = self.register_bot("beta")
        gamma = self.register_bot("gamma")

        tournament = self.manager.start_tournament(
            bot_ids=[alpha["bot_id"], beta["bot_id"], gamma["bot_id"]],
            tournament_format="single_elimination",
        )

        self.assertEqual(tournament["match_count"], 2)
        self.assertEqual(tournament["ongoing_match_count"], 1)
        self.assertEqual(len(tournament["matches"]), 1)
        self.assertEqual(tournament["ongoing_matches"][0]["round_name"], "Semifinal")
        self.assertIsNotNone(tournament["bracket"])
        self.assertEqual(tournament["bracket"]["rounds"][-1]["matches"][0]["white"]["name"], gamma["name"])
        self.assertEqual(tournament["bracket"]["rounds"][-1]["matches"][0]["black"]["label"], "Winner of Match #1")

        semifinal_id = tournament["ongoing_matches"][0]["game_id"]
        self.manager._game_manager.get_game(semifinal_id).game.forfeit("W", "test_complete")

        after_semifinal = self.manager.get_tournament(str(tournament["tournament_id"]))
        self.assertEqual(after_semifinal["ongoing_match_count"], 1)
        self.assertEqual(after_semifinal["completed_match_count"], 1)
        self.assertEqual(len(after_semifinal["matches"]), 2)
        self.assertEqual(after_semifinal["ongoing_matches"][0]["round_name"], "Final")
        finalists = {
            after_semifinal["ongoing_matches"][0]["black"]["bot_id"],
            after_semifinal["ongoing_matches"][0]["white"]["bot_id"],
        }
        self.assertEqual(finalists, {alpha["bot_id"], gamma["bot_id"]})
        final_match = after_semifinal["bracket"]["rounds"][-1]["matches"][0]
        self.assertEqual(final_match["black"]["name"], alpha["name"])
        self.assertEqual(final_match["white"]["name"], gamma["name"])


if __name__ == "__main__":
    unittest.main()


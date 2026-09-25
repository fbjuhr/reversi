from __future__ import annotations

import time
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from reversi.game import Move
from reversi.manager import GameManager
from reversi.tournament import TournamentManager


@dataclass
class SlowAutonomousPlayer:
    delay_seconds: float = 0.05
    player_type: str = "slow"
    is_human: bool = False
    is_autonomous: bool = True

    def choose_move(
        self,
        *,
        game_id: str,
        color: str,
        state: dict[str, object],
        legal_moves: list[Move],
    ) -> Move | None:
        time.sleep(self.delay_seconds)
        return legal_moves[0] if legal_moves else None


class GameManagerTimeoutTests(unittest.TestCase):
    def test_autonomous_bot_forfeits_after_move_timeout(self) -> None:
        manager = GameManager()
        managed = manager.create_game(
            black={"type": "random", "seed": 1},
            white={"type": "human"},
            auto_advance=False,
        )
        managed.players["B"] = SlowAutonomousPlayer(delay_seconds=0.05)

        with patch("reversi.manager.BOT_TURN_TIMEOUT_SECONDS", 0.01):
            manager.auto_advance(managed.game_id)

        self.assertEqual(managed.game.status, "finished")
        self.assertEqual(managed.game.winner, "W")
        self.assertEqual(managed.game.termination_reason, "bot_timeout_exceeded")

    def test_tournament_bot_forfeits_after_turn_timeout(self) -> None:
        game_manager = GameManager()
        tournament_manager = TournamentManager(game_manager)
        alpha = tournament_manager.register_bot("alpha")
        beta = tournament_manager.register_bot("beta")
        tournament = tournament_manager.start_tournament(
            bot_ids=[alpha["bot_id"], beta["bot_id"]],
            double_round_robin=False,
        )
        game_id = tournament["ongoing_matches"][0]["game_id"]
        managed = game_manager.get_game(game_id)

        with patch("reversi.manager.BOT_TURN_TIMEOUT_SECONDS", 0.01):
            managed.turn_started_monotonic -= 1.0
            synced = tournament_manager.get_tournament(str(tournament["tournament_id"]))

        self.assertEqual(managed.game.status, "finished")
        self.assertEqual(managed.game.winner, "W")
        self.assertEqual(managed.game.termination_reason, "bot_timeout_exceeded")
        self.assertEqual(synced["status"], "finished")
        self.assertEqual(synced["completed_match_count"], 1)


if __name__ == "__main__":
    unittest.main()


from __future__ import annotations

import unittest

from reversi.game import BLACK, EMPTY, WHITE, InvalidMoveError, ReversiGame


class ReversiGameTests(unittest.TestCase):
    def test_initial_legal_moves_for_black(self) -> None:
        game = ReversiGame()
        moves = {(move.row, move.col) for move in game.legal_moves()}
        self.assertEqual(moves, {(2, 3), (3, 2), (4, 5), (5, 4)})

    def test_play_move_flips_pieces_and_switches_turn(self) -> None:
        game = ReversiGame()
        game.play_move(2, 3)
        self.assertEqual(game.board[2][3], BLACK)
        self.assertEqual(game.board[3][3], BLACK)
        self.assertEqual(game.current_player, WHITE)
        self.assertEqual(game.counts(), {BLACK: 4, WHITE: 1})

    def test_pass_turn_only_when_no_legal_moves_exist(self) -> None:
        game = ReversiGame(
            board=[[BLACK for _ in range(8)] for _ in range(8)],
            current_player=WHITE,
        )
        game.board[3][3] = WHITE
        game.board[3][4] = EMPTY

        self.assertEqual(game.legal_moves(), [])
        self.assertEqual([(move.row, move.col) for move in game.legal_moves_for(BLACK)], [(3, 4)])

        game.pass_turn()
        self.assertEqual(game.current_player, BLACK)
        self.assertTrue(game.move_history[-1]["pass"])

    def test_cannot_play_illegal_move(self) -> None:
        game = ReversiGame()
        with self.assertRaises(InvalidMoveError):
            game.play_move(0, 0)

    def test_forfeit_sets_winner(self) -> None:
        game = ReversiGame()
        game.forfeit(BLACK, "bot_error")
        self.assertEqual(game.status, "finished")
        self.assertEqual(game.winner, WHITE)
        self.assertEqual(game.termination_reason, "bot_error")


if __name__ == "__main__":
    unittest.main()


from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

BOARD_SIZE = 8
BLACK = "B"
WHITE = "W"
EMPTY = "."
DIRECTIONS = [
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
]


class InvalidMoveError(ValueError):
    """Raised when a move is not legal for the current state."""


@dataclass(frozen=True)
class Move:
    row: int
    col: int

    def to_dict(self) -> dict[str, int]:
        return {"row": self.row, "col": self.col}


@dataclass
class ReversiGame:
    board: list[list[str]] = field(default_factory=list)
    current_player: str = BLACK
    status: str = "active"
    winner: str | None = None
    termination_reason: str | None = None
    move_history: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.board:
            self.board = [[EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
            middle = BOARD_SIZE // 2
            self.board[middle - 1][middle - 1] = WHITE
            self.board[middle - 1][middle] = BLACK
            self.board[middle][middle - 1] = BLACK
            self.board[middle][middle] = WHITE

    @staticmethod
    def opponent(player: str) -> str:
        return WHITE if player == BLACK else BLACK

    @staticmethod
    def inside_board(row: int, col: int) -> bool:
        return 0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE

    def counts(self) -> dict[str, int]:
        black = sum(cell == BLACK for row in self.board for cell in row)
        white = sum(cell == WHITE for row in self.board for cell in row)
        return {BLACK: black, WHITE: white}

    def captures_for_move(self, row: int, col: int, player: str) -> list[tuple[int, int]]:
        if not self.inside_board(row, col) or self.board[row][col] != EMPTY:
            return []

        opponent = self.opponent(player)
        flips: list[tuple[int, int]] = []
        for row_delta, col_delta in DIRECTIONS:
            path: list[tuple[int, int]] = []
            r, c = row + row_delta, col + col_delta
            while self.inside_board(r, c) and self.board[r][c] == opponent:
                path.append((r, c))
                r += row_delta
                c += col_delta
            if path and self.inside_board(r, c) and self.board[r][c] == player:
                flips.extend(path)
        return flips

    def legal_moves_for(self, player: str) -> list[Move]:
        moves: list[Move] = []
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                if self.captures_for_move(row, col, player):
                    moves.append(Move(row, col))
        return moves

    def legal_moves(self) -> list[Move]:
        return self.legal_moves_for(self.current_player)

    def play_move(self, row: int, col: int) -> None:
        if self.status != "active":
            raise InvalidMoveError("Game is already finished.")

        flips = self.captures_for_move(row, col, self.current_player)
        if not flips:
            raise InvalidMoveError(f"Move ({row}, {col}) is not legal for {self.current_player}.")

        mover = self.current_player
        self.board[row][col] = mover
        for flip_row, flip_col in flips:
            self.board[flip_row][flip_col] = mover

        self.move_history.append(
            {
                "player": mover,
                "move": {"row": row, "col": col},
                "flipped": [{"row": r, "col": c} for r, c in flips],
            }
        )
        self._advance_after_turn(mover)

    def pass_turn(self) -> None:
        if self.status != "active":
            raise InvalidMoveError("Game is already finished.")
        if self.legal_moves():
            raise InvalidMoveError("Cannot pass when legal moves exist.")

        mover = self.current_player
        self.move_history.append({"player": mover, "move": None, "pass": True})
        self._advance_after_turn(mover)

    def forfeit(self, loser: str, reason: str) -> None:
        self.status = "finished"
        self.winner = self.opponent(loser)
        self.termination_reason = reason
        self.move_history.append({"player": loser, "move": None, "forfeit": True, "reason": reason})

    def _advance_after_turn(self, mover: str) -> None:
        next_player = self.opponent(mover)
        next_moves = self.legal_moves_for(next_player)
        mover_moves = self.legal_moves_for(mover)

        if next_moves:
            self.current_player = next_player
            return
        if mover_moves:
            self.current_player = mover
            return

        self.status = "finished"
        scores = self.counts()
        if scores[BLACK] > scores[WHITE]:
            self.winner = BLACK
        elif scores[WHITE] > scores[BLACK]:
            self.winner = WHITE
        else:
            self.winner = "draw"
        self.termination_reason = "no_legal_moves"

    def to_dict(self, game_id: str | None = None) -> dict[str, object]:
        scores = self.counts()
        legal = self.legal_moves()
        payload: dict[str, object] = {
            "board": [row[:] for row in self.board],
            "current_player": self.current_player,
            "status": self.status,
            "winner": self.winner,
            "termination_reason": self.termination_reason,
            "scores": {"black": scores[BLACK], "white": scores[WHITE]},
            "legal_moves": [move.to_dict() for move in legal],
            "move_count": len(self.move_history),
            "history": self.move_history,
        }
        if game_id is not None:
            payload["game_id"] = game_id
        return payload

    def clone_board_rows(self) -> Iterable[str]:
        return ("".join(row) for row in self.board)


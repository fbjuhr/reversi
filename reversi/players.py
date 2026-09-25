from __future__ import annotations

import json
import random
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .game import Move


class Player(Protocol):
    player_type: str
    is_human: bool
    is_autonomous: bool

    def choose_move(
        self,
        *,
        game_id: str,
        color: str,
        state: dict[str, object],
        legal_moves: list[Move],
    ) -> Move | None: ...


@dataclass
class HumanPlayer:
    player_type: str = "human"
    is_human: bool = True
    is_autonomous: bool = False

    def choose_move(
        self,
        *,
        game_id: str,
        color: str,
        state: dict[str, object],
        legal_moves: list[Move],
    ) -> Move | None:
        raise RuntimeError("Human players do not auto-select moves.")


@dataclass
class RandomPlayer:
    seed: int | None = None
    player_type: str = "random"
    is_human: bool = False
    is_autonomous: bool = True

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def choose_move(
        self,
        *,
        game_id: str,
        color: str,
        state: dict[str, object],
        legal_moves: list[Move],
    ) -> Move | None:
        if not legal_moves:
            return None
        return self._rng.choice(legal_moves)


@dataclass
class GreedyPlayer:
    player_type: str = "greedy"
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
        if not legal_moves:
            return None
        # Prefer corners, then edges, then top-left deterministic order.
        return min(
            legal_moves,
            key=lambda move: (
                0 if (move.row, move.col) in {(0, 0), (0, 7), (7, 0), (7, 7)} else 1,
                0 if move.row in {0, 7} or move.col in {0, 7} else 1,
                -_flip_count(state, move),
                move.row,
                move.col,
            ),
        )


@dataclass
class RemoteHttpPlayer:
    url: str
    timeout_seconds: float = 10.0
    player_type: str = "remote"
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
        payload = {
            "game_id": game_id,
            "you_are": color,
            "state": state,
            "legal_moves": [move.to_dict() for move in legal_moves],
        }
        data = json.dumps(payload).encode("utf-8")
        endpoint = self.url.rstrip("/") + "/move"
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Remote bot request failed: {exc}") from exc

        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Remote bot returned invalid JSON.") from exc

        if parsed.get("pass") is True:
            return None
        move_data = parsed.get("move", parsed)
        if move_data is None:
            return None
        if not isinstance(move_data, dict) or "row" not in move_data or "col" not in move_data:
            raise RuntimeError("Remote bot response must contain row and col.")
        return Move(int(move_data["row"]), int(move_data["col"]))


@dataclass
class TournamentBotPlayer:
    bot_id: str
    bot_name: str
    player_type: str = "tournament_bot"
    is_human: bool = False
    is_autonomous: bool = False

    def choose_move(
        self,
        *,
        game_id: str,
        color: str,
        state: dict[str, object],
        legal_moves: list[Move],
    ) -> Move | None:
        raise RuntimeError("Tournament bots submit moves back to the server instead of auto-playing.")


def _flip_count(state: dict[str, object], move: Move) -> int:
    board = state.get("board", [])
    if not isinstance(board, list):
        return 0
    player = state.get("current_player")
    opponent = "W" if player == "B" else "B"
    total = 0
    for row_delta, col_delta in [
        (-1, -1),
        (-1, 0),
        (-1, 1),
        (0, -1),
        (0, 1),
        (1, -1),
        (1, 0),
        (1, 1),
    ]:
        r, c = move.row + row_delta, move.col + col_delta
        path = 0
        while 0 <= r < 8 and 0 <= c < 8 and board[r][c] == opponent:
            path += 1
            r += row_delta
            c += col_delta
        if path and 0 <= r < 8 and 0 <= c < 8 and board[r][c] == player:
            total += path
    return total


def build_player(spec: dict[str, object] | None) -> Player:
    spec = spec or {"type": "human"}
    player_type = str(spec.get("type", "human")).lower()
    if player_type == "human":
        return HumanPlayer()
    if player_type == "random":
        seed = spec.get("seed")
        return RandomPlayer(seed=int(seed) if seed is not None else None)
    if player_type == "greedy":
        return GreedyPlayer()
    if player_type == "remote":
        url = spec.get("url")
        if not url:
            raise ValueError("Remote players require a 'url'.")
        timeout = float(spec.get("timeout_seconds", 10.0))
        return RemoteHttpPlayer(url=str(url), timeout_seconds=timeout)
    if player_type == "tournament_bot":
        bot_id = spec.get("bot_id")
        bot_name = spec.get("bot_name")
        if not bot_id or not bot_name:
            raise ValueError("Tournament bot players require 'bot_id' and 'bot_name'.")
        return TournamentBotPlayer(bot_id=str(bot_id), bot_name=str(bot_name))
    raise ValueError(f"Unsupported player type: {player_type}")


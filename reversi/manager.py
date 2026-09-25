from __future__ import annotations

import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable, TypeVar
from uuid import uuid4

from .game import InvalidMoveError, Move, ReversiGame
from .players import Player, build_player


BOT_TURN_TIMEOUT_SECONDS = 10.0


class BotMoveTimeoutError(TimeoutError):
    """Raised when a bot exceeds the per-turn thinking limit."""


T = TypeVar("T")


def _run_with_timeout(callback: Callable[[], T], *, timeout_seconds: float) -> T:
    if timeout_seconds <= 0:
        raise BotMoveTimeoutError("Bot move timed out.")

    if (
        threading.current_thread() is threading.main_thread()
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and hasattr(signal, "ITIMER_REAL")
    ):
        previous_handler = signal.getsignal(signal.SIGALRM)

        def _handle_timeout(signum: int, frame: object) -> None:  # noqa: ARG001
            raise BotMoveTimeoutError("Bot move timed out.")

        signal.signal(signal.SIGALRM, _handle_timeout)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        try:
            return callback()
        finally:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            signal.signal(signal.SIGALRM, previous_handler)

    result: dict[str, T] = {}
    error: dict[str, Exception] = {}
    completed = threading.Event()

    def _target() -> None:
        try:
            result["value"] = callback()
        except Exception as exc:  # noqa: BLE001 - propagate bot failures to caller.
            error["value"] = exc
        finally:
            completed.set()

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    if not completed.wait(timeout_seconds):
        raise BotMoveTimeoutError("Bot move timed out.")
    if "value" in error:
        raise error["value"]
    return result["value"]


@dataclass
class ManagedGame:
    game_id: str
    game: ReversiGame
    players: dict[str, Player]
    player_specs: dict[str, dict[str, object]]
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    turn_started_monotonic: float = field(default_factory=time.monotonic, repr=False)

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat()

    def restart_turn_timer(self) -> None:
        self.turn_started_monotonic = time.monotonic()

    def to_dict(self) -> dict[str, object]:
        payload = self.game.to_dict(self.game_id)
        payload["created_at"] = self.created_at
        payload["updated_at"] = self.updated_at
        payload["players"] = {
            "black": self.player_specs["B"],
            "white": self.player_specs["W"],
        }
        payload["metadata"] = self.metadata
        return payload


class GameManager:
    def __init__(self) -> None:
        self._games: dict[str, ManagedGame] = {}

    def create_game(
        self,
        black: dict[str, object] | None = None,
        white: dict[str, object] | None = None,
        *,
        auto_advance: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> ManagedGame:
        game_id = uuid4().hex[:8]
        player_specs = {"B": black or {"type": "human"}, "W": white or {"type": "greedy"}}
        managed = ManagedGame(
            game_id=game_id,
            game=ReversiGame(),
            players={
                "B": build_player(player_specs["B"]),
                "W": build_player(player_specs["W"]),
            },
            player_specs=player_specs,
            metadata=metadata or {},
        )
        self._games[game_id] = managed
        if auto_advance:
            self.auto_advance(game_id)
        return managed

    def list_games(self) -> list[dict[str, object]]:
        games = sorted(self._games.values(), key=lambda item: item.created_at, reverse=True)
        return [game.to_dict() for game in games]

    def get_game(self, game_id: str) -> ManagedGame:
        if game_id not in self._games:
            raise KeyError(f"Unknown game id: {game_id}")
        return self._games[game_id]

    def play_human_move(self, game_id: str, row: int, col: int) -> ManagedGame:
        managed = self.get_game(game_id)
        current_player = managed.game.current_player
        if managed.game.status != "active":
            raise InvalidMoveError("Game is already finished.")
        if not managed.players[current_player].is_human:
            raise InvalidMoveError("It is not a human-controlled turn.")
        managed.game.play_move(row, col)
        if managed.game.status == "active":
            managed.restart_turn_timer()
        managed.touch()
        self.auto_advance(game_id)
        return managed

    def play_external_move(self, game_id: str, row: int, col: int) -> ManagedGame:
        managed = self.get_game(game_id)
        current_player = managed.game.current_player
        if managed.game.status != "active":
            raise InvalidMoveError("Game is already finished.")
        player = managed.players[current_player]
        if player.is_human:
            raise InvalidMoveError("It is not an externally controlled turn.")
        if player.is_autonomous:
            raise InvalidMoveError("This player is auto-controlled by the server.")
        managed.game.play_move(row, col)
        if managed.game.status == "active":
            managed.restart_turn_timer()
        managed.touch()
        self.auto_advance(game_id)
        return managed

    def check_turn_timeout(self, game_id: str) -> ManagedGame:
        managed = self.get_game(game_id)
        self._forfeit_if_turn_timed_out(managed)
        return managed

    def auto_advance(self, game_id: str, max_turns: int = 200) -> ManagedGame:
        managed = self.get_game(game_id)
        turns = 0
        while managed.game.status == "active":
            if self._forfeit_if_turn_timed_out(managed):
                break
            turns += 1
            if turns > max_turns:
                managed.game.forfeit(managed.game.current_player, "turn_limit_exceeded")
                break

            player = managed.players[managed.game.current_player]
            legal_moves = managed.game.legal_moves()
            if not player.is_autonomous:
                break
            if not legal_moves:
                managed.game.pass_turn()
                if managed.game.status == "active":
                    managed.restart_turn_timer()
                managed.touch()
                continue

            state = managed.to_dict()
            try:
                move = _run_with_timeout(
                    lambda: player.choose_move(
                        game_id=managed.game_id,
                        color=managed.game.current_player,
                        state=state,
                        legal_moves=legal_moves,
                    ),
                    timeout_seconds=BOT_TURN_TIMEOUT_SECONDS,
                )
                self._apply_bot_move(managed, move, legal_moves)
            except BotMoveTimeoutError:
                managed.game.forfeit(managed.game.current_player, "bot_timeout_exceeded")
            except Exception as exc:  # noqa: BLE001 - surface bot errors inside game state.
                managed.game.forfeit(
                    managed.game.current_player,
                    f"bot_error: {type(exc).__name__}: {exc}",
                )
            managed.touch()
        return managed

    @staticmethod
    def _apply_bot_move(managed: ManagedGame, move: Move | None, legal_moves: list[Move]) -> None:
        legal_lookup = {(item.row, item.col) for item in legal_moves}
        if move is None:
            managed.game.forfeit(managed.game.current_player, "bot_returned_pass_with_legal_moves")
            return
        if (move.row, move.col) not in legal_lookup:
            managed.game.forfeit(managed.game.current_player, "bot_returned_illegal_move")
            return
        managed.game.play_move(move.row, move.col)
        if managed.game.status == "active":
            managed.restart_turn_timer()

    @staticmethod
    def _forfeit_if_turn_timed_out(managed: ManagedGame) -> bool:
        if managed.game.status != "active":
            return False
        player = managed.players[managed.game.current_player]
        if player.is_human:
            return False
        elapsed = time.monotonic() - managed.turn_started_monotonic
        if elapsed <= BOT_TURN_TIMEOUT_SECONDS:
            return False
        managed.game.forfeit(managed.game.current_player, "bot_timeout_exceeded")
        managed.touch()
        return True


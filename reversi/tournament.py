from __future__ import annotations

import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from uuid import uuid4

from .manager import GameManager, ManagedGame

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS = 0.25
SUPPORTED_TOURNAMENT_FORMATS = (
    "double_round_robin",
    "single_round_robin",
    "single_elimination",
)


class ExternalBotRateLimitError(RuntimeError):
    def __init__(self, retry_after_seconds: float, *, limit_seconds: float) -> None:
        self.retry_after_seconds = max(retry_after_seconds, 0.0)
        self.limit_seconds = max(limit_seconds, 0.0)
        super().__init__(
            f"Rate limit exceeded for this bot. Wait {self.retry_after_seconds:.3f} seconds before the next request."
        )


@dataclass
class RegisteredBot:
    bot_id: str
    name: str
    secret: str
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_seen_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: str = "connected"
    last_request_monotonic: float | None = None

    def touch(self) -> None:
        self.last_seen_at = datetime.now(UTC).isoformat()

    def to_public_dict(self) -> dict[str, object]:
        return {
            "bot_id": self.bot_id,
            "name": self.name,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "status": self.status,
        }

    def to_private_dict(self) -> dict[str, object]:
        payload = self.to_public_dict()
        payload["secret"] = self.secret
        return payload


@dataclass
class Tournament:
    tournament_id: str
    bot_ids: list[str]
    participants: dict[str, str]
    match_game_ids: list[str]
    active_game_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: str = "running"
    started_at: str | None = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    format: str = "double_round_robin"
    match_plan: list[dict[str, object]] = field(default_factory=list)


@dataclass
class ManagedSpawn:
    name: str
    reservation_token: str
    process: subprocess.Popen[bytes]
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    bot_id: str | None = None


class TournamentManager:
    def __init__(self, game_manager: GameManager) -> None:
        self._game_manager = game_manager
        self._bots: dict[str, RegisteredBot] = {}
        self._tournaments: dict[str, Tournament] = {}
        self._pending_spawns: dict[str, ManagedSpawn] = {}
        self._managed_bot_processes: dict[str, ManagedSpawn] = {}
        self._external_bot_request_interval_seconds = EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS

    @property
    def external_bot_request_interval_seconds(self) -> float:
        return self._external_bot_request_interval_seconds

    @property
    def external_bot_request_interval_milliseconds(self) -> int:
        return int(round(self._external_bot_request_interval_seconds * 1000))

    def get_external_bot_settings(self) -> dict[str, object]:
        return {
            "external_bot_request_interval_seconds": self.external_bot_request_interval_seconds,
            "external_bot_request_interval_ms": self.external_bot_request_interval_milliseconds,
            "applies_during_tournaments": True,
        }

    def set_external_bot_request_interval_milliseconds(self, value_ms: int | float) -> dict[str, object]:
        if value_ms <= 0:
            raise ValueError("external_bot_request_interval_ms must be greater than zero.")
        self._external_bot_request_interval_seconds = float(value_ms) / 1000.0
        return self.get_external_bot_settings()

    def register_bot(self, name: str, reservation_token: str | None = None) -> dict[str, object]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Bot name is required.")
        normalized_name = clean_name.casefold()
        if any(existing.name.casefold() == normalized_name for existing in self._bots.values()):
            raise ValueError(f"Bot name '{clean_name}' is already registered. Bot names must be unique.")
        pending_spawn = self._pending_spawns.get(normalized_name)
        if pending_spawn is not None and reservation_token != pending_spawn.reservation_token:
            raise ValueError(f"Bot name '{clean_name}' is already reserved. Bot names must be unique.")
        bot = RegisteredBot(
            bot_id=uuid4().hex[:8],
            name=clean_name,
            secret=secrets.token_urlsafe(24),
        )
        self._bots[bot.bot_id] = bot
        if pending_spawn is not None:
            pending_spawn.bot_id = bot.bot_id
            self._managed_bot_processes[bot.bot_id] = pending_spawn
            self._pending_spawns.pop(normalized_name, None)
        return self._serialize_private_bot(bot)

    def list_bots(self) -> list[dict[str, object]]:
        self._refresh_managed_processes()
        bots = sorted(self._bots.values(), key=lambda item: item.created_at)
        return [self._serialize_public_bot(bot) for bot in bots]

    def spawn_bots(
        self,
        *,
        name_prefix: str,
        count: int,
        server_url: str,
        poll_interval: float = 0.25,
        timeout_seconds: float = 5.0,
    ) -> dict[str, object]:
        clean_prefix = name_prefix.strip()
        if not clean_prefix:
            raise ValueError("A bot name or prefix is required.")
        if count < 1 or count > 32:
            raise ValueError("count must be between 1 and 32.")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero.")

        spawned: list[dict[str, object]] = []
        for index in range(count):
            name = self._allocate_spawn_name(clean_prefix, index + 1 if count > 1 else None)
            reservation_token = secrets.token_urlsafe(12)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "reversi.spawned_bot",
                    "--server",
                    server_url,
                    "--name",
                    name,
                    "--poll-interval",
                    str(poll_interval),
                    "--reservation-token",
                    reservation_token,
                ],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            managed = ManagedSpawn(name=name, reservation_token=reservation_token, process=process)
            self._pending_spawns[name.casefold()] = managed
            try:
                bot = self._wait_for_bot_registration(name, timeout_seconds)
            except Exception:
                self._pending_spawns.pop(name.casefold(), None)
                self._terminate_process(managed)
                raise
            spawned.append(self._serialize_public_bot(bot))

        return {"spawned": spawned}

    def remove_bot(self, bot_id: str) -> None:
        self._refresh_managed_processes()
        if bot_id not in self._bots:
            raise KeyError(f"Unknown bot id: {bot_id}")
        tournament = self._running_tournament()
        if tournament is not None and bot_id in tournament.bot_ids:
            raise RuntimeError("Cannot remove a bot that is participating in the running tournament.")
        managed = self._managed_bot_processes.pop(bot_id, None)
        if managed is not None:
            self._terminate_process(managed)
        del self._bots[bot_id]

    def list_tournaments(self) -> list[dict[str, object]]:
        tournaments = sorted(self._tournaments.values(), key=lambda item: item.created_at, reverse=True)
        for tournament in tournaments:
            self._sync_tournament(tournament)
        return [self._serialize_tournament(tournament) for tournament in tournaments]

    def get_tournament(self, tournament_id: str) -> dict[str, object]:
        tournament = self._lookup_tournament(tournament_id)
        self._sync_tournament(tournament)
        return self._serialize_tournament(tournament)

    def start_tournament(
        self,
        *,
        bot_ids: list[str] | None = None,
        tournament_format: str | None = None,
        double_round_robin: bool | None = None,
    ) -> dict[str, object]:
        if self._running_tournament() is not None:
            raise RuntimeError("A tournament is already running.")

        selected_ids = bot_ids or list(self._bots.keys())
        selected_ids = list(dict.fromkeys(selected_ids))
        if len(selected_ids) < 2:
            raise ValueError("At least two connected bots are required to start a tournament.")

        missing = [bot_id for bot_id in selected_ids if bot_id not in self._bots]
        if missing:
            raise KeyError(f"Unknown bot ids: {', '.join(missing)}")

        format_name = self._normalize_tournament_format(
            tournament_format=tournament_format,
            double_round_robin=double_round_robin,
        )
        participants = {bot_id: self._bots[bot_id].name for bot_id in selected_ids}
        tournament = Tournament(
            tournament_id=uuid4().hex[:8],
            bot_ids=selected_ids,
            participants=participants,
            match_game_ids=[],
            format=format_name,
        )

        if format_name == "single_elimination":
            tournament.match_plan = self._build_single_elimination_plan(selected_ids)
            self._materialize_tournament_games(tournament)
        else:
            pairings = self._build_pairings(selected_ids, tournament_format=format_name)
            for index, (black_bot_id, white_bot_id) in enumerate(pairings, start=1):
                self._create_tournament_game(
                    tournament,
                    black_bot_id=black_bot_id,
                    white_bot_id=white_bot_id,
                    match_index=index,
                )

        self._tournaments[tournament.tournament_id] = tournament
        self._schedule_games(tournament)
        return self._serialize_tournament(tournament)

    def get_assignment(self, *, bot_id: str, secret: str) -> dict[str, object]:
        bot = self._authenticate_bot(bot_id, secret)
        self._consume_external_request_slot(bot)
        bot.touch()

        tournament = self._running_tournament()
        if tournament is None:
            return {
                "bot": bot.to_public_dict(),
                "available": False,
                "limit_seconds": self.external_bot_request_interval_seconds,
                "message": "No running tournament.",
            }

        self._sync_tournament(tournament)
        if tournament.status != "running":
            return {
                "bot": bot.to_public_dict(),
                "available": False,
                "limit_seconds": self.external_bot_request_interval_seconds,
                "message": "Tournament finished.",
                "tournament": self._serialize_tournament(tournament),
            }

        active_game = self._active_game_for_bot(tournament, bot_id)
        if active_game is None:
            ongoing_matches = self._ongoing_managed_games(tournament)
            return {
                "bot": bot.to_public_dict(),
                "available": False,
                "limit_seconds": self.external_bot_request_interval_seconds,
                "message": "Waiting for the next match slot.",
                "tournament": self._serialize_tournament(tournament),
                "active_game_ids": [managed.game_id for managed in ongoing_matches],
            }

        current_spec = active_game.player_specs[active_game.game.current_player]
        assigned_bot_id = current_spec.get("bot_id")
        if assigned_bot_id != bot_id:
            return {
                "bot": bot.to_public_dict(),
                "available": False,
                "limit_seconds": self.external_bot_request_interval_seconds,
                "message": "Waiting for another bot's turn.",
                "tournament": self._serialize_tournament(tournament),
                "active_game_id": active_game.game_id,
            }

        color = active_game.game.current_player
        opponent_color = "W" if color == "B" else "B"
        opponent = active_game.player_specs[opponent_color]
        state = active_game.to_dict()
        return {
            "bot": bot.to_public_dict(),
            "available": True,
            "limit_seconds": self.external_bot_request_interval_seconds,
            "tournament_id": tournament.tournament_id,
            "game_id": active_game.game_id,
            "you_are": color,
            "opponent": {
                "bot_id": opponent.get("bot_id"),
                "name": opponent.get("bot_name"),
            },
            "state": state,
            "legal_moves": state["legal_moves"],
        }

    def submit_move(
        self,
        *,
        bot_id: str,
        secret: str,
        game_id: str,
        row: int,
        col: int,
    ) -> dict[str, object]:
        bot = self._authenticate_bot(bot_id, secret)
        self._consume_external_request_slot(bot)
        bot.touch()
        tournament = self._find_tournament_for_game(game_id)
        self._sync_tournament(tournament)
        if tournament.status != "running":
            raise RuntimeError("Tournament is not running.")
        if game_id not in tournament.active_game_ids:
            raise RuntimeError("That match is not currently active.")

        current_game = self._game_manager.get_game(game_id)
        if current_game.game.status != "active":
            raise RuntimeError("That match is not currently active.")

        current_spec = current_game.player_specs[current_game.game.current_player]
        if current_spec.get("bot_id") != bot_id:
            raise PermissionError("It is not this bot's turn.")

        managed = self._game_manager.play_external_move(game_id, row, col)
        self._sync_tournament(tournament)
        return {
            "limit_seconds": self.external_bot_request_interval_seconds,
            "game": managed.to_dict(),
            "tournament": self._serialize_tournament(tournament),
        }

    def _lookup_tournament(self, tournament_id: str) -> Tournament:
        if tournament_id not in self._tournaments:
            raise KeyError(f"Unknown tournament id: {tournament_id}")
        return self._tournaments[tournament_id]

    def _find_tournament_for_game(self, game_id: str) -> Tournament:
        for tournament in self._tournaments.values():
            if game_id in tournament.match_game_ids:
                return tournament
        raise KeyError(f"No tournament contains game id: {game_id}")

    def _authenticate_bot(self, bot_id: str, secret: str) -> RegisteredBot:
        bot = self._bots.get(bot_id)
        if bot is None:
            raise KeyError(f"Unknown bot id: {bot_id}")
        if bot.secret != secret:
            raise PermissionError("Invalid bot secret.")
        return bot

    def _consume_external_request_slot(self, bot: RegisteredBot) -> None:
        now = time.monotonic()
        limit_seconds = self.external_bot_request_interval_seconds
        if bot.last_request_monotonic is not None:
            elapsed = now - bot.last_request_monotonic
            if elapsed < limit_seconds:
                raise ExternalBotRateLimitError(limit_seconds - elapsed, limit_seconds=limit_seconds)
        bot.last_request_monotonic = now

    def _allocate_spawn_name(self, prefix: str, suggested_index: int | None) -> str:
        candidate = prefix.strip() if suggested_index is None else f"{prefix.strip()}-{suggested_index}"
        if not self._name_in_use(candidate):
            return candidate

        suffix = suggested_index or 1
        while True:
            suffix += 1
            candidate = f"{prefix.strip()}-{suffix}"
            if not self._name_in_use(candidate):
                return candidate

    def _name_in_use(self, name: str) -> bool:
        normalized = name.casefold()
        if any(existing.name.casefold() == normalized for existing in self._bots.values()):
            return True
        return normalized in self._pending_spawns

    def _wait_for_bot_registration(self, name: str, timeout_seconds: float) -> RegisteredBot:
        deadline = time.monotonic() + timeout_seconds
        normalized = name.casefold()
        while time.monotonic() < deadline:
            for bot in self._bots.values():
                if bot.name.casefold() == normalized:
                    return bot
            pending = self._pending_spawns.get(normalized)
            if pending is None:
                break
            if pending.process.poll() is not None:
                raise RuntimeError(f"Spawned bot '{name}' exited before registration completed.")
            time.sleep(0.05)
        raise RuntimeError(f"Timed out waiting for spawned bot '{name}' to register.")

    def _refresh_managed_processes(self) -> None:
        dead_bot_ids = [bot_id for bot_id, managed in self._managed_bot_processes.items() if managed.process.poll() is not None]
        for bot_id in dead_bot_ids:
            self._managed_bot_processes.pop(bot_id, None)
        dead_pending = [name for name, managed in self._pending_spawns.items() if managed.process.poll() is not None]
        for name in dead_pending:
            self._pending_spawns.pop(name, None)

    @staticmethod
    def _terminate_process(managed: ManagedSpawn) -> None:
        if managed.process.poll() is not None:
            return
        managed.process.terminate()
        try:
            managed.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            managed.process.kill()

    def _serialize_public_bot(self, bot: RegisteredBot) -> dict[str, object]:
        payload = bot.to_public_dict()
        managed = self._managed_bot_processes.get(bot.bot_id)
        payload["managed"] = managed is not None
        payload["process_alive"] = managed.process.poll() is None if managed is not None else False
        payload["pid"] = managed.process.pid if managed is not None else None
        return payload

    def _serialize_private_bot(self, bot: RegisteredBot) -> dict[str, object]:
        payload = self._serialize_public_bot(bot)
        payload["secret"] = bot.secret
        return payload

    def _running_tournament(self) -> Tournament | None:
        for tournament in sorted(self._tournaments.values(), key=lambda item: item.created_at, reverse=True):
            self._sync_tournament(tournament)
            if tournament.status == "running":
                return tournament
        return None

    @staticmethod
    def _build_pairings(bot_ids: list[str], *, tournament_format: str) -> list[tuple[str, str]]:
        if tournament_format == "double_round_robin":
            return [(black, white) for black in bot_ids for white in bot_ids if black != white]
        if tournament_format == "single_round_robin":
            return [(left, right) for left, right in combinations(bot_ids, 2)]
        raise ValueError(f"Unsupported tournament format: {tournament_format}")

    @staticmethod
    def _normalize_tournament_format(
        *,
        tournament_format: str | None,
        double_round_robin: bool | None,
    ) -> str:
        if tournament_format is not None:
            normalized = tournament_format.strip().casefold()
            if normalized not in SUPPORTED_TOURNAMENT_FORMATS:
                supported = ", ".join(SUPPORTED_TOURNAMENT_FORMATS)
                raise ValueError(f"Unsupported tournament format '{tournament_format}'. Supported formats: {supported}.")
            return normalized
        if double_round_robin is None:
            return "double_round_robin"
        return "double_round_robin" if double_round_robin else "single_round_robin"

    @staticmethod
    def _build_single_elimination_plan(bot_ids: list[str]) -> list[dict[str, object]]:
        entrants: list[dict[str, object]] = [{"type": "bot", "bot_id": bot_id} for bot_id in bot_ids]
        match_plan: list[dict[str, object]] = []
        round_index = 1
        next_match_index = 1
        while len(entrants) > 1:
            next_round: list[dict[str, object]] = []
            slot_index = 1
            for index in range(0, len(entrants), 2):
                left = entrants[index]
                right = entrants[index + 1] if index + 1 < len(entrants) else None
                if right is None:
                    next_round.append(left)
                    continue
                match_plan.append(
                    {
                        "match_index": next_match_index,
                        "round_index": round_index,
                        "slot_index": slot_index,
                        "black_source": left,
                        "white_source": right,
                        "game_id": None,
                        "winner_bot_id": None,
                    }
                )
                next_round.append({"type": "winner", "match_index": next_match_index})
                next_match_index += 1
                slot_index += 1
            entrants = next_round
            round_index += 1
        return match_plan

    def _create_tournament_game(
        self,
        tournament: Tournament,
        *,
        black_bot_id: str,
        white_bot_id: str,
        match_index: int,
        extra_metadata: dict[str, object] | None = None,
    ) -> ManagedGame:
        black_bot = self._bots[black_bot_id]
        white_bot = self._bots[white_bot_id]
        metadata = {
            "mode": "tournament",
            "tournament_id": tournament.tournament_id,
            "match_index": match_index,
            "black_bot_id": black_bot.bot_id,
            "white_bot_id": white_bot.bot_id,
            "black_bot_name": black_bot.name,
            "white_bot_name": white_bot.name,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        managed = self._game_manager.create_game(
            black={
                "type": "tournament_bot",
                "bot_id": black_bot.bot_id,
                "bot_name": black_bot.name,
            },
            white={
                "type": "tournament_bot",
                "bot_id": white_bot.bot_id,
                "bot_name": white_bot.name,
            },
            auto_advance=False,
            metadata=metadata,
        )
        tournament.match_game_ids.append(managed.game_id)
        return managed

    def _materialize_tournament_games(self, tournament: Tournament) -> None:
        if tournament.format != "single_elimination":
            return
        for plan_entry in tournament.match_plan:
            if plan_entry.get("winner_bot_id") is not None:
                continue
            game_id = plan_entry.get("game_id")
            if isinstance(game_id, str):
                managed = self._game_manager.get_game(game_id)
                if managed.game.status != "finished":
                    continue
                plan_entry["winner_bot_id"] = self._winner_bot_id_for_match(tournament, managed)
                continue

            black_bot_id = self._resolve_match_source(tournament, plan_entry["black_source"])
            white_bot_id = self._resolve_match_source(tournament, plan_entry["white_source"])
            if black_bot_id is None or white_bot_id is None:
                continue

            managed = self._create_tournament_game(
                tournament,
                black_bot_id=black_bot_id,
                white_bot_id=white_bot_id,
                match_index=int(plan_entry["match_index"]),
                extra_metadata={
                    "round_index": int(plan_entry["round_index"]),
                    "round_name": self._playoff_round_name(tournament, int(plan_entry["round_index"])),
                    "bracket_slot": int(plan_entry["slot_index"]),
                },
            )
            plan_entry["game_id"] = managed.game_id

    def _resolve_match_source(self, tournament: Tournament, source: object) -> str | None:
        if not isinstance(source, dict):
            return None
        source_type = str(source.get("type", ""))
        if source_type == "bot":
            bot_id = source.get("bot_id")
            return str(bot_id) if bot_id is not None else None
        if source_type == "winner":
            match_index = source.get("match_index")
            if match_index is None:
                return None
            plan_entry = self._plan_entry_for_match_index(tournament, int(match_index))
            winner_bot_id = plan_entry.get("winner_bot_id")
            return str(winner_bot_id) if winner_bot_id is not None else None
        return None

    @staticmethod
    def _plan_entry_for_match_index(tournament: Tournament, match_index: int) -> dict[str, object]:
        for plan_entry in tournament.match_plan:
            if int(plan_entry["match_index"]) == match_index:
                return plan_entry
        raise KeyError(f"Unknown tournament match index: {match_index}")

    @staticmethod
    def _winner_bot_id_for_match(tournament: Tournament, managed: ManagedGame) -> str:
        winner = managed.game.winner
        black_bot_id = str(managed.player_specs["B"].get("bot_id"))
        white_bot_id = str(managed.player_specs["W"].get("bot_id"))
        if winner == "B":
            return black_bot_id
        if winner == "W":
            return white_bot_id
        seeding_order = {bot_id: index for index, bot_id in enumerate(tournament.bot_ids)}
        return min((black_bot_id, white_bot_id), key=lambda bot_id: seeding_order.get(bot_id, sys.maxsize))

    @staticmethod
    def _playoff_round_name(tournament: Tournament, round_index: int) -> str:
        total_rounds = max((int(item["round_index"]) for item in tournament.match_plan), default=round_index)
        rounds_remaining = total_rounds - round_index
        if rounds_remaining == 0:
            return "Final"
        if rounds_remaining == 1:
            return "Semifinal"
        if rounds_remaining == 2:
            return "Quarterfinal"
        return f"Round {round_index}"

    def _sync_tournament(self, tournament: Tournament) -> None:
        if tournament.status != "running":
            return

        for game_id in list(tournament.active_game_ids):
            self._game_manager.check_turn_timeout(game_id)

        tournament.active_game_ids = [
            game_id
            for game_id in tournament.active_game_ids
            if self._game_manager.get_game(game_id).game.status == "active"
        ]
        self._materialize_tournament_games(tournament)
        self._schedule_games(tournament)

        tournament_finished = False
        if tournament.format == "single_elimination":
            tournament_finished = bool(tournament.match_plan) and all(
                plan_entry.get("winner_bot_id") is not None for plan_entry in tournament.match_plan
            )
        else:
            tournament_finished = all(
                self._game_manager.get_game(game_id).game.status == "finished" for game_id in tournament.match_game_ids
            )

        if tournament_finished:
            tournament.status = "finished"
            tournament.finished_at = datetime.now(UTC).isoformat()
            tournament.active_game_ids = []

    def _ongoing_managed_games(self, tournament: Tournament) -> list[ManagedGame]:
        self._sync_tournament(tournament)
        return [
            self._game_manager.get_game(game_id)
            for game_id in tournament.active_game_ids
            if self._game_manager.get_game(game_id).game.status == "active"
        ]

    def _active_game_for_bot(self, tournament: Tournament, bot_id: str) -> ManagedGame | None:
        for managed in self._ongoing_managed_games(tournament):
            if bot_id in self._bot_ids_for_game(managed):
                return managed
        return None

    def _schedule_games(self, tournament: Tournament) -> None:
        busy_bot_ids = {
            bot_id
            for game_id in tournament.active_game_ids
            for bot_id in self._bot_ids_for_game(self._game_manager.get_game(game_id))
        }
        active_lookup = set(tournament.active_game_ids)
        for game_id in tournament.match_game_ids:
            if game_id in active_lookup:
                continue
            managed = self._game_manager.get_game(game_id)
            if managed.game.status == "finished":
                continue
            bot_ids = self._bot_ids_for_game(managed)
            if busy_bot_ids.intersection(bot_ids):
                continue
            managed.restart_turn_timer()
            managed.touch()
            tournament.active_game_ids.append(game_id)
            active_lookup.add(game_id)
            busy_bot_ids.update(bot_ids)

    @staticmethod
    def _bot_ids_for_game(managed: ManagedGame) -> set[str]:
        return {
            str(spec["bot_id"])
            for spec in managed.player_specs.values()
            if spec.get("bot_id") is not None
        }

    def _serialize_bracket_source(self, tournament: Tournament, source: object) -> dict[str, object]:
        if not isinstance(source, dict):
            return {
                "bot_id": None,
                "name": None,
                "label": "TBD",
                "resolved": False,
                "source": None,
            }

        source_type = str(source.get("type", ""))
        if source_type == "bot":
            bot_id = source.get("bot_id")
            bot_id_text = str(bot_id) if bot_id is not None else None
            name = tournament.participants.get(bot_id_text, bot_id_text) if bot_id_text is not None else None
            return {
                "bot_id": bot_id_text,
                "name": name,
                "label": name or "TBD",
                "resolved": bot_id_text is not None,
                "source": {"type": "bot", "bot_id": bot_id_text},
            }

        if source_type == "winner":
            match_index = source.get("match_index")
            winner_bot_id = self._resolve_match_source(tournament, source)
            winner_name = tournament.participants.get(winner_bot_id) if winner_bot_id is not None else None
            match_index_value = int(match_index) if match_index is not None else None
            return {
                "bot_id": winner_bot_id,
                "name": winner_name,
                "label": winner_name or (
                    f"Winner of Match #{match_index_value}" if match_index_value is not None else "Winner of previous match"
                ),
                "resolved": winner_bot_id is not None,
                "source": {"type": "winner", "match_index": match_index_value},
            }

        return {
            "bot_id": None,
            "name": None,
            "label": "TBD",
            "resolved": False,
            "source": {"type": source_type or "unknown"},
        }

    def _serialize_single_elimination_bracket(
        self,
        tournament: Tournament,
        matches_by_index: dict[int, dict[str, object]],
    ) -> dict[str, object]:
        rounds: list[dict[str, object]] = []
        round_lookup: dict[int, dict[str, object]] = {}
        ordered_plan = sorted(
            tournament.match_plan,
            key=lambda item: (int(item["round_index"]), int(item["slot_index"]), int(item["match_index"])),
        )

        for plan_entry in ordered_plan:
            round_index = int(plan_entry["round_index"])
            round_payload = round_lookup.get(round_index)
            if round_payload is None:
                round_payload = {
                    "round_index": round_index,
                    "round_name": self._playoff_round_name(tournament, round_index),
                    "matches": [],
                }
                rounds.append(round_payload)
                round_lookup[round_index] = round_payload

            match_index = int(plan_entry["match_index"])
            match_payload = matches_by_index.get(match_index)
            winner_bot_id = plan_entry.get("winner_bot_id")
            winner_bot_id_text = str(winner_bot_id) if winner_bot_id is not None else None
            winner_name = tournament.participants.get(winner_bot_id_text) if winner_bot_id_text is not None else None
            round_payload["matches"].append(
                {
                    "match_index": match_index,
                    "slot_index": int(plan_entry["slot_index"]),
                    "game_id": plan_entry.get("game_id"),
                    "status": (
                        str(match_payload["status"])
                        if match_payload is not None
                        else ("finished" if winner_bot_id_text is not None else "pending")
                    ),
                    "active": bool(match_payload and match_payload.get("active")),
                    "winner": match_payload["winner"] if match_payload is not None else None,
                    "scores": match_payload["scores"] if match_payload is not None else None,
                    "winner_bot_id": winner_bot_id_text,
                    "winner_name": winner_name,
                    "black": self._serialize_bracket_source(tournament, plan_entry["black_source"]),
                    "white": self._serialize_bracket_source(tournament, plan_entry["white_source"]),
                }
            )

        champion: dict[str, object] | None = None
        if ordered_plan:
            final_entry = max(
                ordered_plan,
                key=lambda item: (int(item["round_index"]), int(item["slot_index"]), int(item["match_index"])),
            )
            winner_bot_id = final_entry.get("winner_bot_id")
            if winner_bot_id is not None:
                champion = {
                    "bot_id": str(winner_bot_id),
                    "name": tournament.participants.get(str(winner_bot_id), str(winner_bot_id)),
                    "match_index": int(final_entry["match_index"]),
                    "round_index": int(final_entry["round_index"]),
                    "round_name": self._playoff_round_name(tournament, int(final_entry["round_index"])),
                }

        return {
            "rounds": rounds,
            "winner": champion,
        }

    def _serialize_tournament(self, tournament: Tournament) -> dict[str, object]:
        matches: list[dict[str, object]] = []
        managed_matches = sorted(
            (self._game_manager.get_game(game_id) for game_id in tournament.match_game_ids),
            key=lambda managed: int(managed.metadata.get("match_index", 0)),
        )
        matches_by_index: dict[int, dict[str, object]] = {}
        active_game_ids = {
            game_id
            for game_id in tournament.active_game_ids
            if self._game_manager.get_game(game_id).game.status == "active"
        }
        standings = {
            bot_id: {
                "bot_id": bot_id,
                "name": tournament.participants[bot_id],
                "played": 0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "points": 0.0,
                "pieces_for": 0,
                "pieces_against": 0,
            }
            for bot_id in tournament.bot_ids
        }

        for managed in managed_matches:
            game_id = managed.game_id
            game_payload = managed.to_dict()
            black_bot_id = str(managed.player_specs["B"].get("bot_id"))
            white_bot_id = str(managed.player_specs["W"].get("bot_id"))
            matches.append(
                {
                    "game_id": game_id,
                    "match_index": int(game_payload["metadata"].get("match_index", len(matches) + 1)),
                    "round_index": game_payload["metadata"].get("round_index"),
                    "round_name": game_payload["metadata"].get("round_name"),
                    "status": game_payload["status"],
                    "active": game_id in active_game_ids,
                    "winner": game_payload["winner"],
                    "scores": game_payload["scores"],
                    "board": game_payload["board"],
                    "current_player": game_payload["current_player"],
                    "legal_moves": game_payload["legal_moves"],
                    "black": {"bot_id": black_bot_id, "name": tournament.participants[black_bot_id]},
                    "white": {"bot_id": white_bot_id, "name": tournament.participants[white_bot_id]},
                }
            )
            matches_by_index[matches[-1]["match_index"]] = matches[-1]
            if game_payload["status"] != "finished":
                continue

            black_score = int(game_payload["scores"]["black"])
            white_score = int(game_payload["scores"]["white"])
            standings[black_bot_id]["played"] += 1
            standings[white_bot_id]["played"] += 1
            standings[black_bot_id]["pieces_for"] += black_score
            standings[black_bot_id]["pieces_against"] += white_score
            standings[white_bot_id]["pieces_for"] += white_score
            standings[white_bot_id]["pieces_against"] += black_score

            if game_payload["winner"] == "B":
                standings[black_bot_id]["wins"] += 1
                standings[white_bot_id]["losses"] += 1
                standings[black_bot_id]["points"] += 1.0
            elif game_payload["winner"] == "W":
                standings[white_bot_id]["wins"] += 1
                standings[black_bot_id]["losses"] += 1
                standings[white_bot_id]["points"] += 1.0
            else:
                standings[black_bot_id]["draws"] += 1
                standings[white_bot_id]["draws"] += 1
                standings[black_bot_id]["points"] += 0.5
                standings[white_bot_id]["points"] += 0.5

        ordered_standings = sorted(
            standings.values(),
            key=lambda item: (-item["points"], -(item["pieces_for"] - item["pieces_against"]), -item["pieces_for"], item["name"]),
        )
        active_match = next((match for match in matches if match["active"]), None)
        ongoing_matches = [match for match in matches if match["active"]]
        match_count = len(tournament.bot_ids) - 1 if tournament.format == "single_elimination" else len(matches)
        bracket = (
            self._serialize_single_elimination_bracket(tournament, matches_by_index)
            if tournament.format == "single_elimination"
            else None
        )
        return {
            "tournament_id": tournament.tournament_id,
            "status": tournament.status,
            "format": tournament.format,
            "created_at": tournament.created_at,
            "started_at": tournament.started_at,
            "finished_at": tournament.finished_at,
            "bot_count": len(tournament.bot_ids),
            "bots": [
                self._bots[bot_id].to_public_dict()
                if bot_id in self._bots
                else {
                    "bot_id": bot_id,
                    "name": tournament.participants[bot_id],
                    "created_at": None,
                    "last_seen_at": None,
                    "status": "removed",
                }
                for bot_id in tournament.bot_ids
            ],
            "match_count": match_count,
            "completed_match_count": sum(1 for match in matches if match["status"] == "finished"),
            "active_match": active_match,
            "ongoing_match_count": len(ongoing_matches),
            "ongoing_matches": ongoing_matches,
            "matches": matches,
            "bracket": bracket,
            "champion": None if bracket is None else bracket["winner"],
            "standings": ordered_standings,
        }


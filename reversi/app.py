from __future__ import annotations

from math import ceil
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, render_template, request

from .game import InvalidMoveError
from .manager import BOT_TURN_TIMEOUT_SECONDS, GameManager
from .tournament import (
    EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS,
    SUPPORTED_TOURNAMENT_FORMATS,
    ExternalBotRateLimitError,
    TournamentManager,
)

BASE_DIR = Path(__file__).resolve().parent
RECOMMENDED_ASSIGNMENT_POLL_INTERVAL_SECONDS = EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS
EXTERNALLY_ACCESSIBLE_ENDPOINTS = {
    "get_tournament_assignment",
    "register_tournament_bot",
    "submit_tournament_move",
    "swagger_ui",
    "openapi_spec",
    "bot_instructions",
}
OPERATOR_PASSWORD_REQUIRED_ERROR = "Operator access requires the configured server password."
OPERATOR_AUTH_REALM = "Reversi Operator"
OPERATOR_USERNAME = "operator"
OPERATOR_PASSWORD_HEADER = "X-Reversi-Operator-Password"


def _local_loopback_server_url(scheme: str, host_header: str) -> str:
    parsed = urlsplit(f"{scheme}://{host_header}")
    port = parsed.port or (443 if scheme == "https" else 80)
    return f"{scheme}://127.0.0.1:{port}"


def _request_has_operator_password(expected_password: str) -> bool:
    header_password = request.headers.get(OPERATOR_PASSWORD_HEADER)
    if header_password is not None:
        return header_password == expected_password

    auth = request.authorization
    if auth is None:
        return False

    return auth.username == OPERATOR_USERNAME and auth.password == expected_password


def _operator_auth_error_response() -> tuple[Response, int] | tuple[dict[str, str], int] | tuple[str, int]:
    if request.path.startswith("/api/"):
        return {"error": OPERATOR_PASSWORD_REQUIRED_ERROR}, 401

    response = Response(OPERATOR_PASSWORD_REQUIRED_ERROR, 401)
    response.headers["WWW-Authenticate"] = f'Basic realm="{OPERATOR_AUTH_REALM}", charset="UTF-8"'
    return response, 401


def _rate_limit_error_response(exc: ExternalBotRateLimitError) -> tuple[dict[str, object], int, dict[str, str]]:
    retry_after_seconds = round(exc.retry_after_seconds, 3)
    return (
        {
            "error": str(exc),
            "retry_after_seconds": retry_after_seconds,
            "limit_seconds": exc.limit_seconds,
        },
        429,
        {
            "Retry-After": str(max(1, ceil(exc.retry_after_seconds))),
            "Cache-Control": "no-store",
        },
    )



def _external_bot_openapi_spec(server_url: str, *, request_interval_seconds: float) -> dict[str, object]:
    move_example = {"row": 2, "col": 3}
    game_state_example = {
        "game_id": "abc12345",
        "board": [
            [".", ".", ".", ".", ".", ".", ".", "."],
            [".", ".", ".", ".", ".", ".", ".", "."],
            [".", ".", ".", ".", ".", ".", ".", "."],
            [".", ".", ".", "W", "B", ".", ".", "."],
            [".", ".", ".", "B", "W", ".", ".", "."],
            [".", ".", ".", ".", ".", ".", ".", "."],
            [".", ".", ".", ".", ".", ".", ".", "."],
            [".", ".", ".", ".", ".", ".", ".", "."],
        ],
        "current_player": "B",
        "status": "active",
        "winner": None,
        "termination_reason": None,
        "scores": {"black": 2, "white": 2},
        "legal_moves": [move_example, {"row": 3, "col": 2}, {"row": 4, "col": 5}, {"row": 5, "col": 4}],
        "move_count": 0,
        "history": [],
        "created_at": "2026-09-25T12:00:00+00:00",
        "updated_at": "2026-09-25T12:00:00+00:00",
        "players": {
            "black": {"type": "tournament_bot", "bot_id": "2bca0f14", "bot_name": "alpha"},
            "white": {"type": "tournament_bot", "bot_id": "7f31c8aa", "bot_name": "beta"},
        },
        "metadata": {
            "mode": "tournament",
            "tournament_id": "tour1234",
            "match_index": 1,
            "black_bot_id": "2bca0f14",
            "white_bot_id": "7f31c8aa",
            "black_bot_name": "alpha",
            "white_bot_name": "beta",
        },
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Reversi Workshop Bot API",
            "version": "1.0.0",
            "description": (
                "External tournament bots register once, keep the returned bot_id/secret, "
                f"must currently wait at least {request_interval_seconds:.2f} seconds between authenticated bot requests, "
                "should expect the operator to change that limit even while a tournament is running, "
                "and should obey both the latest limit_seconds values and every Retry-After response. "
                f"typically poll for assignments about every {request_interval_seconds:.2f} seconds, "
                f"and must submit their move before the {int(BOT_TURN_TIMEOUT_SECONDS)} second turn timeout or the game is forfeited."
            ),
        },
        "servers": [{"url": server_url, "description": "Current Reversi server"}],
        "tags": [
            {
                "name": "Bots",
                "description": "Registration and turn polling endpoints intended for external workshop bots.",
            },
            {
                "name": "Matches",
                "description": "Authenticated move submission for active tournament matches.",
            },
        ],
        "paths": {
            "/api/tournament/bots/register": {
                "post": {
                    "tags": ["Bots"],
                    "summary": "Register a tournament bot",
                    "description": (
                        "Register the bot before the operator starts the tournament. "
                        "Bot names must be unique. Store the returned bot_id and secret; both are required later."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RegisterBotRequest"},
                                "examples": {
                                    "basic": {
                                        "summary": "Simple registration",
                                        "value": {"name": "alpha"},
                                    }
                                },
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Bot registered successfully.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PrivateBot"}
                                }
                            },
                        },
                        "400": {
                            "description": "Invalid registration request, such as a missing or duplicate name.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                }
                            },
                        },
                    },
                }
            },
            "/api/tournament/bots/{bot_id}/assignment": {
                "get": {
                    "tags": ["Bots"],
                    "summary": "Poll for the bot's next assignment",
                    "description": (
                        "Call this endpoint repeatedly after registration. When available is false, keep waiting and poll again. "
                        f"Bots may currently send at most one authenticated request every {request_interval_seconds:.2f} seconds across both assignment polling and move submission. "
                        "That value can change during a tournament, so bots should update their pacing from successful responses and always honor 429 Retry-After delays. "
                        f"A poll interval around {request_interval_seconds:.2f} seconds keeps games responsive without hammering the server."
                    ),
                    "parameters": [
                        {
                            "name": "bot_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "The bot_id returned from registration.",
                        },
                        {
                            "name": "secret",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "The secret returned from registration.",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Current assignment state for the bot.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/AssignmentResponse"},
                                    "examples": {
                                        "waiting": {
                                            "summary": "Bot is idle",
                                            "value": {
                                                "bot": {
                                                    "bot_id": "2bca0f14",
                                                    "name": "alpha",
                                                    "created_at": "2026-09-25T12:00:00+00:00",
                                                    "last_seen_at": "2026-09-25T12:00:05+00:00",
                                                    "status": "connected",
                                                },
                                                "available": False,
                                                "limit_seconds": request_interval_seconds,
                                                "message": "Waiting for another bot's turn.",
                                                "active_game_id": "abc12345",
                                            },
                                        },
                                        "ready": {
                                            "summary": "Bot must submit a move",
                                            "value": {
                                                "bot": {
                                                    "bot_id": "2bca0f14",
                                                    "name": "alpha",
                                                    "created_at": "2026-09-25T12:00:00+00:00",
                                                    "last_seen_at": "2026-09-25T12:00:05+00:00",
                                                    "status": "connected",
                                                },
                                                "available": True,
                                                "limit_seconds": request_interval_seconds,
                                                "tournament_id": "tour1234",
                                                "game_id": "abc12345",
                                                "you_are": "B",
                                                "opponent": {"bot_id": "7f31c8aa", "name": "beta"},
                                                "state": game_state_example,
                                                "legal_moves": [move_example],
                                            },
                                        },
                                    },
                                }
                            },
                        },
                        "403": {
                            "description": "The secret does not match the bot_id.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                }
                            },
                        },
                        "404": {
                            "description": "Unknown bot_id.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                }
                            },
                        },
                        "429": {
                            "description": "The bot sent another authenticated request too soon. Wait for retry_after_seconds before polling or submitting a move again.",
                            "headers": {
                                "Retry-After": {
                                    "description": "Whole-second retry delay required by the HTTP standard, rounded up from the precise sub-second wait.",
                                    "schema": {"type": "integer", "minimum": 1, "example": 1},
                                }
                            },
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/RateLimitErrorResponse"}
                                }
                            },
                        },
                    },
                }
            },
            "/api/tournament/matches/{game_id}/move": {
                "post": {
                    "tags": ["Matches"],
                    "summary": "Submit one tournament move",
                    "description": (
                        "Submit exactly one legal move when assignment polling says available=true. "
                        f"Bots may currently send at most one authenticated request every {request_interval_seconds:.2f} seconds across both assignment polling and move submission, so a bot may need to wait briefly after a successful poll before posting its move. "
                        "The operator may change this limit during a tournament, so production bots should monitor successful response metadata and 429 Retry-After headers. "
                        f"If the active turn exceeds {int(BOT_TURN_TIMEOUT_SECONDS)} seconds, the current bot forfeits automatically."
                    ),
                    "parameters": [
                        {
                            "name": "game_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "The active game_id returned by the assignment response.",
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/MoveSubmissionRequest"},
                                "examples": {
                                    "legalMove": {
                                        "summary": "Submit a legal move",
                                        "value": {
                                            "bot_id": "2bca0f14",
                                            "secret": "generated-secret",
                                            "row": 2,
                                            "col": 3,
                                        },
                                    }
                                },
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Move accepted. The response includes the updated game and tournament snapshot.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/MoveSubmissionResponse"}
                                }
                            },
                        },
                        "400": {
                            "description": "Malformed payload, illegal move, or match is not currently playable.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                }
                            },
                        },
                        "403": {
                            "description": "Wrong secret or the move came from the wrong bot for the current turn.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                }
                            },
                        },
                        "404": {
                            "description": "Unknown game_id or bot_id.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                }
                            },
                        },
                        "429": {
                            "description": "The bot sent another authenticated request too soon. Wait for retry_after_seconds before polling or submitting another move request.",
                            "headers": {
                                "Retry-After": {
                                    "description": "Whole-second retry delay required by the HTTP standard, rounded up from the precise sub-second wait.",
                                    "schema": {"type": "integer", "minimum": 1, "example": 1},
                                }
                            },
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/RateLimitErrorResponse"}
                                }
                            },
                        },
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "RegisterBotRequest": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Unique display name for the bot.",
                            "example": "alpha",
                        },
                        "reservation_token": {
                            "type": "string",
                            "nullable": True,
                            "description": "Optional token used by the operator-controlled spawn flow.",
                        },
                    },
                },
                "PublicBot": {
                    "type": "object",
                    "required": ["bot_id", "name", "created_at", "last_seen_at", "status"],
                    "properties": {
                        "bot_id": {"type": "string", "example": "2bca0f14"},
                        "name": {"type": "string", "example": "alpha"},
                        "created_at": {"type": "string", "format": "date-time"},
                        "last_seen_at": {"type": "string", "format": "date-time"},
                        "status": {"type": "string", "example": "connected"},
                    },
                },
                "PrivateBot": {
                    "allOf": [
                        {"$ref": "#/components/schemas/PublicBot"},
                        {
                            "type": "object",
                            "required": ["secret", "managed", "process_alive", "pid"],
                            "properties": {
                                "secret": {"type": "string", "example": "generated-secret"},
                                "managed": {"type": "boolean", "example": False},
                                "process_alive": {"type": "boolean", "example": False},
                                "pid": {"type": "integer", "nullable": True, "example": None},
                            },
                        },
                    ]
                },
                "Move": {
                    "type": "object",
                    "required": ["row", "col"],
                    "properties": {
                        "row": {"type": "integer", "minimum": 0, "maximum": 7, "example": 2},
                        "col": {"type": "integer", "minimum": 0, "maximum": 7, "example": 3},
                    },
                },
                "Scores": {
                    "type": "object",
                    "required": ["black", "white"],
                    "properties": {
                        "black": {"type": "integer", "example": 2},
                        "white": {"type": "integer", "example": 2},
                    },
                },
                "PlayerSpec": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "example": "tournament_bot"},
                        "url": {"type": "string", "format": "uri", "nullable": True},
                        "bot_id": {"type": "string", "nullable": True},
                        "bot_name": {"type": "string", "nullable": True},
                    },
                    "additionalProperties": True,
                },
                "GameState": {
                    "type": "object",
                    "required": [
                        "game_id",
                        "board",
                        "current_player",
                        "status",
                        "scores",
                        "legal_moves",
                        "move_count",
                        "history",
                        "created_at",
                        "updated_at",
                        "players",
                        "metadata"
                    ],
                    "properties": {
                        "game_id": {"type": "string", "example": "abc12345"},
                        "board": {
                            "type": "array",
                            "description": "8x8 board using '.', 'B', and 'W'.",
                            "items": {
                                "type": "array",
                                "items": {"type": "string", "enum": [".", "B", "W"]},
                            },
                        },
                        "current_player": {"type": "string", "enum": ["B", "W"], "example": "B"},
                        "status": {"type": "string", "enum": ["active", "finished"], "example": "active"},
                        "winner": {
                            "type": "string",
                            "nullable": True,
                            "description": "Winner is 'B', 'W', 'draw', or null while active.",
                            "example": None,
                        },
                        "termination_reason": {
                            "type": "string",
                            "nullable": True,
                            "example": None,
                            "description": "May become values like no_legal_moves or bot_timeout_exceeded.",
                        },
                        "scores": {"$ref": "#/components/schemas/Scores"},
                        "legal_moves": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Move"},
                        },
                        "move_count": {"type": "integer", "example": 0},
                        "history": {
                            "type": "array",
                            "description": "Move/pass/forfeit records in play order.",
                            "items": {"type": "object", "additionalProperties": True},
                        },
                        "created_at": {"type": "string", "format": "date-time"},
                        "updated_at": {"type": "string", "format": "date-time"},
                        "players": {
                            "type": "object",
                            "required": ["black", "white"],
                            "properties": {
                                "black": {"$ref": "#/components/schemas/PlayerSpec"},
                                "white": {"$ref": "#/components/schemas/PlayerSpec"},
                            },
                        },
                        "metadata": {
                            "type": "object",
                            "additionalProperties": True,
                            "description": "Tournament metadata such as tournament_id, match_index, and bot ids.",
                        },
                    },
                    "example": game_state_example,
                },
                "AssignmentResponse": {
                    "type": "object",
                    "required": ["bot", "available", "limit_seconds"],
                    "properties": {
                        "bot": {"$ref": "#/components/schemas/PublicBot"},
                        "available": {"type": "boolean"},
                        "limit_seconds": {
                            "type": "number",
                            "format": "float",
                            "minimum": 0,
                            "example": request_interval_seconds,
                            "description": "Current minimum spacing between authenticated requests for this bot.",
                        },
                        "message": {"type": "string", "nullable": True},
                        "tournament_id": {"type": "string", "nullable": True},
                        "game_id": {"type": "string", "nullable": True},
                        "active_game_id": {"type": "string", "nullable": True},
                        "active_game_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "you_are": {"type": "string", "nullable": True, "enum": ["B", "W"]},
                        "opponent": {
                            "type": "object",
                            "nullable": True,
                            "properties": {
                                "bot_id": {"type": "string", "nullable": True},
                                "name": {"type": "string", "nullable": True},
                            },
                        },
                        "state": {
                            "allOf": [{"$ref": "#/components/schemas/GameState"}],
                            "nullable": True,
                        },
                        "legal_moves": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Move"},
                        },
                        "tournament": {
                            "$ref": "#/components/schemas/TournamentSummary"
                        },
                    },
                },
                "MoveSubmissionRequest": {
                    "type": "object",
                    "required": ["bot_id", "secret", "row", "col"],
                    "properties": {
                        "bot_id": {"type": "string", "example": "2bca0f14"},
                        "secret": {"type": "string", "example": "generated-secret"},
                        "row": {"type": "integer", "minimum": 0, "maximum": 7, "example": 2},
                        "col": {"type": "integer", "minimum": 0, "maximum": 7, "example": 3},
                    },
                },
                "TournamentSummary": {
                    "type": "object",
                    "description": "Current tournament snapshot returned together with assignments and move submissions.",
                    "properties": {
                        "tournament_id": {"type": "string"},
                        "status": {"type": "string", "example": "running"},
                        "format": {
                            "type": "string",
                            "enum": list(SUPPORTED_TOURNAMENT_FORMATS),
                            "example": SUPPORTED_TOURNAMENT_FORMATS[0],
                        },
                        "bot_count": {"type": "integer", "example": 2},
                        "match_count": {"type": "integer", "example": 1},
                        "completed_match_count": {"type": "integer", "example": 0},
                        "ongoing_match_count": {"type": "integer", "example": 1},
                        "active_match": {
                            "type": "object",
                            "nullable": True,
                            "additionalProperties": True,
                        },
                        "ongoing_matches": {
                            "type": "array",
                            "items": {"type": "object", "additionalProperties": True},
                        },
                        "matches": {
                            "type": "array",
                            "items": {"type": "object", "additionalProperties": True},
                        },
                        "standings": {
                            "type": "array",
                            "items": {"type": "object", "additionalProperties": True},
                        },
                        "bracket": {
                            "type": "object",
                            "nullable": True,
                            "additionalProperties": True,
                        },
                        "champion": {
                            "type": "object",
                            "nullable": True,
                            "additionalProperties": True,
                        },
                    },
                    "additionalProperties": True,
                },
                "MoveSubmissionResponse": {
                    "type": "object",
                    "required": ["limit_seconds", "game", "tournament"],
                    "properties": {
                        "limit_seconds": {
                            "type": "number",
                            "format": "float",
                            "minimum": 0,
                            "example": request_interval_seconds,
                            "description": "Current minimum spacing between authenticated requests for this bot.",
                        },
                        "game": {"$ref": "#/components/schemas/GameState"},
                        "tournament": {"$ref": "#/components/schemas/TournamentSummary"},
                    },
                },
                "ErrorResponse": {
                    "type": "object",
                    "required": ["error"],
                    "properties": {
                        "error": {"type": "string"}
                    },
                },
                "RateLimitErrorResponse": {
                    "type": "object",
                    "required": ["error", "retry_after_seconds", "limit_seconds"],
                    "properties": {
                        "error": {"type": "string"},
                        "retry_after_seconds": {
                            "type": "number",
                            "format": "float",
                            "minimum": 0,
                            "example": 0.25,
                            "description": "Precise delay before this bot may send its next authenticated request.",
                        },
                        "limit_seconds": {
                            "type": "number",
                            "format": "float",
                            "minimum": 0,
                            "example": request_interval_seconds,
                            "description": "Minimum spacing enforced between authenticated bot requests.",
                        },
                    },
                },
            }
        },
    }


def create_app(*, operator_password: str | None = None) -> Flask:
    if not operator_password:
        raise ValueError("operator_password is required for protected operator routes.")

    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "web" / "templates"),
        static_folder=str(BASE_DIR / "web" / "static"),
        static_url_path="/static",
    )
    manager = GameManager()
    tournament_manager = TournamentManager(manager)
    app.config["game_manager"] = manager
    app.config["tournament_manager"] = tournament_manager
    app.config["operator_password"] = operator_password

    @app.before_request
    def restrict_operator_routes() -> tuple[Response, int] | tuple[dict[str, str], int] | tuple[str, int] | None:
        if request.endpoint in EXTERNALLY_ACCESSIBLE_ENDPOINTS:
            return None

        if _request_has_operator_password(app.config["operator_password"]):
            return None
        return _operator_auth_error_response()


    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            external_bot_request_interval_ms=tournament_manager.external_bot_request_interval_milliseconds,
        )

    @app.get("/swagger")
    @app.get("/docs")
    def swagger_ui() -> str:
        return render_template(
            "swagger.html",
            bot_turn_timeout_seconds=int(BOT_TURN_TIMEOUT_SECONDS),
            recommended_poll_interval_seconds=tournament_manager.external_bot_request_interval_seconds,
            external_bot_request_interval_seconds=tournament_manager.external_bot_request_interval_seconds,
        )

    @app.get("/openapi.json")
    def openapi_spec() -> tuple[dict[str, object], int]:
        return (
            _external_bot_openapi_spec(
                request.host_url.rstrip("/"),
                request_interval_seconds=tournament_manager.external_bot_request_interval_seconds,
            ),
            200,
        )

    @app.get("/instructions")
    def bot_instructions() -> str:
        return render_template(
            "bot_instructions.html",
            server_base_url=request.host_url.rstrip("/"),
            bot_turn_timeout_seconds=int(BOT_TURN_TIMEOUT_SECONDS),
            recommended_poll_interval_seconds=tournament_manager.external_bot_request_interval_seconds,
            external_bot_request_interval_seconds=tournament_manager.external_bot_request_interval_seconds,
            supported_formats=SUPPORTED_TOURNAMENT_FORMATS,
        )

    @app.get("/health")
    def health() -> tuple[dict[str, str], int]:
        return {"status": "ok"}, 200

    @app.get("/api/players/catalog")
    def player_catalog() -> tuple[dict[str, object], int]:
        return (
            {
                "player_types": [
                    {
                        "type": "human",
                        "label": "Human in browser",
                        "description": "Play locally by clicking the board in the UI.",
                    },
                    {
                        "type": "random",
                        "label": "Random bot",
                        "description": "Sample bot that chooses any legal move.",
                    },
                    {
                        "type": "greedy",
                        "label": "Greedy bot",
                        "description": "Sample bot that prefers corners, edges, then high-flip moves.",
                    },
                    {
                        "type": "remote",
                        "label": "Remote HTTP bot",
                        "description": "Server calls POST <url>/move with the game state.",
                    },
                    {
                        "type": "tournament_bot",
                        "label": "Tournament bot",
                        "description": "Connected workshop bot that polls for assignments and submits moves with its secret.",
                    },
                ],
                "remote_contract": {
                    "request": {
                        "game_id": "abc12345",
                        "you_are": "B",
                        "state": "See GET /api/games/<id> response",
                        "legal_moves": [{"row": 2, "col": 3}],
                    },
                    "response_examples": [
                        {"row": 2, "col": 3},
                        {"move": {"row": 2, "col": 3}},
                        {"pass": True},
                    ],
                },
                "tournament_contract": {
                    "formats": [
                        {
                            "type": "double_round_robin",
                            "label": "Double round robin",
                            "description": "Every bot plays both colors against every other bot.",
                        },
                        {
                            "type": "single_round_robin",
                            "label": "Single round robin",
                            "description": "Every bot plays one match against every other bot.",
                        },
                        {
                            "type": "single_elimination",
                            "label": "Single elimination playoff",
                            "description": "Lose once and you are out. Byes are assigned automatically when needed.",
                        },
                    ],
                    "register": {
                        "method": "POST",
                        "path": "/api/tournament/bots/register",
                        "body": {"name": "my-bot"},
                    },
                    "spawn": {
                        "method": "POST",
                        "path": "/api/tournament/spawn-bots",
                        "body": {
                            "name_prefix": "local-bot",
                            "count": 1,
                            "poll_interval": tournament_manager.external_bot_request_interval_seconds,
                        },
                    },
                    "settings": {
                        "method": "PUT",
                        "path": "/api/tournament/settings",
                        "body": {
                            "external_bot_request_interval_ms": tournament_manager.external_bot_request_interval_milliseconds
                        },
                    },
                    "remove": {
                        "method": "DELETE",
                        "path": "/api/tournament/bots/<bot_id>",
                    },
                    "poll_assignment": {
                        "method": "GET",
                        "path": "/api/tournament/bots/<bot_id>/assignment?secret=<secret>",
                    },
                    "submit_move": {
                        "method": "POST",
                        "path": "/api/tournament/matches/<game_id>/move",
                        "body": {"bot_id": "abc12345", "secret": "...", "row": 2, "col": 3},
                    },
                },
            },
            200,
        )

    @app.get("/api/tournament/bots")
    def list_tournament_bots() -> tuple[dict[str, object], int]:
        return {"bots": tournament_manager.list_bots()}, 200

    @app.get("/api/tournament/settings")
    def get_tournament_settings() -> tuple[dict[str, object], int]:
        return tournament_manager.get_external_bot_settings(), 200

    @app.put("/api/tournament/settings")
    def update_tournament_settings() -> tuple[dict[str, object], int]:
        payload = request.get_json(silent=True) or {}
        if "external_bot_request_interval_ms" not in payload:
            return {"error": "Expected JSON body with external_bot_request_interval_ms."}, 400
        try:
            settings = tournament_manager.set_external_bot_request_interval_milliseconds(
                float(payload["external_bot_request_interval_ms"])
            )
        except (TypeError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "external_bot_request_interval_ms must be numeric."
            return {"error": message}, 400
        return settings, 200

    @app.delete("/api/tournament/bots/<bot_id>")
    def remove_tournament_bot(bot_id: str) -> tuple[dict[str, object], int]:
        try:
            tournament_manager.remove_bot(bot_id)
        except KeyError as exc:
            return {"error": str(exc)}, 404
        except RuntimeError as exc:
            return {"error": str(exc)}, 409
        return {"removed": bot_id}, 200

    @app.post("/api/tournament/spawn-bots")
    def spawn_tournament_bots() -> tuple[dict[str, object], int]:
        payload = request.get_json(silent=True) or {}
        try:
            result = tournament_manager.spawn_bots(
                name_prefix=str(payload.get("name_prefix", "local-bot")),
                count=int(payload.get("count", 1)),
                server_url=_local_loopback_server_url(request.scheme, request.host),
                poll_interval=float(
                    payload.get("poll_interval", tournament_manager.external_bot_request_interval_seconds)
                ),
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except RuntimeError as exc:
            return {"error": str(exc)}, 502
        return result, 201

    @app.post("/api/tournament/bots/register")
    def register_tournament_bot() -> tuple[dict[str, object], int]:
        payload = request.get_json(silent=True) or {}
        try:
            registered = tournament_manager.register_bot(
                str(payload.get("name", "")),
                reservation_token=(
                    str(payload["reservation_token"]) if payload.get("reservation_token") is not None else None
                ),
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return registered, 201

    @app.get("/api/tournament/bots/<bot_id>/assignment")
    def get_tournament_assignment(bot_id: str) -> tuple[dict[str, object], int]:
        secret = request.args.get("secret", "")
        try:
            assignment = tournament_manager.get_assignment(bot_id=bot_id, secret=secret)
        except KeyError as exc:
            return {"error": str(exc)}, 404
        except PermissionError as exc:
            return {"error": str(exc)}, 403
        except ExternalBotRateLimitError as exc:
            return _rate_limit_error_response(exc)
        return assignment, 200

    @app.get("/api/tournaments")
    def list_tournaments() -> tuple[dict[str, object], int]:
        return {"tournaments": tournament_manager.list_tournaments()}, 200

    @app.post("/api/tournaments")
    def start_tournament() -> tuple[dict[str, object], int]:
        payload = request.get_json(silent=True) or {}
        bot_ids = payload.get("bot_ids")
        if bot_ids is not None and not isinstance(bot_ids, list):
            return {"error": "bot_ids must be an array when provided."}, 400
        tournament_format = payload.get("tournament_format")
        if tournament_format is not None and not isinstance(tournament_format, str):
            return {"error": "tournament_format must be a string when provided."}, 400
        try:
            tournament = tournament_manager.start_tournament(
                bot_ids=bot_ids,
                tournament_format=tournament_format,
                double_round_robin=(
                    bool(payload.get("double_round_robin")) if "double_round_robin" in payload else None
                ),
                pause_between_rounds=bool(payload.get("pause_between_rounds", False)),
            )
        except KeyError as exc:
            return {"error": str(exc)}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except RuntimeError as exc:
            return {"error": str(exc)}, 409
        return {**tournament, "supported_formats": list(SUPPORTED_TOURNAMENT_FORMATS)}, 201

    @app.get("/api/tournaments/<tournament_id>")
    def get_tournament(tournament_id: str) -> tuple[dict[str, object], int]:
        try:
            tournament = tournament_manager.get_tournament(tournament_id)
        except KeyError as exc:
            return {"error": str(exc)}, 404
        return {**tournament, "supported_formats": list(SUPPORTED_TOURNAMENT_FORMATS)}, 200

    @app.post("/api/tournaments/<tournament_id>/resume")
    def resume_tournament(tournament_id: str) -> tuple[dict[str, object], int]:
        try:
            tournament = tournament_manager.resume_tournament(tournament_id)
        except KeyError as exc:
            return {"error": str(exc)}, 404
        except RuntimeError as exc:
            return {"error": str(exc)}, 409
        return {**tournament, "supported_formats": list(SUPPORTED_TOURNAMENT_FORMATS)}, 200

    @app.post("/api/tournament/matches/<game_id>/move")
    def submit_tournament_move(game_id: str) -> tuple[dict[str, object], int]:
        payload = request.get_json(silent=True) or {}
        required = {"bot_id", "secret", "row", "col"}
        if not required.issubset(payload):
            return {"error": "Expected JSON body with bot_id, secret, row, and col."}, 400
        try:
            result = tournament_manager.submit_move(
                bot_id=str(payload["bot_id"]),
                secret=str(payload["secret"]),
                game_id=game_id,
                row=int(payload["row"]),
                col=int(payload["col"]),
            )
        except KeyError as exc:
            return {"error": str(exc)}, 404
        except PermissionError as exc:
            return {"error": str(exc)}, 403
        except ExternalBotRateLimitError as exc:
            return _rate_limit_error_response(exc)
        except (InvalidMoveError, RuntimeError) as exc:
            return {"error": str(exc)}, 400
        return result, 200

    @app.get("/api/games")
    def list_games() -> tuple[dict[str, object], int]:
        return {"games": manager.list_games()}, 200

    @app.post("/api/games")
    def create_game() -> tuple[dict[str, object], int]:
        payload = request.get_json(silent=True) or {}
        black = payload.get("black")
        white = payload.get("white")
        try:
            game = manager.create_game(black=black, white=white)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return game.to_dict(), 201

    @app.get("/api/games/<game_id>")
    def get_game(game_id: str) -> tuple[dict[str, object], int]:
        try:
            game = manager.get_game(game_id)
        except KeyError as exc:
            return {"error": str(exc)}, 404
        return game.to_dict(), 200

    @app.post("/api/games/<game_id>/move")
    def play_move(game_id: str) -> tuple[dict[str, object], int]:
        payload = request.get_json(silent=True) or {}
        if "row" not in payload or "col" not in payload:
            return {"error": "Expected JSON body with 'row' and 'col'."}, 400
        try:
            game = manager.play_human_move(game_id, int(payload["row"]), int(payload["col"]))
        except KeyError as exc:
            return {"error": str(exc)}, 404
        except InvalidMoveError as exc:
            return {"error": str(exc)}, 400
        return game.to_dict(), 200

    @app.post("/api/games/<game_id>/advance")
    def advance_game(game_id: str) -> tuple[dict[str, object], int]:
        try:
            game = manager.auto_advance(game_id)
        except KeyError as exc:
            return {"error": str(exc)}, 404
        return game.to_dict(), 200

    return app


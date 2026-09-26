from __future__ import annotations

import base64
import time
import unittest

from reversi.app import create_app
from reversi.manager import BOT_TURN_TIMEOUT_SECONDS
from reversi.tournament import EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS


class AppTests(unittest.TestCase):
    remote_server_base_url = "http://192.168.1.20:8000"

    def setUp(self) -> None:
        self.app, self.client = self.create_password_protected_client()
        self.operator_headers = self.basic_operator_headers("swordfish")

    def register_bot(self, name: str) -> dict[str, object]:
        response = self.client.post("/api/tournament/bots/register", json={"name": name})
        self.assertEqual(response.status_code, 201)
        return response.get_json()

    def age_inactive_matches(self, tournament: dict[str, object]) -> None:
        game_manager = self.app.config["game_manager"]
        for match in tournament["matches"]:
            if match["active"]:
                continue
            managed = game_manager.get_game(match["game_id"])
            managed.turn_started_monotonic = time.monotonic() - BOT_TURN_TIMEOUT_SECONDS - 1

    def create_password_protected_client(self, password: str = "swordfish"):
        app = create_app(operator_password=password)
        app.config.update(TESTING=True)
        return app, app.test_client()

    @staticmethod
    def basic_operator_headers(password: str) -> dict[str, str]:
        token = base64.b64encode(f"operator:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def operator_get(self, path: str, **kwargs):
        headers = {**self.operator_headers, **kwargs.pop("headers", {})}
        return self.client.get(path, headers=headers, **kwargs)

    def operator_post(self, path: str, **kwargs):
        headers = {**self.operator_headers, **kwargs.pop("headers", {})}
        return self.client.post(path, headers=headers, **kwargs)

    def operator_delete(self, path: str, **kwargs):
        headers = {**self.operator_headers, **kwargs.pop("headers", {})}
        return self.client.delete(path, headers=headers, **kwargs)

    def operator_put(self, path: str, **kwargs):
        headers = {**self.operator_headers, **kwargs.pop("headers", {})}
        return self.client.put(path, headers=headers, **kwargs)

    @staticmethod
    def wait_for_bot_request_window(seconds: float | None = None) -> None:
        time.sleep((seconds if seconds is not None else EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS) + 0.05)

    def get_assignment(self, bot: dict[str, object], **kwargs):
        path = f"/api/tournament/bots/{bot['bot_id']}/assignment?secret={bot['secret']}"
        response = self.client.get(path, **kwargs)
        if response.status_code == 429:
            self.wait_for_bot_request_window(float(response.get_json()["retry_after_seconds"]))
            response = self.client.get(path, **kwargs)
        return response

    def submit_tournament_move(self, game_id: str, bot: dict[str, object], move: dict[str, int], **kwargs):
        response = self.client.post(
            f"/api/tournament/matches/{game_id}/move",
            json={
                "bot_id": bot["bot_id"],
                "secret": bot["secret"],
                "row": move["row"],
                "col": move["col"],
            },
            **kwargs,
        )
        if response.status_code == 429:
            self.wait_for_bot_request_window(float(response.get_json()["retry_after_seconds"]))
            response = self.client.post(
                f"/api/tournament/matches/{game_id}/move",
                json={
                    "bot_id": bot["bot_id"],
                    "secret": bot["secret"],
                    "row": move["row"],
                    "col": move["col"],
                },
                **kwargs,
            )
        return response

    def test_create_local_human_vs_greedy_game(self) -> None:
        response = self.operator_post(
            "/api/games",
            json={"black": {"type": "human"}, "white": {"type": "greedy"}},
        )
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertEqual(data["current_player"], "B")
        self.assertEqual(data["status"], "active")
        self.assertEqual(len(data["legal_moves"]), 4)

    def test_human_move_triggers_bot_reply(self) -> None:
        created = self.operator_post(
            "/api/games",
            json={"black": {"type": "human"}, "white": {"type": "greedy"}},
        ).get_json()
        game_id = created["game_id"]

        response = self.operator_post(f"/api/games/{game_id}/move", json={"row": 2, "col": 3})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["current_player"], "B")
        self.assertGreaterEqual(data["move_count"], 2)

    def test_bot_vs_bot_game_finishes(self) -> None:
        response = self.operator_post(
            "/api/games",
            json={"black": {"type": "random", "seed": 7}, "white": {"type": "greedy"}},
        )
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertEqual(data["status"], "finished")
        self.assertIn(data["winner"], {"B", "W", "draw"})
        self.assertGreaterEqual(data["move_count"], 1)

    def test_rejects_invalid_remote_player_spec(self) -> None:
        response = self.operator_post(
            "/api/games",
            json={"black": {"type": "remote"}, "white": {"type": "human"}},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("url", response.get_json()["error"])

    def test_rejects_operator_routes_without_password(self) -> None:
        remote_environ = {"REMOTE_ADDR": "203.0.113.10"}

        index_response = self.client.get(
            "/",
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(index_response.status_code, 401)
        self.assertIn("password", index_response.get_data(as_text=True).lower())

        games_response = self.client.get(
            "/api/games",
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(games_response.status_code, 401)
        self.assertIn("password", games_response.get_json()["error"].lower())

        bots_response = self.client.get(
            "/api/tournament/bots",
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(bots_response.status_code, 401)
        self.assertIn("password", bots_response.get_json()["error"].lower())

    def test_allows_operator_routes_with_basic_auth(self) -> None:
        remote_environ = {"REMOTE_ADDR": "203.0.113.10"}

        index_response = self.operator_get(
            "/",
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(index_response.status_code, 200)

        create_response = self.operator_post(
            "/api/games",
            json={"black": {"type": "human"}, "white": {"type": "greedy"}},
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(create_response.status_code, 201)

    def test_index_exposes_poll_rate_controls(self) -> None:
        response = self.operator_get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("poll-rate-input", html)
        self.assertIn("save-poll-rate", html)
        self.assertIn('value="250"', html)
        # The slider was removed; the number input is the only way to set the rate.
        self.assertNotIn("poll-rate-slider", html)

    def test_index_poll_rate_controls_support_explicit_apply_and_reset(self) -> None:
        response = self.operator_get("/")
        html = response.get_data(as_text=True)
        # The operator must be able to discard an edit and see the active server value,
        # because the periodic refresh no longer overwrites the inputs while editing.
        self.assertIn("reset-poll-rate", html)
        self.assertIn("poll-rate-active", html)
        self.assertIn("poll-rate-pending", html)
        # Both buttons start disabled until the operator actually changes something.
        self.assertIn('id="save-poll-rate" class="secondary-button" disabled', html)
        self.assertIn('id="reset-poll-rate" class="secondary-button" disabled', html)

    def test_index_reflects_the_current_poll_rate(self) -> None:
        self.app.config["tournament_manager"].set_external_bot_request_interval_milliseconds(400)

        html = self.operator_get("/").get_data(as_text=True)

        self.assertIn('value="400"', html)
        self.assertIn("400 ms", html)

    def test_allows_operator_routes_with_password_header(self) -> None:
        remote_environ = {"REMOTE_ADDR": "203.0.113.10"}
        response = self.client.get(
            "/api/games",
            headers={"X-Reversi-Operator-Password": "swordfish"},
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(response.status_code, 200)

    def test_external_bot_endpoints_remain_accessible_remotely(self) -> None:
        alpha_ip = "203.0.113.10"
        beta_ip = "203.0.113.11"
        alpha = self.client.post(
            "/api/tournament/bots/register",
            json={"name": "alpha"},
            base_url=self.remote_server_base_url,
            environ_overrides={"REMOTE_ADDR": alpha_ip},
        ).get_json()
        beta = self.client.post(
            "/api/tournament/bots/register",
            json={"name": "beta"},
            base_url=self.remote_server_base_url,
            environ_overrides={"REMOTE_ADDR": beta_ip},
        ).get_json()

        started = self.operator_post("/api/tournaments", json={"double_round_robin": False})
        self.assertEqual(started.status_code, 201)

        assignment = None
        acting_bot = None
        acting_ip = None
        for bot, remote_ip in ((alpha, alpha_ip), (beta, beta_ip)):
            assignment_response = self.get_assignment(
                bot,
                base_url=self.remote_server_base_url,
                environ_overrides={"REMOTE_ADDR": remote_ip},
            )
            self.assertEqual(assignment_response.status_code, 200)
            payload = assignment_response.get_json()
            if payload["available"]:
                assignment = payload
                acting_bot = bot
                acting_ip = remote_ip
                break

        self.assertIsNotNone(assignment)
        move = assignment["legal_moves"][0]
        submit_response = self.submit_tournament_move(
            assignment["game_id"],
            acting_bot,
            move,
            base_url=self.remote_server_base_url,
            environ_overrides={"REMOTE_ADDR": acting_ip},
        )
        self.assertEqual(submit_response.status_code, 200)

    def test_external_bot_docs_remain_accessible_remotely(self) -> None:
        remote_environ = {"REMOTE_ADDR": "203.0.113.10"}

        swagger_response = self.client.get(
            "/swagger",
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(swagger_response.status_code, 200)
        swagger_html = swagger_response.get_data(as_text=True)
        self.assertIn("SwaggerUIBundle", swagger_html)
        self.assertIn("/openapi.json", swagger_html)

        instructions_response = self.client.get(
            "/instructions",
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(instructions_response.status_code, 200)
        instructions_html = instructions_response.get_data(as_text=True)
        self.assertIn("/api/tournament/bots/register", instructions_html)
        self.assertIn("bot_timeout_exceeded", instructions_html)
        self.assertIn("0.25 seconds", instructions_html)
        self.assertIn("429 Too Many Requests", instructions_html)
        self.assertIn("Retry-After", instructions_html)
        self.assertIn("limit_seconds", instructions_html)
        self.assertIn("change that rate during a tournament", instructions_html)

        openapi_response = self.client.get(
            "/openapi.json",
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(openapi_response.status_code, 200)
        openapi_payload = openapi_response.get_json()
        self.assertEqual(openapi_payload["info"]["title"], "Reversi Workshop Bot API")
        self.assertIn("10 second turn timeout", openapi_payload["info"]["description"])
        self.assertIn("must currently wait at least 0.25 seconds", openapi_payload["info"]["description"])
        self.assertIn("Retry-After", openapi_payload["info"]["description"])
        self.assertIn("/api/tournament/bots/register", openapi_payload["paths"])
        self.assertIn("/api/tournament/bots/{bot_id}/assignment", openapi_payload["paths"])
        self.assertIn("/api/tournament/matches/{game_id}/move", openapi_payload["paths"])
        self.assertIn("429", openapi_payload["paths"]["/api/tournament/bots/{bot_id}/assignment"]["get"]["responses"])
        self.assertIn(
            "Retry-After",
            openapi_payload["paths"]["/api/tournament/bots/{bot_id}/assignment"]["get"]["responses"]["429"]["headers"],
        )
        self.assertIn(
            "limit_seconds",
            openapi_payload["components"]["schemas"]["AssignmentResponse"]["properties"],
        )

    def test_operator_can_update_live_tournament_settings(self) -> None:
        settings_response = self.operator_get("/api/tournament/settings")
        self.assertEqual(settings_response.status_code, 200)
        self.assertEqual(settings_response.get_json()["external_bot_request_interval_ms"], 250)

        updated = self.operator_put(
            "/api/tournament/settings",
            json={"external_bot_request_interval_ms": 500},
        )
        self.assertEqual(updated.status_code, 200)
        payload = updated.get_json()
        self.assertEqual(payload["external_bot_request_interval_ms"], 500)
        self.assertEqual(payload["external_bot_request_interval_seconds"], 0.5)
        self.assertTrue(payload["applies_during_tournaments"])

        html = self.operator_get("/").get_data(as_text=True)
        self.assertIn('value="500"', html)

        alpha = self.register_bot("alpha")
        first = self.client.get(f"/api/tournament/bots/{alpha['bot_id']}/assignment?secret={alpha['secret']}")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["limit_seconds"], 0.5)

        limited = self.client.get(f"/api/tournament/bots/{alpha['bot_id']}/assignment?secret={alpha['secret']}")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.get_json()["limit_seconds"], 0.5)

    def test_external_bot_endpoints_remain_public_without_operator_auth(self) -> None:
        remote_environ = {"REMOTE_ADDR": "203.0.113.10"}

        registered = self.client.post(
            "/api/tournament/bots/register",
            json={"name": "alpha"},
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(registered.status_code, 201)

        docs = self.client.get(
            "/swagger",
            base_url=self.remote_server_base_url,
            environ_overrides=remote_environ,
        )
        self.assertEqual(docs.status_code, 200)

    def test_registers_tournament_bots_and_hides_secrets_from_list(self) -> None:
        registered = self.register_bot("alpha")
        self.assertIn("secret", registered)

        response = self.operator_get("/api/tournament/bots")
        self.assertEqual(response.status_code, 200)
        bots = response.get_json()["bots"]
        self.assertEqual(len(bots), 1)
        self.assertEqual(bots[0]["name"], "alpha")
        self.assertNotIn("secret", bots[0])

    def test_rejects_duplicate_tournament_bot_names(self) -> None:
        self.register_bot("alpha")
        response = self.client.post("/api/tournament/bots/register", json={"name": "  ALPHA  "})
        self.assertEqual(response.status_code, 400)
        self.assertIn("unique", response.get_json()["error"])

    def test_operator_can_remove_bot_before_tournament(self) -> None:
        alpha = self.register_bot("alpha")
        response = self.operator_delete(f"/api/tournament/bots/{alpha['bot_id']}")
        self.assertEqual(response.status_code, 200)

        listed = self.operator_get("/api/tournament/bots").get_json()
        self.assertEqual(listed["bots"], [])

    def test_requires_two_bots_to_start_tournament(self) -> None:
        self.register_bot("solo")
        response = self.operator_post("/api/tournaments", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("At least two", response.get_json()["error"])

    def test_accepts_named_tournament_format(self) -> None:
        self.register_bot("alpha")
        self.register_bot("beta")
        self.register_bot("gamma")
        self.register_bot("delta")

        response = self.operator_post(
            "/api/tournaments",
            json={"tournament_format": "single_elimination"},
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["format"], "single_elimination")
        self.assertEqual(payload["match_count"], 3)
        self.assertEqual(payload["ongoing_match_count"], 2)
        self.assertIn("single_elimination", payload["supported_formats"])
        self.assertIsNotNone(payload["bracket"])
        self.assertIsNone(payload["champion"])
        self.assertEqual(payload["bracket"]["rounds"][0]["round_name"], "Semifinal")

    def test_single_elimination_api_reports_champion_after_playoff_finishes(self) -> None:
        self.register_bot("alpha")
        self.register_bot("beta")
        self.register_bot("gamma")
        self.register_bot("delta")

        response = self.operator_post(
            "/api/tournaments",
            json={"tournament_format": "single_elimination"},
        )
        self.assertEqual(response.status_code, 201)
        tournament = response.get_json()

        game_manager = self.app.config["game_manager"]
        for match in tournament["ongoing_matches"]:
            game_manager.get_game(match["game_id"]).game.forfeit("W", "test_complete")

        after_semis = self.operator_get(f"/api/tournaments/{tournament['tournament_id']}")
        self.assertEqual(after_semis.status_code, 200)
        semifinal_payload = after_semis.get_json()
        self.assertEqual(semifinal_payload["ongoing_match_count"], 1)
        final_game_id = semifinal_payload["ongoing_matches"][0]["game_id"]
        game_manager.get_game(final_game_id).game.forfeit("W", "test_complete")

        finished = self.operator_get(f"/api/tournaments/{tournament['tournament_id']}")
        self.assertEqual(finished.status_code, 200)
        payload = finished.get_json()
        self.assertEqual(payload["status"], "finished")
        self.assertEqual(payload["champion"]["name"], "alpha")
        self.assertEqual(payload["bracket"]["winner"]["name"], "alpha")
        self.assertEqual(payload["bracket"]["rounds"][-1]["matches"][0]["winner_name"], "alpha")

    def test_rejects_invalid_named_tournament_format(self) -> None:
        self.register_bot("alpha")
        self.register_bot("beta")

        response = self.operator_post(
            "/api/tournaments",
            json={"tournament_format": "playoffs"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported tournament format", response.get_json()["error"])

    def test_rejects_non_string_tournament_format(self) -> None:
        self.register_bot("alpha")
        self.register_bot("beta")

        response = self.operator_post(
            "/api/tournaments",
            json={"tournament_format": True},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("tournament_format must be a string", response.get_json()["error"])

    def test_rejects_tournament_move_with_wrong_secret(self) -> None:
        alpha = self.register_bot("alpha")
        beta = self.register_bot("beta")
        tournament = self.operator_post(
            "/api/tournaments",
            json={"double_round_robin": False},
        ).get_json()
        assignment = self.get_assignment(alpha).get_json()
        acting_bot = alpha if assignment["available"] else beta
        if not assignment["available"]:
            assignment = self.get_assignment(beta).get_json()
        move = assignment["legal_moves"][0]

        response = self.client.post(
            f"/api/tournament/matches/{assignment['game_id']}/move",
            json={
                "bot_id": acting_bot["bot_id"],
                "secret": "wrong-secret",
                "row": move["row"],
                "col": move["col"],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("secret", response.get_json()["error"])
        self.assertEqual(tournament["status"], "running")

    def test_rate_limits_external_assignment_requests(self) -> None:
        alpha = self.register_bot("alpha")

        first = self.client.get(f"/api/tournament/bots/{alpha['bot_id']}/assignment?secret={alpha['secret']}")
        self.assertEqual(first.status_code, 200)

        limited = self.client.get(f"/api/tournament/bots/{alpha['bot_id']}/assignment?secret={alpha['secret']}")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["Retry-After"], "1")
        payload = limited.get_json()
        self.assertEqual(payload["limit_seconds"], EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS)
        self.assertGreater(payload["retry_after_seconds"], 0)
        self.assertLessEqual(payload["retry_after_seconds"], EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS)

    def test_rate_limits_move_immediately_after_assignment_then_allows_retry(self) -> None:
        alpha = self.register_bot("alpha")
        beta = self.register_bot("beta")
        self.operator_post("/api/tournaments", json={"double_round_robin": False})

        assignment = self.client.get(
            f"/api/tournament/bots/{alpha['bot_id']}/assignment?secret={alpha['secret']}"
        ).get_json()
        acting_bot = alpha if assignment["available"] else beta
        if not assignment["available"]:
            assignment = self.client.get(
                f"/api/tournament/bots/{beta['bot_id']}/assignment?secret={beta['secret']}"
            ).get_json()

        move = assignment["legal_moves"][0]
        limited = self.client.post(
            f"/api/tournament/matches/{assignment['game_id']}/move",
            json={
                "bot_id": acting_bot["bot_id"],
                "secret": acting_bot["secret"],
                "row": move["row"],
                "col": move["col"],
            },
        )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["Retry-After"], "1")

        time.sleep(EXTERNAL_BOT_REQUEST_INTERVAL_SECONDS + 0.05)

        played = self.client.post(
            f"/api/tournament/matches/{assignment['game_id']}/move",
            json={
                "bot_id": acting_bot["bot_id"],
                "secret": acting_bot["secret"],
                "row": move["row"],
                "col": move["col"],
            },
        )
        self.assertEqual(played.status_code, 200)

    def test_cannot_remove_bot_from_running_tournament(self) -> None:
        alpha = self.register_bot("alpha")
        self.register_bot("beta")
        started = self.operator_post("/api/tournaments", json={"double_round_robin": False})
        self.assertEqual(started.status_code, 201)

        response = self.operator_delete(f"/api/tournament/bots/{alpha['bot_id']}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("running tournament", response.get_json()["error"])

    def test_tournament_only_runs_disjoint_matches_in_parallel(self) -> None:
        alpha = self.register_bot("alpha")
        beta = self.register_bot("beta")
        gamma = self.register_bot("gamma")
        delta = self.register_bot("delta")

        response = self.operator_post("/api/tournaments", json={"double_round_robin": False})
        self.assertEqual(response.status_code, 201)
        tournament = response.get_json()
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

        alpha_assignment = self.get_assignment(alpha)
        self.assertEqual(alpha_assignment.status_code, 200)
        assignment = alpha_assignment.get_json()
        self.assertTrue(assignment["available"])

        target_match = next(
            match for match in tournament["matches"]
            if match["black"]["bot_id"] == alpha["bot_id"] and match["white"]["bot_id"] == beta["bot_id"]
        )
        self.assertEqual(assignment["game_id"], target_match["game_id"])

        idle_assignment = self.get_assignment(beta)
        self.assertEqual(idle_assignment.status_code, 200)
        self.assertFalse(idle_assignment.get_json()["available"])

        move = assignment["legal_moves"][0]
        played = self.submit_tournament_move(assignment["game_id"], alpha, move)
        self.assertEqual(played.status_code, 200)
        result = played.get_json()
        self.assertEqual(result["game"]["game_id"], target_match["game_id"])
        self.assertGreaterEqual(result["tournament"]["ongoing_match_count"], 2)

    def test_round_robin_assignment_survives_late_match_activation(self) -> None:
        alpha = self.register_bot("alpha")
        beta = self.register_bot("beta")
        gamma = self.register_bot("gamma")
        delta = self.register_bot("delta")
        bots_by_id = {bot["bot_id"]: bot for bot in (alpha, beta, gamma, delta)}

        response = self.operator_post("/api/tournaments", json={"double_round_robin": False})
        self.assertEqual(response.status_code, 201)
        tournament = response.get_json()
        self.age_inactive_matches(tournament)

        game_manager = self.app.config["game_manager"]
        for match in tournament["ongoing_matches"]:
            game_manager.get_game(match["game_id"]).game.forfeit("B", "test_complete")

        tournament_id = tournament["tournament_id"]
        scheduled = self.operator_get(f"/api/tournaments/{tournament_id}")
        self.assertEqual(scheduled.status_code, 200)
        scheduled_payload = scheduled.get_json()
        self.assertEqual(scheduled_payload["completed_match_count"], 2)
        self.assertEqual(scheduled_payload["ongoing_match_count"], 2)

        resynced = self.operator_get(f"/api/tournaments/{tournament_id}")
        self.assertEqual(resynced.status_code, 200)
        resynced_payload = resynced.get_json()
        self.assertEqual(resynced_payload["completed_match_count"], 2)
        self.assertEqual(resynced_payload["ongoing_match_count"], 2)

        active_match = resynced_payload["ongoing_matches"][0]
        active_bot = bots_by_id[active_match["black"]["bot_id"]]
        assignment = self.get_assignment(active_bot)
        self.assertEqual(assignment.status_code, 200)
        assignment_payload = assignment.get_json()
        self.assertTrue(assignment_payload["available"])
        self.assertEqual(assignment_payload["game_id"], active_match["game_id"])

    def test_operator_started_tournament_finishes_via_registered_bots(self) -> None:
        alpha = self.register_bot("alpha")
        beta = self.register_bot("beta")

        response = self.operator_post(
            "/api/tournaments",
            json={"double_round_robin": False},
        )
        self.assertEqual(response.status_code, 201)
        tournament = response.get_json()
        self.assertEqual(tournament["status"], "running")
        self.assertEqual(tournament["match_count"], 1)
        self.assertEqual(len(tournament["bots"]), 2)
        self.assertEqual(len(tournament["ongoing_matches"]), 1)
        self.assertEqual(len(tournament["ongoing_matches"][0]["board"]), 8)
        self.assertEqual(tournament["ongoing_matches"][0]["current_player"], "B")

        latest = tournament
        for _ in range(80):
            if latest["status"] == "finished":
                break
            progressed = False
            for bot in (alpha, beta):
                assignment_response = self.get_assignment(bot)
                self.assertEqual(assignment_response.status_code, 200)
                assignment = assignment_response.get_json()
                if assignment.get("available") is not True:
                    continue
                move = assignment["legal_moves"][0]
                submit_response = self.submit_tournament_move(assignment["game_id"], bot, move)
                self.assertEqual(submit_response.status_code, 200)
                result = submit_response.get_json()
                latest = result["tournament"]
                progressed = True
                break
            if not progressed:
                latest = self.operator_get(f"/api/tournaments/{tournament['tournament_id']}").get_json()

        self.assertEqual(latest["status"], "finished")
        self.assertEqual(latest["completed_match_count"], 1)
        self.assertEqual(len(latest["standings"]), 2)
        self.assertIn(latest["matches"][0]["winner"], {"B", "W", "draw"})

        game_id = latest["matches"][0]["game_id"]
        game = self.operator_get(f"/api/games/{game_id}").get_json()
        self.assertEqual(game["metadata"]["mode"], "tournament")
        self.assertEqual(game["status"], "finished")


if __name__ == "__main__":
    unittest.main()

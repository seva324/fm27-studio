"""Tests for the AI assistant: the prompt, the plans and the key store.

Nothing here talks to a provider. The transport is exercised through its own
pure helpers (the endpoint normaliser, the status messages and the reply
reader), and the plans are checked against the game's own files.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from fmrte import assistant as A
from fmrte import fmplans
from fmrte import fmtools as F
from fmrte import fmroles
from fmrte import spec
from fmrte import tacticfiles


class PromptTests(unittest.TestCase):
    def test_the_envelope_is_the_assistants_own(self):
        prompt = A.build_prompt(_FakeDb(), "Who should I sell?")
        self.assertIn("The following Football Manager 2027 context was loaded "
                      "locally by the application.", prompt)
        self.assertIn("User message:\nWho should I sell?", prompt)
        # The three layers are all present.
        self.assertIn("S20-CA-INERT", prompt)
        self.assertIn("The loaded save:", prompt)

    def test_the_always_on_conclusions_are_few(self):
        # The assistant keeps this list small because every message pays for it.
        self.assertLessEqual(len(A.ALWAYS_ON), 6)
        codes = [code for code, _text in A.ALWAYS_ON]
        self.assertIn("S20-CA-INERT", codes)
        self.assertIn("S5-SHARPNESS-FIRST", codes)

    def test_the_save_context_names_the_game_date_and_counts(self):
        context = A.save_context(_FakeDb())
        self.assertIn("2026-07-20", context)
        self.assertIn("42,344 players", context)

    def test_the_injured_note_comes_from_the_data_layer(self):
        context = A.save_context(_FakeDb())
        self.assertIn("Mike Maignan", context)
        self.assertIn("Hamstring", context)


class EndpointTests(unittest.TestCase):
    def test_a_base_url_becomes_one_completions_endpoint(self):
        self.assertEqual(A._endpoint("https://api.commandcode.ai/provider/v1"),
                         "https://api.commandcode.ai/provider/v1/chat/completions")
        self.assertEqual(A._endpoint("https://api.commandcode.ai/provider/v1/"),
                         "https://api.commandcode.ai/provider/v1/chat/completions")

    def test_a_full_endpoint_is_not_doubled(self):
        self.assertEqual(
            A._endpoint("https://x.test/v1/chat/completions"),
            "https://x.test/v1/chat/completions")

    def test_a_plain_address_is_refused(self):
        with self.assertRaises(A.AssistantError):
            A._endpoint("api.example.com")

    def test_the_status_messages_are_the_assistants(self):
        self.assertIn("Invalid API key", A._status_message(401))
        self.assertIn("insufficient credits", A._status_message(402))
        self.assertIn("Rate limit", A._status_message(429))

    def test_the_reply_reader_handles_the_openai_shape(self):
        payload = {"choices": [{"message": {"content": "  Sell the winger.  "}}]}
        self.assertEqual(A._extract_text(payload), "Sell the winger.")

    def test_the_reply_reader_handles_a_content_list(self):
        payload = {"choices": [{"message": {"content": [
            {"type": "text", "text": "First"}, {"type": "text", "text": "Second"}]}}]}
        self.assertEqual(A._extract_text(payload), "First\nSecond")

    def test_an_empty_reply_is_empty_not_an_error(self):
        self.assertEqual(A._extract_text({"choices": []}), "")

    def test_an_http_failure_carries_its_status(self):
        """A5: the number is on the error, not only inside its prose."""
        error = urllib.error.HTTPError(
            "https://x.test/v1/chat/completions", 404, "Not Found", None, None)
        with mock.patch.object(A.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(A.AssistantError) as caught:
                A._http_chat(A.AssistantSettings(), "key",
                             [{"role": "user", "content": "Hi"}])
        self.assertEqual(caught.exception.status, 404)
        # ... and the prose alone could never have been searched for it.
        self.assertNotIn("404", str(caught.exception))

    def test_a_failure_without_a_status_keeps_none(self):
        self.assertIsNone(A.AssistantError("no key").status)

    def test_remote_disconnect_is_retried_and_wrapped_cleanly(self):
        import http.client
        import io

        class _FakeResp:
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self, lines):
                self._lines = lines

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                return iter(self._lines)

        sse_lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"Think"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"Done"}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"fm27_","arguments":"{\\"a\\":"}}]}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"ok","arguments":"1}"}}]}}]}\n',
            b'data: {"choices":[],"usage":{"completion_tokens_details":{"reasoning_tokens":42}}}\n',
            b'data: [DONE]\n',
        ]
        attempts = {"n": 0}

        def flaky_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
            return _FakeResp(sse_lines)

        with mock.patch.object(A.urllib.request, "urlopen", side_effect=flaky_urlopen), \
                mock.patch.object(A.time, "sleep", return_value=None):
            out = A._http_chat(A.AssistantSettings(), "key", [{"role": "user", "content": "Hi"}])
        self.assertEqual(attempts["n"], 2)
        msg = out["choices"][0]["message"]
        self.assertEqual(msg["content"], "Done")
        self.assertEqual(msg["reasoning_content"], "Think")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "fm27_ok")
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"], '{"a":1}')
        self.assertEqual(A._extract_reasoning_tokens(out), 42)

        with mock.patch.object(
            A.urllib.request, "urlopen",
            side_effect=http.client.RemoteDisconnected("Remote end closed connection without response"),
        ), mock.patch.object(A.time, "sleep", return_value=None):
            with self.assertRaises(A.AssistantError) as caught:
                A._http_chat(A.AssistantSettings(), "key", [{"role": "user", "content": "Hi"}])
        self.assertIn("closed the connection", str(caught.exception))


class HistoryContractTests(unittest.TestCase):
    """A1: one user turn per request, even when the caller already recorded it."""

    def setUp(self):
        self.sent: list[list[dict]] = []
        self.original = A._http_chat

        def fake(settings, key, messages, tools=None, timeout=None):
            self.sent.append([dict(message) for message in messages])
            return {"choices": [{"message": {"content": "ok"}}]}

        A._http_chat = fake

    def tearDown(self):
        A._http_chat = self.original

    def _turns(self, index: int = 0) -> list[str]:
        return [message["role"] for message in self.sent[index]]

    def _assert_no_adjacent_duplicate_user_turn(self, index: int = 0) -> None:
        """The wire format a strict gateway accepts: one user turn per ask."""
        messages = self.sent[index]
        for previous, current in zip(messages, messages[1:]):
            if previous["role"] != "user" or current["role"] != "user":
                continue
            self.assertNotEqual(previous["content"], current["content"],
                                "the same user turn was sent twice in a row")

    def test_a_history_that_repeats_the_current_turn_sends_it_once(self):
        # Exactly what the panel used to pass: the conversation including the
        # message being asked about, plus the message as its own argument.
        history = [
            {"role": "user", "text": "Who is my best defender?"},
            {"role": "assistant", "text": "Pulisic, on the left."},
            {"role": "user", "text": "And the striker?"},
        ]
        answer = A.chat_with_tools(A.AssistantSettings(), "key", _FakeDb(),
                                   "And the striker?", history=history)
        self.assertEqual(answer, "ok")
        messages = self.sent[0]
        users = [message["content"] for message in messages
                 if message["role"] == "user"]
        self.assertEqual(users, ["Who is my best defender?", "And the striker?"])
        # The two turns the person actually asked, and no adjacent duplicate.
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "And the striker?")
        self._assert_no_adjacent_duplicate_user_turn()

    def test_the_contract_is_in_the_docstring(self):
        doc = A.chat_with_tools.__doc__ or ""
        self.assertIn("must not already contain it", " ".join(doc.split()))

    def test_a_history_without_the_current_turn_is_sent_unchanged(self):
        history = [
            {"role": "user", "text": "Who is my best defender?"},
            {"role": "assistant", "text": "Pulisic, on the left."},
        ]
        A.chat_with_tools(A.AssistantSettings(), "key", _FakeDb(),
                          "And the striker?", history=history)
        self.assertEqual(self._turns(), ["system", "user", "assistant", "user"])
        self.assertEqual(self.sent[0][-1]["content"], "And the striker?")
        self._assert_no_adjacent_duplicate_user_turn()


class NoToolFallbackTests(unittest.TestCase):
    """A5: the no-tools fallback is chosen by status, not by prose."""

    def setUp(self):
        self.db = _FakeDb()
        self.settings = A.AssistantSettings(provider="commandcode")
        self.original = A._http_chat

    def tearDown(self):
        A._http_chat = self.original

    def test_each_rejection_status_falls_back_to_a_plain_completion(self):
        for status in (400, 404, 422, 501):
            with self.subTest(status=status):
                calls: list[bool] = []

                def fake(settings, key, messages, tools=None, timeout=None,
                         status=status):
                    calls.append(bool(tools))
                    if tools:
                        raise A.AssistantError(A._status_message(status),
                                               status=status)
                    return {"choices": [{"message": {"content": "Plain answer."}}]}

                A._http_chat = fake
                answer = A.chat_with_tools(self.settings, "key", self.db, "Hi")
                self.assertEqual(answer, "Plain answer.")
                self.assertEqual(calls, [True, False])

    def test_the_404_message_alone_would_not_have_triggered_it(self):
        # The old branch searched str(exc) for "400".  This message carries no
        # status digit at all, which is why a 404 never fell back; the status
        # attribute is what the branch reads now.
        self.assertNotIn("404", A._status_message(404))
        self.assertNotIn("400", A._status_message(404))

    def test_a_status_that_is_not_a_rejection_is_raised(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):

                def fake(settings, key, messages, tools=None, timeout=None,
                         status=status):
                    raise A.AssistantError(A._status_message(status),
                                           status=status)

                A._http_chat = fake
                with self.assertRaises(A.AssistantError) as caught:
                    A.chat_with_tools(self.settings, "key", self.db, "Hi")
                self.assertEqual(caught.exception.status, status)


class TacticTests(unittest.TestCase):
    def test_a_formation_is_counted_the_way_the_game_names_it(self):
        positions = ["Goalkeeper", "DefenderLeft", "DefenderRight",
                     "DefenderCentral", "DefenderCentral",
                     "DefensiveMidfielder", "DefensiveMidfielder",
                     "AttackingMidfielderCentral", "AttackingMidfielderLeft",
                     "AttackingMidfielderRight", "Striker"]
        self.assertEqual(A.formation_label(positions), "4-2-3-1")

    def test_a_plan_names_a_role_for_every_slot(self):
        templates = [{"file": "4-2-3-1.json", "name": "4-2-3-1",
                      "positions": ["Goalkeeper", "DefenderLeft",
                                    "DefenderRight", "DefenderCentral",
                                    "DefenderCentral", "DefensiveMidfielder",
                                    "DefensiveMidfielder",
                                    "AttackingMidfielderCentral",
                                    "AttackingMidfielderLeft",
                                    "AttackingMidfielderRight", "Striker"]}]
        plan = A.build_tactic_plan(templates, "4-2-3-1", "Attacking", "")
        self.assertEqual(len(plan.slots), 11)
        self.assertEqual(plan.formation, "4-2-3-1")
        for slot in plan.slots:
            self.assertTrue(slot.role)
            self.assertIn(slot.duty, A.DUTIES)
        self.assertIn("mentality", plan.instructions)
        self.assertIn("Eleven roles:", plan.sheet())

    def test_the_style_changes_the_instructions(self):
        templates = [{"file": "x.json", "name": "x",
                      "positions": ["Goalkeeper"] + ["DefenderCentral"] * 4
                                   + ["MidfielderCentral"] * 4 + ["Striker"] * 2}]
        attacking = A.build_tactic_plan(templates, "x", "Attacking", "")
        defensive = A.build_tactic_plan(templates, "x", "Defensive", "")
        self.assertEqual(attacking.instructions["mentality"], "Positive")
        self.assertEqual(defensive.instructions["mentality"], "Cautious")

    def test_every_instruction_value_is_one_the_decoder_accepts(self):
        # The tactic decoder's own vocabulary: a neutral value exists for every
        # setting, and the scale settings run from much lower to much higher.
        for setting, values in A.TEAM_INSTRUCTIONS.items():
            self.assertTrue(values, setting)
            self.assertGreaterEqual(len(values), 3, setting)
        self.assertIn("Balanced", A.TEAM_INSTRUCTIONS["mentality"])
        self.assertIn("Standard", A.TEAM_INSTRUCTIONS["tempo"])
        self.assertIn("Much Higher", A.TEAM_INSTRUCTIONS["tempo"])

    def test_the_games_own_templates_are_read_when_present(self):
        templates = A.load_templates()
        if not templates:
            self.skipTest("the game's Tactics folder is not on this machine")
        for template in templates:
            self.assertEqual(len(template["positions"]), 11)
            self.assertTrue(template["name"])


class TrainingTests(unittest.TestCase):
    def test_a_week_has_seven_days_of_three_sessions(self):
        plan = A.build_training_plan("Balanced")
        self.assertEqual(len(plan.days), 7)
        for day in plan.days:
            self.assertEqual(len(day), A.SESSIONS_PER_DAY)

    def test_every_session_is_a_measured_identifier(self):
        for focus in ("Balanced", "Attacking", "Defensive press",
                      "Ball control", "Recovery", "Team bonding"):
            plan = A.build_training_plan(focus)
            for day in plan.days:
                for activity in day:
                    self.assertIn(activity, A.ACTIVITY_BY_ID,
                                  f"{focus}: {activity}")

    def test_the_identifiers_are_the_assistants_table(self):
        self.assertEqual(A.ACTIVITY_BY_ID[0][1], "Passing scenario")
        self.assertEqual(A.ACTIVITY_BY_ID[300][1], "Rest")
        self.assertEqual(A.ACTIVITY_BY_ID[301][1], "Recovery")
        self.assertEqual(A.ACTIVITY_BY_ID[500][1], "Match")
        self.assertEqual(A.ACTIVITY_BY_ID[102][2], "进攻三区")

    def test_the_focus_changes_the_week(self):
        press = A.build_training_plan("Defensive press")
        recovery = A.build_training_plan("Recovery")
        self.assertNotEqual(press.days, recovery.days)
        self.assertIn(301, recovery.days[0])
        self.assertIn(5, press.days[0])

    def test_the_sheet_names_every_day(self):
        sheet = A.build_training_plan("Attacking").sheet()
        for day in A.DAYS:
            self.assertIn(day, sheet)

    def test_the_assistant_week_and_the_tool_week_are_one_plan(self):
        # The panel builds its week with build_training_plan and the model's
        # fm27_create_training_schedule tool builds it with training_week; both
        # are the same generator, so the sheets must be identical.
        for focus in ("Balanced", "Attacking", "Defensive press",
                      "Ball control", "Recovery", "Team bonding"):
            self.assertEqual(A.build_training_plan(focus).sheet(),
                             F.training_week(focus)["sheet"], focus)


class OneAuthorityTests(unittest.TestCase):
    """The assistant re-exports the plan rules; it does not declare them."""

    def test_the_activity_table_is_the_plan_module_table(self):
        self.assertIs(A.TRAINING_ACTIVITIES, fmplans.TRAINING_ACTIVITIES)
        self.assertIs(A.ACTIVITY_BY_ID, fmplans.ACTIVITY_BY_ID)
        self.assertIs(A.DAYS, fmplans.DAYS)
        self.assertIs(A.SESSIONS_PER_DAY, fmplans.SESSIONS_PER_DAY)

    def test_the_instruction_table_is_the_plan_module_table(self):
        self.assertIs(A.TEAM_INSTRUCTIONS, fmplans.TEAM_INSTRUCTIONS)
        self.assertIs(A.TEAM_INSTRUCTIONS, fmplans.TEAM_INSTRUCTION_VALUES)

    def test_the_position_labels_are_the_spec_table(self):
        self.assertIs(A.POSITION_LABELS, spec.POSITION_LABELS)
        self.assertIs(A.POSITION_LABELS, fmroles.POSITION_LABELS)

    def test_the_template_reader_is_the_neutral_module(self):
        self.assertIs(A.load_templates, tacticfiles.load_templates)
        self.assertIs(A.tactic_templates, tacticfiles.tactic_templates)
        self.assertIs(A.game_data_directory, tacticfiles.game_data_directory)

    def test_the_tool_catalogue_has_no_second_name(self):
        # The panel and the model call fmtools directly; the two-line
        # delegations this refactor removed were only a second name.
        self.assertFalse(hasattr(A, "tool_definitions"))
        self.assertFalse(hasattr(A, "execute_tool"))


class SettingsTests(unittest.TestCase):
    def test_the_defaults_are_the_assistants_own(self):
        settings = A.AssistantSettings()
        self.assertEqual(settings.provider, "commandcode")
        self.assertEqual(settings.base_url, A.DEFAULT_BASE_URL)
        self.assertEqual(settings.model, A.DEFAULT_MODEL)

    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = os.environ.get("FMRTE_ASSISTANT_DIR")
            os.environ["FMRTE_ASSISTANT_DIR"] = folder
            try:
                settings = A.AssistantSettings(provider="openrouter",
                                               model="some/model")
                settings.save()
                loaded = A.AssistantSettings.load()
                self.assertEqual(loaded.provider, "openrouter")
                self.assertEqual(loaded.model, "some/model")
            finally:
                if previous is None:
                    os.environ.pop("FMRTE_ASSISTANT_DIR", None)
                else:
                    os.environ["FMRTE_ASSISTANT_DIR"] = previous

    def test_a_key_is_never_written_in_plain_text(self):
        with tempfile.TemporaryDirectory() as folder:
            store = A.KeyStore(os.path.join(folder, "provider.key"))
            secret = "sk-test-0123456789"
            store.save(secret)
            self.assertEqual(store.load(), secret)
            if os.path.isfile(store.path):
                with open(store.path, "r", encoding="ascii") as handle:
                    raw = handle.read()
                self.assertNotIn(secret, raw)
            store.clear()
            self.assertEqual(store.load(), "")

    def test_the_description_never_contains_the_key(self):
        with tempfile.TemporaryDirectory() as folder:
            store = A.KeyStore(os.path.join(folder, "provider.key"))
            store.save("sk-secret-value")
            self.assertNotIn("sk-secret-value", store.describe())


class _FakeDb:
    """The smallest stand-in for Snapshot that the prompt needs."""

    attached = False
    read_only = False
    live: dict = {}

    def meta(self):
        return {"game_date": "2026-07-20", "fm_build": "2494687",
                "game_version": "27.1.0+2505117"}

    def counts(self):
        return {"players": 42344, "staff": 23309, "clubs": 42129,
                "competitions": 6625}

    def game_date(self, meta=None):
        return "2026-07-20"

    def club_by_name(self, name):
        return {"name": "Milan", "competition": "Serie A Enilive",
                "reputation": 8300, "balance": 50861508,
                "transfer_budget": 0}

    def squad(self, club, limit=8):
        return [{"name": "Christian Pulisic", "age": 27, "ca": 158, "pa": 163,
                 "condition": 9417, "morale": 14, "injured": 0}]

    def injured_players(self, limit=6):
        return [{"name": "Mike Maignan", "club": "Milan", "injury": "Hamstring"}]


if __name__ == "__main__":
    unittest.main()

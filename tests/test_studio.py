"""Tests for FM27 Studio backend, HTTP/SSE server, and assistant event streaming."""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from fmrte import assistant as A
from fmrte.db import Snapshot
from fmrte.studio_api import StudioBackend, StudioServer


def _http_json(url: str, *, method: str = "GET", token: str | None = None,
               body: dict | None = None) -> tuple[int, dict]:
    headers = {}
    if token is not None:
        headers["X-Studio-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(payload)
        except Exception:
            return exc.code, {"raw": payload}


def _http_sse(url: str, *, token: str, body: dict) -> tuple[int, list[dict]]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "X-Studio-Token": token,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=10) as resp:
        status = resp.status
        raw = resp.read().decode("utf-8")
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return status, events


class AssistantEventStreamTests(unittest.TestCase):
    """Verify reasoning extraction and on_event callbacks in chat_with_tools."""

    def test_on_event_emits_thinking_and_tools_and_strips_think_tags(self) -> None:
        db = Snapshot()
        try:
            events: list[dict] = []
            responses = iter([
                {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "Need to inspect Pulisic first.",
                            "content": "",
                            "tool_calls": [{
                                "id": "tc_1",
                                "type": "function",
                                "function": {
                                    "name": "fm27_player_detail",
                                    "arguments": json.dumps({"player_unique_id": 92074020}),
                                },
                            }],
                        }
                    }]
                },
                {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "<think>Final synthesis.</think>Pulisic is ready.",
                        }
                    }]
                },
            ])

            def fake_http(*_args, **_kwargs):
                return next(responses)

            settings = A.AssistantSettings(
                provider="openai",
                base_url="http://example.invalid/v1",
                model="gpt-5",
                enabled=True,
            )
            with mock.patch.object(A, "_http_chat", side_effect=fake_http):
                reply = A.chat_with_tools(
                    settings, "fake-key", db, "Check Pulisic",
                    on_event=events.append,
                )

            self.assertEqual(reply, "Pulisic is ready.")
            kinds = [e["type"] for e in events]
            self.assertIn("thinking", kinds)
            self.assertIn("tool_start", kinds)
            self.assertIn("tool_done", kinds)
            self.assertIn("step", kinds)
            thinking_texts = [e["text"] for e in events if e["type"] == "thinking"]
            self.assertIn("Need to inspect Pulisic first.", thinking_texts)
            self.assertIn("Final synthesis.", thinking_texts)
            done_evt = next(e for e in events if e["type"] == "tool_done")
            self.assertEqual(done_evt["name"], "fm27_player_detail")
            self.assertTrue(done_evt["ok"])
        finally:
            db.close()


class StudioServerTests(unittest.TestCase):
    """End-to-end tests against a real loopback StudioServer (offline snapshot)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = Snapshot()
        cls.backend = StudioBackend(cls.db)
        cls.server = StudioServer(cls.backend, host="127.0.0.1", port=0)
        cls.url = cls.server.start()
        cls.base = f"http://{cls.server.host}:{cls.server.port}"
        cls.token = cls.server.token

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.db.close()

    def tearDown(self) -> None:
        self.db.reset_edits()

    def test_static_assets_and_token_injection(self) -> None:
        self.assertIn(f"?token={self.token}", self.url)
        with urllib.request.urlopen(self.url, timeout=5) as resp:
            html = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
            self.assertIn("FM27 Studio", html)
        with urllib.request.urlopen(f"{self.base}/studio.css", timeout=5) as resp:
            css = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
            self.assertIn("--lime", css)
        with urllib.request.urlopen(f"{self.base}/studio.js", timeout=5) as resp:
            js = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
            self.assertIn("/api/bootstrap", js)

    def test_api_requires_token(self) -> None:
        code, body = _http_json(f"{self.base}/api/status")
        self.assertEqual(code, 401)
        self.assertIn("error", body)

        code2, body2 = _http_json(f"{self.base}/api/status", token=self.token)
        self.assertEqual(code2, 200)
        self.assertIn("game_date", body2)
        self.assertIn("counts", body2)

    def test_bootstrap_and_entities(self) -> None:
        code, boot = _http_json(f"{self.base}/api/bootstrap", token=self.token)
        self.assertEqual(code, 200)
        self.assertGreater(boot["status"]["counts"]["players"], 1000)
        self.assertTrue(boot["default_players"])

        # Mbappé display name fix preserved through Studio API
        code, mbappe = _http_json(f"{self.base}/api/player/85139014", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("Mbappé", mbappe["name"])
        self.assertIn("Technical", mbappe["attributes"])
        self.assertTrue(mbappe["positions"])

        # Club view (Milan)
        code, club = _http_json(f"{self.base}/api/club/5598", token=self.token)
        self.assertEqual(code, 200)
        self.assertGreater(club["squad_count"], 10)
        self.assertTrue(club["staff"])

        # Staff view
        staff_uid = club["staff"][0]["unique_id"]
        code, staff = _http_json(f"{self.base}/api/staff/{staff_uid}", token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(staff["unique_id"], staff_uid)

        # Nation view (use Mbappé's real nationality from the snapshot)
        nat = urllib.parse.quote(mbappe["nationality"], safe="")
        code, nation = _http_json(f"{self.base}/api/nation/{nat}", token=self.token)
        self.assertEqual(code, 200)
        self.assertGreater(nation["counts"]["players"], 10)

    def test_edit_preset_undo_redo_and_compare(self) -> None:
        uid = 92074020
        code, res = _http_json(
            f"{self.base}/api/player/{uid}/edit",
            method="POST", token=self.token,
            body={"column": "finishing", "value": 19},
        )
        self.assertEqual(code, 200)
        self.assertTrue(res["ok"])
        self.assertEqual(res["applied"], 19)

        # Inspire preset sends morale=100 (clamped to 20 offline, 100 in live FM27)
        # and jadedness=0. Verify both the offline clamp and that morale=100 is requested.
        calls: list[tuple[str, int]] = []
        orig_set = self.db.set_value

        def spy_set(table, row, col, val, *a, **kw):
            calls.append((col, val))
            return orig_set(table, row, col, val, *a, **kw)

        with mock.patch.object(self.db, "set_value", side_effect=spy_set):
            code, insp = _http_json(
                f"{self.base}/api/player/{uid}/preset",
                method="POST", token=self.token,
                body={"preset": "inspire"},
            )
        self.assertEqual(code, 200)
        self.assertTrue(insp["ok"])
        self.assertIn(("morale", 100), calls)
        self.assertIn(("jadedness", 0), calls)
        self.assertEqual(insp["player"]["morale"], 20)
        self.assertEqual(insp["player"]["jadedness"], 0)

        # History + Undo + Redo
        code, hist = _http_json(f"{self.base}/api/history", token=self.token)
        self.assertEqual(code, 200)
        self.assertGreaterEqual(len(hist["entries"]), 2)

        code, undo_res = _http_json(f"{self.base}/api/undo", method="POST", token=self.token, body={})
        self.assertEqual(code, 200)
        self.assertTrue(undo_res["ok"])

        code, redo_res = _http_json(f"{self.base}/api/redo", method="POST", token=self.token, body={})
        self.assertEqual(code, 200)
        self.assertTrue(redo_res["ok"])

        # Compare two players (radar + overview + detailed rows)
        code, cmp_res = _http_json(
            f"{self.base}/api/player/{uid}/compare?other=85139014",
            token=self.token,
        )
        self.assertEqual(code, 200)
        self.assertEqual(cmp_res["a"]["unique_id"], uid)
        self.assertEqual(cmp_res["b"]["unique_id"], 85139014)
        self.assertEqual(len(cmp_res["radar"]), 8)
        self.assertIn("rating_diff", cmp_res["overview"])
        self.assertIn("ca_diff", cmp_res["overview"])
        self.assertIn("value_diff", cmp_res["overview"])
        self.assertIn("wage_diff", cmp_res["overview"])
        self.assertGreater(len(cmp_res["rows"]), 20)

        # Save endpoint clears dirty count on staged edits
        code, save_res = _http_json(
            f"{self.base}/api/save",
            method="POST", token=self.token, body={},
        )
        self.assertEqual(code, 200)
        self.assertTrue(save_res["ok"])
        self.assertEqual(save_res["status"]["dirty"], 0)

    def test_fuzzy_and_diacritic_search_and_avatars_and_ua(self) -> None:
        # 1) Diacritic-insensitive ("Modric" -> "Luka Modrić") & typo-tolerant ("Modrics")
        for query in ("Modric", "Modrics", "Luka Modrics"):
            code, res = _http_json(
                f"{self.base}/api/search?scope=players&q={urllib.parse.quote(query)}&limit=10",
                token=self.token,
            )
            self.assertEqual(code, 200, query)
            names = [r["name"] for r in res["rows"]]
            self.assertTrue(
                any("Modri" in n for n in names),
                f"Expected Luka Modrić for query {query!r}, got {names}",
            )

        # 2) Avatar endpoint: real facepack returns PNG if installed; unknown UID returns 404
        if self.backend.faces.available and self.backend.faces.resolve(85139014):
            req = urllib.request.Request(
                f"{self.base}/api/avatar/player/85139014?token={self.token}",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.headers.get("Content-Type", "").startswith("image/"))
                self.assertGreater(len(resp.read()), 100)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                f"{self.base}/api/avatar/player/999999999?token={self.token}", timeout=5
            )
        self.assertEqual(ctx.exception.code, 404)

        # 3) HTML hides top tab bar and swaps dockTransfers for dockSave
        with urllib.request.urlopen(self.url, timeout=5) as resp:
            html = resp.read().decode("utf-8")
            self.assertIn('id="dockSave"', html)
            self.assertNotIn('id="dockTransfers"', html)
            self.assertNotIn('id="tabBar"', html)

        # 4) _http_chat sets explicit User-Agent header (prevents Cloudflare 1010)
        captured: dict[str, str] = {}

        class _FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *_a):
                return False
            def read(self, _n=-1):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

        def fake_urlopen(req, timeout=10):
            captured.update({k.lower(): v for k, v in req.header_items()})
            return _FakeResp()

        settings = A.AssistantSettings(
            provider="commandcode",
            base_url=A.DEFAULT_BASE_URL,
            model=A.DEFAULT_MODEL,
        )
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            A._http_chat(settings, "sk-fake", [{"role": "user", "content": "hi"}])
        self.assertIn("user-agent", captured)
        self.assertNotIn("python-urllib", captured["user-agent"].lower())

    def test_providers_fetch_models_and_reload_pruning(self) -> None:
        # 1) Popular providers present in /api/ai/settings
        code, ai = _http_json(f"{self.base}/api/ai/settings", token=self.token)
        self.assertEqual(code, 200)
        pids = {p["id"] for p in ai["providers"]}
        for expected in ("commandcode", "deepseek", "opencode_go", "claude_code", "gemini", "openrouter", "openai"):
            self.assertIn(expected, pids)

        # 2) Fetch models for HTTP provider (mocked) & CLI provider
        fake_catalog = {"data": [{"id": "deepseek-reasoner"}, {"id": "deepseek-chat"}]}

        class _FakeModelsResp:
            def __enter__(self):
                return self
            def __exit__(self, *_a):
                return False
            def read(self, _n=-1):
                return json.dumps(fake_catalog).encode("utf-8")

        orig_urlopen = urllib.request.urlopen

        def fake_models_urlopen(req, *args, **kwargs):
            url_str = req.full_url if hasattr(req, "full_url") else str(req)
            if "127.0.0.1" in url_str:
                return orig_urlopen(req, *args, **kwargs)
            return _FakeModelsResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_models_urlopen):
            code, mres = _http_json(
                f"{self.base}/api/ai/models",
                method="POST", token=self.token,
                body={"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-test"},
            )
        self.assertEqual(code, 200)
        self.assertTrue(mres["ok"])
        self.assertEqual([m["id"] for m in mres["models"]], ["deepseek-chat", "deepseek-reasoner"])

        code, cli_res = _http_json(
            f"{self.base}/api/ai/models",
            method="POST", token=self.token,
            body={"provider": "claude_code"},
        )
        self.assertEqual(code, 200)
        self.assertTrue(cli_res["ok"])
        self.assertEqual(cli_res["models"][0]["id"], "claude")

        # 3) Reload cache lives inside saves/reloads/ and prunes cleanly
        import os
        rel_dir = Snapshot.create_reload_dir(prefix="unit-test-")
        self.assertTrue(os.path.isdir(rel_dir))
        self.assertIn(os.path.join("saves", "reloads"), rel_dir)
        code, prune = _http_json(
            f"{self.base}/api/saves/prune",
            method="POST", token=self.token,
            body={"max_age_hours": 0, "keep_latest": 0},
        )
        self.assertEqual(code, 200)
        self.assertTrue(prune["ok"])
        self.assertFalse(os.path.exists(rel_dir))

    def test_steam_unregistered_denuvo_build_manager_and_date_pairing(self) -> None:
        """A Steam 27.1.x image found by scan_table resolves date & manager via paired deltas."""
        from fmrte import fmmemory as FM
        from fmrte.procmem import InMemoryProcessMemory

        base = 0x7FF0_0000_0000
        # Simulate a Steam 27.1.x Denuvo table at an unregistered RVA (0x5912340)
        table_rva = 0x5912340
        mem = InMemoryProcessMemory(
            pid=4321,
            module_base=base,
            module_size=0x1790_0000,
            can_write=True,
        )
        mem.exe = r"C:\Program Files (x86)\Steam\steamapps\common\Football Manager 27\fm.exe"
        fm = FM.FmMemory(mem)
        fm.table_base = base + table_rva
        fm.table_rva = table_rva
        fm.scanned = True
        date_cands = fm._date_candidates(2505117)
        mgr_cands = fm._manager_candidates(2505117)
        self.assertEqual(date_cands[0], fm.table_base - FM.TABLE_TO_CURRENT_DATE_RVA)
        self.assertIn(fm.table_base - (0x53faca0 - 0x53eb138), mgr_cands[:2])
        self.assertGreaterEqual(FM.MAX_SCAN_SPAN, 0x0A00_0000)

    def test_builtin_tactics_s24_training_and_online_tactic_parser(self) -> None:
        import os
        import shutil
        import tempfile
        from fmrte import fmplans, fmtools, tacticfiles, tacticweb

        # 1) Built-in templates work with NO external FMRTE_GAME_DIR
        with mock.patch.object(tacticfiles, "game_data_directory", return_value=""):
            built = fmplans.build_tactic_from_squad(self.db, "Milan", template_name="4-2-3-1", style="Balanced")
            self.assertNotIn("error", built)
            self.assertEqual(len(built["roles"]), 11)
            self.assertIn("4-2-3-1", built["template"])
            auto_built = fmplans.build_tactic_from_squad(self.db, "Milan", template_name="", style="Balanced")
            self.assertNotIn("error", auto_built)
            self.assertEqual(len(auto_built["roles"]), 11)

        # 2) Pressing training schedule follows Match Lab S24 (no wasted 4/6 slots)
        press_week = fmplans.training_week("Pressing", match_day="Saturday")
        ids = {s["id"] for d in press_week["days"] for s in d["sessions"]}
        self.assertTrue({2, 5, 7}.issubset(ids))
        self.assertNotIn(4, ids)
        self.assertNotIn(6, ids)

        # 3) FM-Arena & Sortitoutsi HTML parsers + safe .fmf installer
        sample_arena = (
            '<tr id="fmt-tactic-2227">'
            '<td><span class="fmtCrown"></span><a href="/threads/test-tactic.336/">Test 4-2-3-1</a></td>'
            '<td><div title="In possession: GK, DL, DCL, DCR, DR, DMCL, DMCR, AML, AMC, AMR, STC"></div></td>'
            '<td>alice</td><td>1,000</td><td>78 / 114</td><td>+40</td><td>90</td><td>50</td>'
            '<td>80</td><td>45</td><td>+35</td></tr>'
        )
        parsed_arena = tacticweb.parse_fm_arena_html(sample_arena)
        self.assertEqual(len(parsed_arena), 1)
        self.assertEqual(parsed_arena[0]["name"], "Test 4-2-3-1")
        self.assertEqual(parsed_arena[0]["formation"], "4-2-3-1")
        self.assertTrue(parsed_arena[0]["direct_download"])

        sample_sorti = (
            '<tr><th><a href="https://sortitoutsi.net/content/78712/meta-4-2-3-1">Meta 4-2-3-1</a>'
            '<div>By <a href="#">bob</a> 1,200 Downloads</div></th>'
            '<td>2</td><td>71%</td><td>2.35</td><td>80</td><td>30</td><td>+50</td></tr>'
        )
        parsed_sorti = tacticweb.parse_sortitoutsi_html(sample_sorti)
        self.assertEqual(len(parsed_sorti), 1)
        self.assertEqual(parsed_sorti[0]["formation"], "4-2-3-1")
        self.assertFalse(parsed_sorti[0]["direct_download"])

        # 4) Mocked download saves valid .fmf binary to target folder
        fake_fmf = b"\x02\x01afe.\x08\x00" + b"\x00" * 64
        tmp_dir = tempfile.mkdtemp(prefix="fm27_tactic_dl_")
        try:
            def fake_get(url, **_kw):
                if "/attachments/" in url:
                    return fake_fmf, {"content-disposition": 'attachment; filename="Meta 4231.fmf"'}
                return b'<a href="/attachments/meta-4231-fmf.999/">dl</a>', {}

            with mock.patch.object(tacticweb, "_http_get", side_effect=fake_get):
                dl = tacticweb.download_fm_arena_tactic("https://fm-arena.com/threads/meta.1/", target_dir=tmp_dir)
            self.assertTrue(dl["ok"], dl)
            self.assertTrue(dl["valid_fmf_header"])
            self.assertTrue(os.path.isfile(dl["path"]))
            # AI tool schema exposes online tactic tools
            tool_names = {t["function"]["name"] for t in fmtools.tool_definitions()}
            self.assertIn("fm27_search_online_tactics", tool_names)
            self.assertIn("fm27_download_tactic", tool_names)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_search_transfers_retirements_and_plans(self) -> None:
        for scope in ("players", "clubs", "staff", "nations"):
            code, res = _http_json(f"{self.base}/api/search?scope={scope}&limit=10", token=self.token)
            self.assertEqual(code, 200, scope)
            self.assertGreater(res["count"], 0, scope)

        code, tr = _http_json(f"{self.base}/api/transfers?mode=expiring&limit=10", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("rows", tr)

        code, ret = _http_json(f"{self.base}/api/retirements?kind=players&min_age=35&limit=10", token=self.token)
        self.assertEqual(code, 200)
        self.assertGreater(ret["count"], 0)

        # Tactic & Training plans
        code, tactic = _http_json(f"{self.base}/api/plans/tactic?club=Milan&style=Pressing", token=self.token)
        self.assertEqual(code, 200)
        self.assertNotIn("error", tactic)
        self.assertGreaterEqual(len(tactic.get("roles", [])), 10)
        self.assertIn("instructions", tactic)

        code, train = _http_json(f"{self.base}/api/plans/training?focus=Pressing&club=Milan", token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(len(train["days"]), 7)

    def test_ai_chat_sse_local_slash_and_mocked_llm(self) -> None:
        # 1) Local /inspire command runs directly without needing an API key
        status, events = _http_sse(
            f"{self.base}/api/ai/chat",
            token=self.token,
            body={"prompt": "/inspire", "context": {"kind": "player", "id": 92074020}},
        )
        self.assertEqual(status, 200)
        done = [e for e in events if e.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertIn("100", done[0]["text"])
        self.assertEqual(self.backend.get_player(92074020)["morale"], 20)
        self.assertEqual(self.backend.get_player(92074020)["jadedness"], 0)

        # 2) Mocked LLM turn streams thinking + tool_start + tool_done + done
        fake_responses = iter([
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "Inspecting Milan squad.",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "fm27_analyze_squad",
                                "arguments": json.dumps({"club": "Milan"}),
                            },
                        }],
                    }
                }]
            },
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Milan squad analysis complete.",
                    }
                }]
            },
        ])
        with mock.patch.object(self.backend.keys, "load", return_value="sk-test"), \
             mock.patch.object(A, "_http_chat", side_effect=lambda *_a, **_k: next(fake_responses)):
            status2, events2 = _http_sse(
                f"{self.base}/api/ai/chat",
                token=self.token,
                body={"prompt": "/analyze", "context": {"kind": "club", "id": 5598, "club": "Milan"}},
            )
        self.assertEqual(status2, 200)
        kinds = [e.get("type") for e in events2]
        self.assertIn("thinking", kinds)
        self.assertIn("tool_start", kinds)
        self.assertIn("tool_done", kinds)
        self.assertIn("done", kinds)
        final_text = next(e["text"] for e in events2 if e.get("type") == "done")
        self.assertEqual(final_text, "Milan squad analysis complete.")


if __name__ == "__main__":
    unittest.main()

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
        cls.backend.close()
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
            self.assertIn("fm27_lab_status", tool_names)
            self.assertIn("fm27_lab_run_ab_test", tool_names)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_chat_multimodal_images_fmf_attachments_reasoning_and_lab_summary(self) -> None:
        import base64
        from fmrte import tacticslab

        # 1) Reasoning effort forwarded in _http_chat & multimodal image array in chat_with_tools
        sent_bodies: list[dict] = []

        class _FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *_a):
                return False
            def read(self, _n=-1):
                return json.dumps({"choices": [{"message": {"content": "Looks good!"}}]}).encode("utf-8")

        def fake_urlopen(req, timeout=10):
            sent_bodies.append(json.loads(req.data.decode("utf-8")))
            return _FakeResp()

        settings = A.AssistantSettings(
            provider="openai", base_url="http://example.invalid/v1",
            model="gpt-5", reasoning="High", enabled=True,
        )
        tiny_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            reply = A.chat_with_tools(
                settings, "sk-test", self.db, "Check this tactic screenshot",
                user_images=[{"name": "tactic.png", "mime": "image/png", "data_url": tiny_png}],
            )
        self.assertEqual(reply, "Looks good!")
        self.assertEqual(sent_bodies[0].get("reasoning_effort"), "high")
        user_content = sent_bodies[0]["messages"][-1]["content"]
        self.assertIsInstance(user_content, list)
        self.assertEqual(user_content[1]["type"], "image_url")
        self.assertEqual(user_content[1]["image_url"]["url"], tiny_png)

        # 2) .fmf attachment saved & annotated in _prepare_chat_attachments
        fake_fmf = b"\x02\x01afe.\x08\x00" + b"\x00" * 40
        imgs, notes = self.backend._prepare_chat_attachments([
            {"kind": "image", "name": "shot.png", "mime": "image/png", "data_url": tiny_png},
            {"kind": "fmf", "name": "My 4-2-3-1.fmf", "base64": base64.b64encode(fake_fmf).decode("ascii")},
        ])
        self.assertEqual(len(imgs), 1)
        self.assertIn("My 4-2-3-1.fmf", notes)
        self.assertIn("fm27_lab_run_ab_test", notes)

        # 3) Tactics Lab job summary computes A/B winner, verified count & validation_scope
        summary = tacticslab._summarize_job({
            "id": "job1", "name": "AB test", "status": "completed", "mode": "comparison",
            "completed": 4, "total": 4,
            "tactic_snapshots": {"A": {"tactic_name": "Old 4-4-2"}, "B": {"tactic_name": "Meta 4-2-3-1"}},
            "runs": [
                {"variant": "A_0_H", "verified": True, "validation_scope": "native_match_score_and_team_statistics",
                 "result": {"score": [1, 1]}, "statistics": {"teams": [{"xg": 1.2}, {"xg": 1.1}]}},
                {"variant": "A_0_A", "verified": True, "validation_scope": "native_match_score_and_team_statistics",
                 "result": {"score": [2, 0]}, "statistics": {"teams": [{"xg": 1.8}, {"xg": 0.6}]}},
                {"variant": "B_0_H", "verified": True, "validation_scope": "native_match_score_and_team_statistics",
                 "result": {"score": [3, 0]}, "statistics": {"teams": [{"xg": 2.4}, {"xg": 0.5}]}},
                {"variant": "B_0_A", "verified": True, "validation_scope": "native_match_score_and_team_statistics",
                 "result": {"score": [1, 2]}, "statistics": {"teams": [{"xg": 0.9}, {"xg": 1.9}]}},
            ],
        })
        self.assertEqual(summary["verified_matches"], 4)
        self.assertIn("native_match_score_and_team_statistics", summary["validation_scope"])
        self.assertEqual(summary["groups"]["B"]["ppg"], 3.0)
        self.assertEqual(summary["groups"]["A"]["ppg"], 0.5)
        self.assertIn("Meta 4-2-3-1", summary["winner"])

        # 4) /api/lab/status responds cleanly (installed or missing-engine prompt with repo_url)
        code, lab_st = _http_json(f"{self.base}/api/lab/status", token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(lab_st["repo_url"], tacticslab.LAB_REPO_URL)

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

    def test_ai_direct_lab_progress_compare_and_mcp_server(self) -> None:
        """AI tools directly drive Tactics Lab with live progress, plus stdlib MCP stdio server."""
        import fm27_mcp_server
        from fmrte import fmtools

        # 1) Fake Lab client verifies auto-save load + progress streaming + multi-tactic ranking
        class _FakeLab:
            installed = True
            loaded_save_id = ""
            loaded_club_name = ""
            base_scenario = None
            opponents_cache: list = []

            def __init__(self):
                self._jobs: dict[str, dict] = {}
                self._seq = 0

            def ensure_save_loaded(self, club_name_hint="", save_path="", tactic_index=0,
                                   exclude_player_uids=None, on_progress=None):
                if on_progress:
                    on_progress("Importing Milan.fm into native engine…")
                self.loaded_save_id = "save_1"
                self.loaded_club_name = club_name_hint or "Milan"
                self.base_scenario = {"teams": [{"tactic_name": "Save 4-2-3-1"}]}
                self.opponents_cache = [{"key": "opp1", "club_uid": 1001}]
                return {"ok": True, "club_name": self.loaded_club_name, "save_path": "Milan.fm", "opponents_count": 1}

            def create_custom_tactic(self, *, name, formation="4-2-3-1", mentality="Balanced",
                                     roles=None, instructions=None, save_to_tactics_folder=True):
                return {"ok": True, "tactic_name": name, "formation": formation,
                        "filename": f"{name}.fmf", "path": f"/tmp/{name}.fmf", "bytes": 1200}

            def start_ab_test(self, *, tactic_b, tactic_a=None, mode="league", **_kw):
                self._seq += 1
                jid = f"job_{self._seq}"
                b_name = tactic_b.get("path") or tactic_b.get("url") or "Tactic B"
                a_name = (tactic_a or {}).get("path") or "Save 4-2-3-1"
                b_ppg = 2.5 if "fast" in str(b_name).lower() else 1.5
                self._jobs[jid] = {
                    "id": jid, "name": f"AB {a_name} vs {b_name}", "status": "complete",
                    "completed": 4, "total": 4, "verified_matches": 4,
                    "validation_scope": ["native_match_score_and_team_statistics"],
                    "groups": {
                        "A": {"label": "A", "tactic_name": a_name, "matches": 2, "verified": 2,
                              "wins": 1, "draws": 0, "losses": 1, "pts": 3, "ppg": 1.5,
                              "gf": 2, "ga": 2, "gd": 0, "xgf": 2.1, "xga": 2.0, "xgd": 0.1},
                        "B": {"label": "B", "tactic_name": b_name, "matches": 2, "verified": 2,
                              "wins": 2 if b_ppg > 2 else 1, "draws": 0, "losses": 0 if b_ppg > 2 else 1,
                              "pts": 6 if b_ppg > 2 else 3, "ppg": b_ppg,
                              "gf": 4 if b_ppg > 2 else 2, "ga": 1 if b_ppg > 2 else 2,
                              "gd": 3 if b_ppg > 2 else 0, "xgf": 3.4, "xga": 1.2, "xgd": 2.2},
                    },
                    "winner": f"B ({b_name})" if b_ppg > 1.5 else "Tie",
                }
                return {"ok": True, "mode": "league", "job_id": jid, "total": 4, "tactic_a": a_name, "tactic_b": b_name}

            def wait_for_job(self, job_id, *, timeout_seconds=180.0, poll_interval=1.5, on_progress=None):
                if on_progress:
                    on_progress("Match Lab 2/4 matches · simulating")
                    on_progress("Match Lab 4/4 matches · complete")
                return {"done": True, "timed_out": False, "job": self._jobs[job_id]}

            def compare_tactics(self, candidates, *, mode="league", club_name_hint="",
                                opponent_count=4, matches_per_opponent=2,
                                timeout_per_job=180.0, on_progress=None):
                from fmrte.tacticslab import TacticsLabClient
                return TacticsLabClient.compare_tactics(
                    self, candidates, mode=mode, club_name_hint=club_name_hint,
                    opponent_count=opponent_count, matches_per_opponent=matches_per_opponent,
                    timeout_per_job=timeout_per_job, on_progress=on_progress)

            def status(self):
                return {"installed": True, "can_simulate": True, "compatible": True}

            def list_jobs(self):
                return list(self._jobs.values())

            def stop(self):
                pass

        fake_lab = _FakeLab()
        progress_log: list[str] = []
        orig_lab = fmtools._SHARED_LAB_CLIENT
        try:
            fmtools.set_lab_client(fake_lab)
            res = fmtools.execute_tool(
                self.db, "fm27_lab_run_ab_test",
                {"tactic_a_kind": "save", "tactic_b_kind": "local_fmf", "tactic_b_ref": "FastPress.fmf",
                 "mode": "league", "wait": True},
                default_club="Milan",
                on_progress=progress_log.append,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["result"]["verified_matches"], 4)
            self.assertIn("FastPress.fmf", res["result"]["winner"])
            self.assertTrue(any("Match Lab" in m for m in progress_log))

            # Multi-tactic ranking tournament
            rank_res = fmtools.execute_tool(
                self.db, "fm27_lab_compare_tactics",
                {"tactics": [
                    {"kind": "save", "ref": "0"},
                    {"kind": "local_fmf", "ref": "Slow.fmf"},
                    {"kind": "local_fmf", "ref": "FastPress.fmf"},
                ], "mode": "league"},
                default_club="Milan",
            )
            self.assertTrue(rank_res["ok"])
            self.assertEqual(len(rank_res["ranking"]), 3)
            self.assertEqual(rank_res["best_tactic"]["tactic_name"], "FastPress.fmf")

            # Custom native tactic builder tool
            created = fmtools.execute_tool(
                self.db, "fm27_lab_create_tactic",
                {"name": "Milan 4231 Press", "formation": "4-2-3-1", "mentality": "Balanced",
                 "instructions": {"PressingTrap": "Standard", "Tempo": "Higher"}},
                default_club="Milan",
            )
            self.assertTrue(created["ok"])
            self.assertEqual(created["filename"], "Milan 4231 Press.fmf")

            # 2) Pure-stdlib MCP server JSON-RPC 2.0 round-trip
            mcp = fm27_mcp_server.FM27McpServer(db=self.db, lab=fake_lab)
            init_r = mcp.handle_message({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            })
            self.assertEqual(init_r["result"]["serverInfo"]["name"], "fm27-studio")
            list_r = mcp.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            mcp_names = {t["name"] for t in list_r["result"]["tools"]}
            self.assertIn("fm27_lab_run_ab_test", mcp_names)
            self.assertIn("fm27_lab_compare_tactics", mcp_names)
            self.assertIn("fm27_lab_list_local", mcp_names)
            call_r = mcp.handle_message({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "fm27_lab_status", "arguments": {}},
            })
            self.assertFalse(call_r["result"]["isError"])
            parsed = json.loads(call_r["result"]["content"][0]["text"])
            self.assertTrue(parsed["can_simulate"])
        finally:
            fmtools.set_lab_client(orig_lab)

    def test_club_squad_includes_loaned_in_rating_value_and_positional_sort(self) -> None:
        """Club squad includes loaned-in players (e.g. Hutchinson), loan status tags, rating, value & tactical pos_rank."""
        code, club = _http_json(f"{self.base}/api/club/Milan", token=self.token)
        self.assertEqual(code, 200)
        squad = club["squad"]
        by_uid = {p["unique_id"]: p for p in squad}

        # 1) Loaned-in player Omari Elijah Giraud-Hutchinson is present & tagged loan_status='in'
        self.assertIn(2000047221, by_uid)
        hutch = by_uid[2000047221]
        self.assertEqual(hutch["loan_status"], "in")
        self.assertEqual(hutch["loan_from"], "Nottm Forest")
        self.assertGreater(club["loan_in_count"], 0)

        # 2) Loaned-out Milan player (Nkunku) is tagged loan_status='out'
        self.assertIn(85140301, by_uid)
        nkunku = by_uid[85140301]
        self.assertEqual(nkunku["loan_status"], "out")
        self.assertEqual(nkunku["loan_to"], "RB Leipzig")
        self.assertGreater(club["loan_out_count"], 0)

        # 3) Every squad player has computed rating & asking value
        maignan = by_uid[85094520]
        pulisic = by_uid[92074020]
        self.assertIsNotNone(maignan["rating"])
        self.assertGreater(maignan["rating"], 50.0)
        self.assertGreater(pulisic["value_raw"], 0)
        self.assertNotEqual(pulisic["value"], "—")

        # 4) Positional sort rank follows GK < CB < FB/WB < DM < CM < Winger < AM < ST
        from fmrte.studio_api import _position_sort_rank
        gk_r = _position_sort_rank({"goalkeeper": 20})
        cb_r = _position_sort_rank({"defender_central": 20})
        fb_r = _position_sort_rank({"defender_left": 20})
        dm_r = _position_sort_rank({"defensive_midfielder": 20})
        cm_r = _position_sort_rank({"midfielder_central": 20})
        wg_r = _position_sort_rank({"attacking_midfielder_right": 20})
        am_r = _position_sort_rank({"attacking_midfielder_central": 20})
        st_r = _position_sort_rank({"striker": 20})
        self.assertLess(gk_r, cb_r)
        self.assertLess(cb_r, fb_r)
        self.assertLess(fb_r, dm_r)
        self.assertLess(dm_r, cm_r)
        self.assertLess(cm_r, wg_r)
        self.assertLess(wg_r, am_r)
        self.assertLess(am_r, st_r)

        # 5) AI lineup/squad analysis also includes loaned-in Hutchinson as AVAILABLE
        from fmrte import fmroles, fmsquad
        ai_rows = fmsquad.squad_rows(self.db, "Milan", 300)
        ai_hutch = next((r for r in ai_rows if r["unique_id"] == 2000047221), None)
        self.assertIsNotNone(ai_hutch)
        self.assertIsNone(fmroles.unavailable_reason(ai_hutch))

    def test_chat_sessions_persistence_and_staff_attributes_editing(self) -> None:
        """Multi-session chat history (create/select/delete/persist) & Staff attributes + editing."""
        import os
        import tempfile

        # 1) Isolated chat session store: create, append, switch, reload from disk, delete
        with tempfile.TemporaryDirectory(prefix="fm27_chat_") as tmp:
            store_path = os.path.join(tmp, "chat_sessions.json")
            orig_store = self.backend.chat_store
            try:
                from fmrte.studio_api import _ChatSessionStore
                self.backend.chat_store = _ChatSessionStore(store_path)
                self.backend.chat_store.append_turns(
                    {"role": "user", "text": "Analyze Milan"},
                    {"role": "assistant", "text": "Milan looks strong.", "steps": [], "tools": []},
                )
                first_id = self.backend.chat_store.active_id

                code, created = _http_json(
                    f"{self.base}/api/ai/sessions", method="POST", token=self.token,
                    body={"action": "new"},
                )
                self.assertEqual(code, 200)
                self.assertTrue(created["ok"])
                second_id = created["active_id"]
                self.assertNotEqual(first_id, second_id)
                self.assertEqual(len(created["sessions"]), 2)
                self.assertEqual(len(created["active"]["turns"]), 0)

                # Switch back to first session -> restores turns
                code, switched = _http_json(
                    f"{self.base}/api/ai/sessions", method="POST", token=self.token,
                    body={"action": "select", "id": first_id},
                )
                self.assertEqual(code, 200)
                self.assertEqual(switched["active_id"], first_id)
                self.assertEqual(len(switched["active"]["turns"]), 2)

                # Reload from disk proves persistence across restarts
                reloaded = _ChatSessionStore(store_path)
                self.assertEqual(len(reloaded.sessions), 2)
                self.assertEqual(reloaded.active_id, first_id)

                # Delete first session -> switches cleanly to remaining session
                code, deleted = _http_json(
                    f"{self.base}/api/ai/sessions", method="POST", token=self.token,
                    body={"action": "delete", "id": first_id},
                )
                self.assertEqual(code, 200)
                self.assertEqual(deleted["active_id"], second_id)
                self.assertEqual(len(deleted["sessions"]), 1)
            finally:
                self.backend.chat_store = orig_store

        # 2) Staff detail exposes structured attribute groups and supports editing + preset
        code, s = _http_json(f"{self.base}/api/staff/1403539", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("Coaching", s["attributes"])
        self.assertIn("Mental", s["attributes"])
        self.assertIn("Scouting & Medical", s["attributes"])
        self.assertIn("Goalkeeping", s["attributes"])
        self.assertIn("Personality", s["attributes"])

        code, ed = _http_json(
            f"{self.base}/api/staff/1403539/edit", method="POST", token=self.token,
            body={"column": "ca", "value": 188},
        )
        self.assertEqual(code, 200)
        self.assertTrue(ed["ok"])
        self.assertEqual(ed["staff"]["ca"], 188)

        code, ed_attr = _http_json(
            f"{self.base}/api/staff/1403539/edit", method="POST", token=self.token,
            body={"column": "physiotherapy", "value": 20},
        )
        self.assertEqual(code, 200)
        self.assertTrue(ed_attr["ok"])
        med = ed_attr["staff"]["attributes"]["Scouting & Medical"]
        phys = next(a for a in med if a["column"] == "physiotherapy")
        self.assertEqual(phys["value"], 20)

        code, pre = _http_json(
            f"{self.base}/api/staff/1403539/preset", method="POST", token=self.token,
            body={"preset": "max_staff"},
        )
        self.assertEqual(code, 200)
        self.assertTrue(pre["ok"])
        self.assertEqual(pre["staff"]["pa"], 200)

    def test_people_index_o1_lookup_and_club_nation_logo_avatars(self) -> None:
        """FmMemory.find_person caches UID->ptr in O(1), and club/nation logos serve via /api/avatar."""
        import os
        import struct
        import tempfile
        from fmrte import fmmemory as FM
        from fmrte.procmem import InMemoryProcessMemory
        from fmrte.studio_api import _FaceResolver

        # 1) O(1) find_person cache + stale-pointer auto-rebuild
        mem = InMemoryProcessMemory(pid=1, module_base=0x100000, module_size=0x100000)
        p1, p2, p3 = 0x200000, 0x201000, 0x202000
        mem.put(p1 + FM.REL_UNIQUE_ID, struct.pack("<I", 1001))
        mem.put(p2 + FM.REL_UNIQUE_ID, struct.pack("<I", 1002))
        mem.put(p3 + FM.REL_UNIQUE_ID, struct.pack("<I", 1003))
        fm = FM.FmMemory(mem)
        scan_calls = [0]
        ptrs = [p1, p2]

        def fake_ptrs():
            scan_calls[0] += 1
            return list(ptrs)

        fm.people_pointers = fake_ptrs  # type: ignore[method-assign]
        self.assertEqual(fm.find_person(1001), p1)
        self.assertEqual(fm.find_person(1002), p2)
        self.assertIsNone(fm.find_person(9999))
        self.assertEqual(scan_calls[0], 1, "Second lookup must hit O(1) index without rescanning vector")

        # Simulate save reload moving 1002 from p2 to p3
        mem.put(p2 + FM.REL_UNIQUE_ID, struct.pack("<I", 0))
        mem.put(p3 + FM.REL_UNIQUE_ID, struct.pack("<I", 1002))
        ptrs[:] = [p1, p3]
        self.assertEqual(fm.find_person(1002), p3)
        self.assertEqual(scan_calls[0], 2, "Stale pointer must trigger exactly one index rebuild")

        # 2) Club & nation logo resolution via TCM-style config.xml + /api/avatar endpoints
        tiny_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        with tempfile.TemporaryDirectory(prefix="fm27_gfx_") as tmp:
            club_dir = os.path.join(tmp, "Europe", "Italy", "Clubs")
            nat_dir = os.path.join(tmp, "Others", "Federations")
            os.makedirs(club_dir, exist_ok=True)
            os.makedirs(nat_dir, exist_ok=True)
            with open(os.path.join(club_dir, "TCM1_1099.png"), "wb") as f:
                f.write(tiny_png)
            with open(os.path.join(club_dir, "config.xml"), "w", encoding="utf-8") as f:
                f.write('<record><list id="maps"><record from="TCM1_1099" to="graphics/pictures/club/1099/logo"/></list></record>')
            with open(os.path.join(nat_dir, "TCM4_776.png"), "wb") as f:
                f.write(tiny_png)
            with open(os.path.join(nat_dir, "config.xml"), "w", encoding="utf-8") as f:
                f.write('<record><list id="maps"><record from="TCM4_776" to="graphics/pictures/nation/776/logo"/></list></record>')

            orig_faces = self.backend.faces
            try:
                resolver = _FaceResolver(tmp)
                resolver._club_uids["milan"] = 1099
                resolver._nation_uids["意大利"] = 776
                self.backend.faces = resolver

                milan = self.db.club_by_name("Milan")
                self.assertIsNotNone(milan)
                for path_part in (
                    f"club/{milan['id']}",
                    "club/AC%20Milan",
                    f"nation/{urllib.parse.quote('意大利')}",
                ):
                    with urllib.request.urlopen(
                        f"{self.base}/api/avatar/{path_part}?token={self.token}", timeout=5
                    ) as resp:
                        self.assertEqual(resp.status, 200, path_part)
                        self.assertTrue(resp.headers.get("Content-Type", "").startswith("image/"))
                        self.assertEqual(resp.read(), tiny_png)
            finally:
                self.backend.faces = orig_faces

    def test_live_injury_vector_and_full_live_overlay_for_ui_and_ai(self) -> None:
        """Empty FM27 injury vector reads as fit; live injuries/contracts/loans/budgets flow to UI & AI."""
        import struct
        from fmrte import fmmemory as FM, fmtools, memmap as M
        from fmrte.procmem import InMemoryProcessMemory

        # 1) read_injury: empty std::vector (begin == end) -> fit; non-empty vector -> active injury
        mem = InMemoryProcessMemory(pid=1, module_base=0x100000, module_size=0x100000)
        fm = FM.FmMemory(mem)
        # Empty vector (recovered player like Pulisic/Modric)
        empty_ref = 0x300000
        mem.put(empty_ref, struct.pack("<QQQ", 0x310000, 0x310000, 0x310008))
        self.assertFalse(fm.read_injury(empty_ref).injured)

        # Active vector: begin -> [item_ptr], end = begin + 8, indirect UTF-8 injury name
        act_ref = 0x320000
        vec_buf = 0x330000
        item_addr = 0x340000
        type_addr = 0x350000
        str_addr = 0x360000
        mem.put(act_ref, struct.pack("<QQQ", vec_buf, vec_buf + 8, vec_buf + 8))
        mem.put(vec_buf, struct.pack("<Q", item_addr))
        item = bytearray(0x40)
        struct.pack_into("<Q", item, M.INJURY_TYPE_REL, type_addr)
        struct.pack_into("<HH", item, M.INJURY_DAY_REL, 360, 2026)  # 2026-12-26
        struct.pack_into("<HH", item, M.INJURY_FULL_TRAINING_REL, 16, 4)
        mem.put(item_addr, bytes(item))
        mem.put(type_addr + M.INJURY_TYPE_NAME_REL, struct.pack("<Q", str_addr))
        name_bytes = "腹股沟拉伤".encode("utf-8")
        mem.put(str_addr, struct.pack("<i", len(name_bytes)) + name_bytes + b"\x00")
        inj = fm.read_injury(act_ref)
        self.assertTrue(inj.injured)
        self.assertEqual(inj.name, "腹股沟拉伤")
        self.assertEqual(inj.start_date, "2026-12-26")
        self.assertEqual(inj.light_training_days, 4)
        self.assertEqual(inj.full_training_days, 16)

        # 2) Full live overlay (recovered Pulisic + renewed contract, loaned Gusto, Milan budget)
        orig_live = dict(self.db.live)
        orig_club_live = dict(self.db.club_live)
        try:
            self.db.apply_live(
                [
                    FM.LivePlayer(
                        unique_id=92074020, ca=158, pa=158,
                        condition=10000, sharpness=8653, jadedness=0, morale=90,
                        home_reputation=8000, current_reputation=8000, world_reputation=8000,
                        contract_end_date="2031-06-30",
                        injured=0, injury="", injury_start_date="",
                        injury_light_training_days_remaining="",
                        injury_full_training_days_remaining="",
                        injury_expected_return="",
                    ),
                    FM.LivePlayer(
                        unique_id=49056280, ca=145, pa=155,
                        condition=9800, sharpness=9000, jadedness=0, morale=85,
                        home_reputation=7000, current_reputation=7000, world_reputation=7000,
                        club="Chelsea", playing_club="West Ham", loan_club="West Ham",
                        is_loaned_out="yes", listed_for_loan=0,
                    ),
                ],
                club_finances={
                    "milan": {"balance": 35638667, "transfer_budget": 67942576, "payroll_budget": 2229752},
                },
            )
            # Pulisic is fit and renewed in both Search API and AI tools
            code, srch = _http_json(f"{self.base}/api/search?scope=players&q=Pulisic&limit=5", token=self.token)
            self.assertEqual(code, 200)
            pul = next(r for r in srch["rows"] if r["unique_id"] == 92074020)
            self.assertFalse(pul["injured"])
            self.assertEqual(pul["contract_iso"], "2031-06-30")

            # AI club_context & player_detail see live Milan budget, fit Pulisic, loaned Gusto
            ctx = fmtools.club_context(self.db, "Milan", squad_limit=40)
            self.assertEqual(ctx["balance"], 35638667)
            self.assertEqual(ctx["transfer_budget"], 67942576)
            g_det = fmtools.player_detail(self.db, 49056280)
            self.assertTrue(g_det["is_loaned_out"])
            self.assertEqual(g_det["loan_club"], "West Ham")
            self.assertFalse(g_det["listed_for_loan"])
            sq = fmtools.analyze_squad(self.db, "Milan")
            self.assertNotIn("Christian Mate Pulisic", sq["injured"])
        finally:
            self.db.live.clear()
            self.db.live.update(orig_live)
            self.db.club_live.clear()
            self.db.club_live.update(orig_club_live)


if __name__ == "__main__":
    unittest.main()

# The FM27 AI Assistant inside this editor

This is the integration of the person's own **FM AI Assistant** project into the
FMRTE-style editor. The goal is a conversation that answers, in plain language,
what the save's own data and the measured match-engine evidence support:

* **tactic setup** — a role with and without the ball for every slot, plus the
  ten team instructions, each chosen from values the game accepts;
* **training schedules** — a seven-day, three-session week built from FM27's
  measured activity ids;
* **lineup recommendations** — the best unique XI for a shape, with
  alternatives and the slots the squad cannot fill;
* **transfer and recruitment advice** — a literal hard filter, then a decision
  score, with an unknown price kept explicitly unknown.

Everything is read-only. The assistant proposes; nothing it does writes into
the game.

---

## 1. What was kept from the original project

| Original | Here | Note |
|---|---|---|
| `CompositeAiPromptContext` three-layer prompt | `assistant.INSTRUCTIONS` + `ALWAYS_ON` + `save_context()` | Same envelope text, so a saved prompt reads the same way |
| `KnowledgeBase` / `studies.json` | `fmrte/knowledge.py` + `data/fm27-studies.json` | The file is the person's own, shipped unchanged (68 findings) |
| `FmKnowledgeTools` | `fm27_search_knowledge`, `fm27_get_knowledge_study`, `fm27_get_management_priorities` | Same priority order and the same "not in snapshot" list |
| `RoleFitService` | `fmtools.role_fit`, `fmtools.slot_fit`, `fmtools.best_role_for` | Same 16/4 weights and the same 35/45/20 slot blend |
| `LineupOptimizerService` | `fmtools.optimize_lineup` | Unique XI, locks, injured and on-duty filters, alternatives |
| `SquadTacticPlanner` | `fmtools.build_tactic_from_squad`, `fmtools.derive_instructions` | Roles and instructions derived from the eleven, never copied from a template |
| `FmDecisionTools` (squad, shortlist, replacements) | `fm27_analyze_squad`, `fm27_transfer_shortlist`, `fm27_find_replacements` | Same literal-filter-then-score shape |
| `TscTrainingScheduleCodec` activity ids | `fmtools.TRAINING_ACTIVITIES` | The measured 0–7, 101–102, 200–203, 300–301, 400, 500 table |
| `CommandCodeConversationService` tool loop | `assistant.chat_with_tools` | Bounded rounds (10) and tool calls (24), a 32 KiB per-result cap, and one fallback for a provider that rejects `tools` |

## 2. The tool catalogue

Sixteen tools, all prefixed `fm27_` so a prompt can name them and a log can be
searched for them. `fmtools.tool_specs()` is the single source of truth for the
JSON schema the model sees, and `fmtools.execute_tool()` is the single
dispatcher, so a new tool is added in one place.

| Tool | Answers |
|---|---|
| `fm27_find_players` | who matches a name, club, age or ability |
| `fm27_player_detail` | everything about one player, including his best role and the CA-blended rating |
| `fm27_club_context` | a club's profile, money, facilities, squad and next fixture |
| `fm27_find_clubs` | club search by name, nation or competition |
| `fm27_analyze_squad` | depth, tactic fit, contract risk, injuries and recruitment needs |
| `fm27_optimize_lineup` | the best unique XI for a shape, with alternatives |
| `fm27_transfer_shortlist` | candidates for one literal position, filtered and scored |
| `fm27_find_replacements` | who would do a named player's job, and the gain |
| `fm27_build_tactic_from_squad` | a complete tactic for the squad that is at the club |
| `fm27_role_attributes` | which attributes a role asks for, at the original's weights |
| `fm27_create_training_schedule` | a seven-day, three-session week |
| `fm27_training_activities` | every measured activity id |
| `fm27_search_knowledge` | measured engine findings |
| `fm27_get_knowledge_study` | one study in full |
| `fm27_get_management_priorities` | what to fix first, in measured order |
| `fm27_fixtures` | a club's fixtures from the game date |

Every tool returns a dict. A failure is `{"error": "..."}`, never an exception,
because a raised exception would end the model's turn with nothing to read.

## 3. The three rules that are not negotiable

1. **Role before rating.** `role_rating` blends CA with the best role's 16/4
   attribute score, so the same player scores differently in another role. The
   tools therefore always name the role they judged.
2. **Current Ability is not a match input** (`S20-CA-INERT`). It is one term in
   the blend, never the sort key. Sharpness (`S5-SHARPNESS-FIRST`) and condition
   (`S5-CONDITION-SECOND`) outrank every tactical change.
3. **A pressing trap that is not Standard costs points** (`S5-PRESSING-TRAP`).
   The instruction tool never proposes one by accident, and the training week
   uses session 6 to *train* pressing, not to change the trap.

## 4. How a turn runs

```
person's message
   │
   ├─ save_context(): data mode, game date, build, counts, active club, top XI
   ├─ knowledge_context(): the four always-on findings
   ▼
POST /chat/completions  (tools = fmtools.tool_definitions())
   │
   ├─ tool_calls ──► fmtools.execute_tool(db, name, args)
   │                    └─ reads the snapshot + live overlay, never writes
   │                 result JSON (32 KiB cap, then "_commandcode_response" marker)
   ◄─────────────────┘
   ▼
final text (or a message saying the round limit was reached)
```

The loop is bounded twice: ten model requests and twenty-four tool calls per
turn. A provider that answers `400` to a request carrying `tools` falls back
once to a plain completion with the full prompt envelope, so a model without
tool support still answers, just without live data.

## 5. Where the data comes from

* `data/fm_snapshot.db` — the exported save (42,344 players).
* `db.live` — values read from the running game, overlaid on the snapshot.
* `db.live_meta` — the live game date and the managed club, when the attached
  game exposed them, so advice is about the club the person is actually at
  rather than the editor's default.

A tool reads the overlay through `Snapshot._apply`, so the model sees what the
screen sees. The live memory is cleared per tool call (`clear_fit_cache`), which
is what makes a refresh visible immediately.

## 6. What this build does not do

* **It does not write `.fmf` tactic or schedule files.** The container needs a
  Zstandard frame and an AES-CTR stream; Python 3.12's standard library has
  neither, and the `.tsc` prefix bytes and the `image.img` / `details.aom`
  resources are only known from one sample. Writing a file FM might reject is
  worse than writing a sheet a person can type in, so the plans are sheets and
  they say so.
* **It does not read tactical familiarity.** The snapshot cannot hold it, and
  `fm27_get_management_priorities` says so rather than assuming.
* **It does not follow a template's settings.** The planner names every
  instruction it sets, which is exactly the leak `SquadTacticPlanner` closed:
  anything unnamed would silently keep the template author's value.

## 7. Where the code lives

The layer was split so one file hides one thing. A caller passes `db` and gets a
dict back; none of these modules imports Tkinter, SQLite or ctypes.

| Module | Lines | Hides |
|---|---|---|
| `fmrte/assistant.py` | 790 | provider transport, settings, DPAPI key store, the tool loop |
| `fmrte/aiprompt.py` | 177 | the three-layer prompt: instructions, always-on findings, save context |
| `fmrte/fmtools.py` | 543 | the 16 tool schemas, the dispatcher, the knowledge wrappers |
| `fmrte/fmroles.py` | 487 | position vocabulary, role fit (16/4), slot fit (35/45/20), fitness, availability |
| `fmrte/fmlineup.py` | 306 | the XI optimizer: uniqueness, locks, injured exclusion, local search |
| `fmrte/fmsquad.py` | 237 | squad depth, weaknesses, club context and money |
| `fmrte/fmtransfers.py` | 224 | the literal shortlist filter, the decision score, replacements |
| `fmrte/fmlookup.py` | 183 | player search, player detail, club search, fixtures |
| `fmrte/fmplans.py` | 617 | **the single plan generator** (tactic-from-squad, training week) and the four rule tables |
| `fmrte/tacticfiles.py` | 78 | the game's own `Tactics/*.json` templates |
| `fmrte/knowledge.py` | 257 | the Match Lab pack |

Three invariants, each pinned by a test rather than by a comment:

1. **One authority per rule.** The training activity ids, weekday names,
   team-instruction vocabulary and position labels each have exactly one home;
   every other mention is the same object, and the tests use `assertIs` (not
   `==`) so a re-introduced copy fails even when it happens to agree today.
2. **One generator.** The panel and the model get the same sheet for the same
   input; the panel has no fallback generator to drift to.
3. **No global cache.** The role-fit memo is a `FitCache` created per call, so a
   stale entry is unrepresentable — the old module-level dict depended on every
   caller remembering to clear it.

`tacticfiles.py` exists so `fmtools` never imports `assistant`: the template
reader sits below both. A subprocess import test pins the direction.

## 8. Tests

`tests/test_fmtools.py` (63 cases) covers the knowledge pack, the position and
role tables, the role-fit blend, the optimizer (uniqueness, minimums, injuries,
locks, alternatives), the literal transfer filters, the unknown-price rule, the
squad analysis, the derived instructions, the training week, the catalogue, the
tool-calling loop with a stubbed transport, and the three invariants above.
`tests/test_assistant.py` (31 cases) covers the prompt, transport, settings and
key storage.

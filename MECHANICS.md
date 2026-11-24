# Mechanics sheet

What this build reads, how it converts the numbers, and where every constant comes
from. Every value below is marked with how it was established:

- **observed** — read directly out of the original's files or bytes.
- **derived** — computed from observed values and checked against the original's
  own output.
- **inferred** — a best guess; the original's code could not be read for it.

The original is `FMRTE\current\FMRTE.exe`, SHA-256
`1da8b1891a029e526ee471be97aa2f1c0573e931bd55f4eb091ed886d546e93f`, 127,948,768
bytes, FMRTE **27.1.0-build14**.

---

## 1. What the original actually is

| Item | Value | How |
|---|---|---|
| Format | PE32+ (x86-64), image base `0x140000000` | observed |
| Runtime | **.NET NativeAOT**, RTR 16.0 (.NET 9/10) | observed |
| Managed metadata | **none** — no CLI header, no IL | observed: `managed_assembly_show` → `no CLI header` |
| Symbols | stripped; 50,000+ functions, all `sub_…` | observed |
| UI stack | Avalonia + SkiaSharp + HarfBuzz | observed: `libSkiaSharp.dll`, `libHarfbuzzSharp.dll`, `av_libglesv2.dll` |
| Launcher | `FMRTE\FMRTE.exe` (536 KB, x86) starts `current\FMRTE.exe` | observed |

**Consequence:** ILSpy / dnSpy / any CIL decompiler cannot read this program. Only
native decompilation applies, and the 122 MB image defeats full auto-analysis
(see §7). The values below therefore come from the original's **own data files and
its own UI output**, cross-checked against its code where the code was readable.

---

## 2. The rating formula (the headline fix)

The build used to show `CA / 200 * 100`. That is **wrong**. The original's
"Rating cur / pot" is a **role rating**: how well the player's attributes fit the
selected role and duty.

### 2.1 Constants (observed)

From `Ratings\rolesAndDuties.json`:

| Constant | Value |
|---|---|
| `KeyAttributesWeight` | **16** |
| `PreferableAttributesWeight` | **4** |
| `AvoidAttributesWeight` | **3** |
| `SettingsVersion` | `27.0.0.0` |

From `Ratings\teamSelection.json` (used for squad selection, not the player rating):

| Constant | Value |
|---|---|
| `PositionAbilityWeight` | 150 |
| `RoleWeight` | 80 |
| `CurrentAbilityWeight` | 120 |
| `PotentialAbilityWeight` | 90 |
| `FitnessWeight` | 80 |
| `ReputationWeight` | 35 |
| `MoraleWeight` | 15 |

### 2.2 The role definition (observed)

`GeneralPositionToRoleAndDutiesDefinitionsMap` maps each general position to roles,
and each role to its duties. For Pulisic the original shows **Wide Forward (IP)** —
general position `Winger`, role `WideForward`, duty `InPossession`:

| Key attributes (8) | Preferable attributes (8) |
|---|---|
| Dribbling, FirstTouch, Technique, Anticipation, OffTheBall, Acceleration, Agility, Pace | Crossing, Finishing, Passing, Composure, Flair, Workrate, Balance, Stamina |

### 2.3 The formula (the original's own help text + compiled 0.5)

The original's settings help says exactly what the rating is
(`current\Assets\Languages\english.json`):

```
Settings.Ratings.RoleWeights.CaShare.Help =
  "How much CA counts in Actual Rating and Potential Rating. The rest comes
   from the player's best role. Default: 50%."
```

The compiled default agrees: `CaSharePercent` is a double field at **+0x38**
initialised to `0.5` by the constructor at `0x140740130` and by the weight
stores at `0x14074042C`/`0x140740436`. So the displayed rating is a blend, not
a plain attribute average:

```
caRatio   = CA / 200

keySum    = Σ key attributes
prefSum   = Σ preferable attributes
roleScore = (keySum/20 × 16 + prefSum/20 × 4)
            / (keyCount × 16 + prefCount × 4)

ratio     = 0.5 × caRatio + 0.5 × roleScore      (float32)
Rating%   = truncate(ratio × 100, 2)             (truncated, never rounded)
```

The weights are compiled-in doubles: **16.0** at `0x140740455` → `+0x20`,
**4.0** at `0x1407403FB` → `+0x28`, **3.0** at `0x140740410` → `+0x30`, and
**0.5** at `0x14074042C` → `+0x38`. The full constructor `0x140740340`
(decompiled in §16) builds the 20-entry ladder into `+0x18` first, then stores
those four values.

**Verification with the person's real save** (Pulisic, uid `92074020`):

| Step | Value |
|---|---|
| CA / 200 | 158 / 200 = **0.79** |
| keySum | 16+16+15+15+17+16+15+14 = **124** |
| prefSum | **112** |
| roleScore | (124/20×16 + 112/20×4) / (8×16 + 8×4) = **0.76** |
| ratio | 0.5 × 0.79 + 0.5 × 0.76 = **0.775** exactly |
| ratio as a **float32** | **0.7749999761581421** |
| × 100, truncated to 2 dp | **77.49 %** |
| Original's screen | **77.49 %** ✓ |

**Why the earlier sheet missed the CA term.** For Pulisic the numbers collide:
his key-only share is also `124/160 = 0.775`, exactly the blend. The old sheet
saw 0.775, assumed key attributes only, and concluded the preferable
attributes and CA were not in the displayed figure. They are: the help text is
explicit, and other players separate the two readings. Kylian Mbappé, for
example, is **85.00 %** key-only but **89.40 %** blended (CA 192 → 0.96;
roleScore 0.828; blend 0.894). Across the save, **42,340 of 42,344** ratings
change under the corrected formula (largest move 27.07 points), while
Pulisic's stays 77.49 — which is why the old build looked right on his page.

**Role selection is still key-based.** Scoring roles by the weighted
`roleScore` instead would change **14,244 of 42,344** highlighted roles, and
nothing in the original's files says which criterion it uses; Pulisic's role is
`WideForward` either way. So `best_role()` keeps the key rule that was verified
against the screen, and only the header formula changed.

The original's own property names for the two figures are
`PlayerRating.ActualRating` and `PlayerRating.PotentialRating` (observed in
`config.json` `ColumnsSettings`), and the club-level average is
`StatsContainer.OverallClub.AverageRating`.

`PositionRatingPenalties` (observed, 20 entries) is the out-of-position penalty
ladder used when a player is fielded away from his natural position:

```
76, 72, 68, 64, 60, 56, 52, 48, 44, 40, 36, 20, 17, 15, 5, 4, 3, 2, 1, 0
```

**This ladder is code, not just a setting** — see §16.1. It is applied by
`position_penalty()`: indexed by how far the player's competence for a position
falls below his best competence, read from the far end so that a player in his
best position takes **0** and a player with no competence there takes **76**.
Verified over all 42,344 players: every player's best position scores exactly 0.
The ladder's length is fixed at **20** in code (`0x1407404C0` rejects any
settings object whose `+0x18` array is not `0x14` elements), and the build now
carries that as `PENALTY_LADDER_LENGTH`.

### 2.3a How the role is chosen (observed + verified)

> **Updated in §13.3.** The original does not use one fixed role per position.
> It scores every role and duty the player's best position offers and shows the
> highest. For Pulisic that is `WideForward` / `InPossession` at 77.49, which is
> the "Wide Forward (IP)" the original displays. The build now does the same in
> `ratings.best_role()`.

The original sets `HighlightBestRoleDutyAutomatically: true` in `config.json`, so
it rates a player in his **best** position rather than his listed one. The
snapshot stores one 1..20 competence per position (`players` table), so
`best_general_position()` takes the general position with the highest competence.

Verified: Pulisic's competences are AMR **20**, AMC 18, ST 17, AML 15, MR 14 …
and the original's page reads **"Wide Forward (IP)"** — AMR → `Winger` →
`WideForward`. The old build guessed this from the position text; it now comes
from the competence data.

Across the save the chosen positions are realistic, which is a second check:

| Position | Players | Share |
|---|---|---|
| DefenderCentral | 7,533 | 17.8 % |
| Winger | 6,738 | 15.9 % |
| Striker | 6,187 | 14.6 % |
| FullBack | 5,529 | 13.1 % |
| MidfielderCentral | 5,473 | 12.9 % |
| Goalkeeper | 4,233 | 10.0 % |
| DefensiveMidfielder | 3,201 | 7.6 % |
| AttackingMidfielderCentral | 2,824 | 6.7 % |
| WingBack | 626 | 1.5 % |

### 2.4 Where this lives in the build

`app/fmrte/ratings.py` — `role_rating()` (the CA blend), `role_score()` (the
16/4 role half), `key_rating()` (the role panel's "Key" figure),
`as_displayed()`, `best_general_position()`, `position_penalty()`, `clamp()`
and `RATINGS`. `CA_SHARE = 0.5` and `PENALTY_LADDER_LENGTH = 20` are the two
constants read straight out of the original's code. `Snapshot.rating()` in
`app/fmrte/db.py` calls `role_rating()`. The role card shows `key_rating()` and
`preferable_rating()` separately, because the original's role panel labels
those two figures while the header shows the blend.

---

## 3. The presets (observed, byte-for-byte)

Read from `Presets\*.json`. The build applies exactly these.

### Heal (`Presets\Heal.json`)

| Property | Value |
|---|---|
| `Condition` | **10000** |
| `MatchSharpness` | **10000** |
| `Jadedness` | **0** |
| `IsInjured` | **false** |

### Inspire (`Presets\Inspire.json`)

| Property | Value |
|---|---|
| `Morale` | **20** |
| `Jadedness` | **0** |
| `Contract.NumberOfUnhappinessMotives` | **0** |
| `Contract.Happiness` | **100** |
| `Contract.PlayingTimeHappiness` | **100** |
| custom | `ImproveTeamCohesion` |

### Destroy (`Presets\Destroy.json`)

| Property | Value |
|---|---|
| `Morale` | **0** |
| `Jadedness` | **1000** |
| `MatchSharpness` | **5000** |
| `Condition` | **5000** |
| `Contract.Happiness` | **0** |
| `Contract.PlayingTimeHappiness` | **0** |
| custom | `DestroyTeamCohesion` |

Every action is `ValueSetType: "Replace"`, `TargetType: "Player"`.

**This settles the jadedness range:** the original's own Destroy preset writes
**1000**, and the person's save holds **−125**. So jadedness is a signed value
whose practical range is roughly −1000…1000, not 0…10000. The old sheet guessed
this; it is now observed.

### Wonderkid generator (`Ratings\wonderkidGenerator.json`)

Attribute values are rolled from these bands. Probabilities are **percent** and
each set sums to 100.

| Band | Key | Additional | Regular |
|---|---|---|---|
| 1–5 | 1 % | 2 % | 1 % |
| 6–8 | 1 % | 3 % | 1 % |
| 9–11 | 10 % | 20 % | 30 % |
| 12–14 | **31 %** | **35 %** | **40 %** |
| 15–18 | **45 %** | 30 % | 20 % |
| 19–20 | 12 % | 10 % | 8 % |

The original's UI states the rule: *"the sum of the probabilities must be 100"*.

---

## 4. The read path (observed, corroborated)

FMRTE reads the running game's memory. The chain is:

```
GetRequiredGameMemory → GetRequiredOffsets → get_ObjectsOffsets
   → get_BlockSource → get_BlockRange → get_MemoryAddress
```

### 4.1 Imported APIs (observed)

`ReadProcessMemory`, `WriteProcessMemory`, `OpenProcess`, `VirtualAllocEx`,
`VirtualQueryEx`, `CreateRemoteThread`, `K32EnumProcessModulesEx` — all from
`kernel32`. `VirtualProtectEx` and `NtReadVirtualMemory` are **not** imported.

Version detection uses `VERSION.dll` (`GetFileVersionInfoSizeExW`,
`GetFileVersionInfoExW`, `VerQueryValueW`) — the build number comes from
`game_plugin.dll`'s file version resource, **not** from a constant in the binary.

### 4.2 The read wrapper at `0x1408410A0` (observed)

`native_function_pseudocode(0x1408410A0)` returns a flattened state machine:

```c
int32_t sub_1408410a0(int64_t rcx, int64_t rdx, int64_t r8, int64_t r9)
{
    rdi = rcx; r14 = rdx; rbx = r8; r15d = r9d;   // 4 register args
    rsi = arg_30;                                  // 5th arg, from the stack
    *(uint32_t*)(rsi) = 0;                         // out-parameter zeroed
    ...
    r8 = &data_14623e0d0;                          // jump table
    goto *r8;                                      // computed jump
}
```

That is the same shape the earlier investigation recorded: **5 arguments**
(context, target address, buffer, length, out bytes-read), the 5th being an out
pointer, and the body is control-flow flattened behind a jump table at
`0x14623E0D0`. The write wrapper at `0x140841320` has the identical shape with its
own table at `0x14623E100`.

| Item | Value |
|---|---|
| Read wrapper | `0x1408410A0` |
| Write wrapper | `0x140841320` |
| Read jump table | `0x14623E0D0` |
| Write jump table | `0x14623E100` |
| Fixed block size at the checked call site | `0x400` (1024 bytes) |

**Honest limit:** the flattened body means the *argument meanings* above are
corroborated (prologue + out-pointer + jump table) but the full body was not
recovered. The build does not depend on them.

### 4.3 The offset table and the record layout (observed)

The person's own working reader (FM AI Assistant 2.1.0) is the authority for
the offsets, because it is the code that actually reads this save. Read out of
`fm-ai-assistant-2.1.0-SNAPSHOT-desktop.jar`:

`com.github.fmaiassistant.linux.FmOffsets`

| Item | Value |
|---|---|
| Registered builds → table RVA | 2494687 → 92407336, 2493426 → 91248104, 2486695 → 91239912 |
| Legacy table RVA | 91239912 |
| `PeopleOffset` slot | **336** |
| `ContinentOffset` / `RegionOffset` slots | 264 / 352 |
| Vector pointer inside a collection | **128** |
| Scan range / chunk | 96 MiB / 1 MiB |
| Minimum accepted table score | **35** of 47 |

`com.github.fmaiassistant.memory.PlayerRecordLayout.current()` gives the
`Direct` offsets, relative to the person object:

| Field | Offset |
|---|---|
| unique id | **+12** |
| date of birth | +136 |
| current ability | **−36** |
| potential ability | **−34** |
| condition | **−44** |
| jadedness | **−46** |
| match sharpness | **−48** |
| morale | **−28** |
| home / current / world reputation | −42 / −40 / −38 |
| history copy source | −314 |

`com.github.fmaiassistant.memory.PersonIdentityLayout.currentSubtypes()` gives
the RTTI dynamic offsets that separate the person kinds: staff **256**,
player **648**, player/staff **896**, human manager **1088**. The classifier
reads `u64(person)` (the vtable), then `u64(vtable − 8)` (the RTTI
descriptor), then `i32(descriptor + 4)`.

### 4.4 Why the first live attempt failed (observed)

Two separate causes, both measured on this machine:

1. **The running build is not in the registry.** `game_plugin.dll`'s product
   version is `27.1.0.14749`, but the game reports build **2505117**
   (`27.1.0+2505117`). That number is not one of the three registered builds,
   so the old code had no table RVA and gave up. The fix is the scoring scan
   in §4.3, which finds the table without knowing the build.
2. **Integrity levels.** Football Manager 27 runs at **Medium** integrity
   (0x2000). A process at **Low** integrity (0x1000) — which is how a sandboxed
   or restricted session runs — cannot open it for reading: `OpenProcess`
   returns `ACCESS_DENIED` (5). A normally launched editor runs at Medium and
   can read it. If the game is ever started elevated, the editor must be
   elevated too, and the window now says so in plain words.

The scan was measured at **1.7 s** over a 96 MiB image with 35 % plausible
pointers, so it is fast enough to run from the Load Game button.


---

## 5. Values and ranges

The ranges below are the ones the original's own property setters, spin
controls and presets use. They are **not** proven by a single clamp helper:
§13.5 and §16.5 show that `0x1404446B0` is a virtual-dispatch wrapper, not the
attribute clamp, so the code evidence is the `mov r8d, <maximum>` argument at
each setter call site, not a rejected out-of-range value. The two rules that
*are* enforced by a validator are in §16.2 and §16.3: the penalty ladder must
have **20** entries and the three rating-group weights must each total **100**.

| Value | Range / rule | How |
|---|---|---|
| current / potential ability | 0–200 | **observed in code**: `mov r8d,0xC8` at `0x1411D0A48` |
| attribute | 0–20 | **observed in code**: `mov r8d,0x14` at `0x1411782B8` |
| condition | 0–10000 (10000 = 100 %) | **observed in code**: `mov r8d,0x2710` at `0x14102D4E0` |
| match sharpness | 0–10000 | same clamp site as condition |
| jadedness | about −1000…1000, **signed** | observed: Destroy preset 1000, save holds −125 |
| morale | 0–20 | observed: Inspire 20, Destroy 0 |
| reputation | 0–10000 | **observed in code**: same 0x2710 clamp |
| rating | 0–100 %, role-based, truncated to 2 dp | derived §2.3, §9.6 |
| wages | weekly by default | observed: `"Wages": "Weekly"` |
| distances | metres by default | observed: `"Distance": "Meters"` |
| currency | United Kingdom Pound | observed: `SelectedCurrencyName` |

`ratings.clamp()` in `app/fmrte/ratings.py` implements this table, and
`Snapshot.set_value()` applies it to every integer player edit, so the build
cannot store a value the original would refuse.

---

## 6. Where the data comes from

- The person's FM27 save, game date **2026-07-20**, FM build **2494687**,
  game version `27.1.0+2505117`.
- `data/fm_snapshot.db` is an export of the person's own FM AI Assistant cache
  (captured from the running game's memory): **42,344 players, 23,309 staff,
  42,129 clubs, 6,625 competitions**. Read-only for the app; `saves/edits.json`
  holds local edits.
- Verified against the original's own screen for Pulisic: CA 158 / PA 163,
  condition 9417, sharpness 3500, jadedness −125, morale 14, asking price
  £48,900,392, wage £84,746, contract to 2027-06-30 — all match exactly.
- `fmrte/live.py` re-reads the same player table from a running Football Manager
  process. It never writes to the game.

---

## 7. What could not be read, and why

| Attempt | Result |
|---|---|
| `managed_assembly_show` | no CLI header — NativeAOT, no IL |
| ILSpy / dnSpy | inapplicable (no managed metadata) |
| Ghidra full auto-analysis | does not converge on 122 MB (documented: 62 min, 4.5 GB, no output) |
| Ghidra targeted decompile | **failed to start** on this image; the built-in lifter was used instead |
| `core_feature_find` on rating names | strings exist only in reflection metadata, with **no code references** |
| UTF-16 literal search for `PositionAbilityWeight` | 0 matches — the name is never a runtime string literal |

So the rating formula in §2 is **derived from the original's data files and
verified against its own displayed output**, not lifted from decompiled code.
That is why it is labelled derived rather than observed.

---

## 9. What decompilation did establish

> **Updated.** Ghidra now opens this image — the earlier failure was a JDK
> mismatch (Ghidra 12.1.4 needs JDK 21; `JAVA_HOME` pointed at JDK 17), not the
> image. See **§13** for the results read with Ghidra, including two corrections
> to this section (§9.2 and §9.5).

Deep mode was on, but **Ghidra could not open this image** — every decompile
call reports `deep_engine_unavailable` and falls back to the built-in clean-room
lifter. The reason is environmental, not the image size: Ghidra also failed on
the 536 KB launcher, and its cache folder for the target is empty. Java itself
runs on this machine (checked directly), so the failure is in how Ghidra is
launched here, not in the program.

The built-in lifter still produced real, checkable results. Everything below is
**observed**, not inferred.

### 9.0 How the functions were found

The original is stripped, so there are no names to search for. The route that
worked was **data first, then code**:

1. `native_bytes_find` for the literal text of a setting name
   (`KeyAttributesWeight`, `PositionRatingPenalties`) — this locates the
   NativeAOT string blob, but the blob is length-prefixed, so its address is
   not the string object's address and `xrefs` on it returns nothing.
2. `native_bytes_find` for the **machine-code pattern** of a constant the
   setting implies — `mov r8d,0x14` (20), `mov r8d,0xC8` (200),
   `mov r8d,0x2710` (10000), and the `41 B8 … 45 33 C9` shape of a clamp call.
3. `native_function_find` on each hit to get the containing function, then
   `native_function_pseudocode` / `native_function_disassemble` to read it.
4. For the ladder, `native_xrefs_list` on the `.rdata` address did work,
   because that reference is a plain `lea` rather than a string object.

That is why the addresses below are trustworthy even though the image is
stripped: each one is either a direct data reference or a byte pattern that
was read back and checked.

### 9.1 The penalty ladder is code (`0x140741F50`)

`native_function_pseudocode(0x140741F50)`:

```c
int64_t sub_140741f50()
{
    rax = sub_143441f00(&data_14619acd0, 0x14);   // allocate int[20]
    rbx = rax;
    sub_14215cbc0(rbx + 16, &data_14517bb38, 0x50); // copy 80 bytes
    rax = rbx;
    return rax;
}
```

| Item | Value |
|---|---|
| Ladder data | `0x14517BB38`, `.rdata`, 80 bytes |
| Element count | `0x14` = **20** |
| Copy length | `0x50` = **80** bytes = 20 × int32 |
| Builder function | `sub_140741F50` @ `0x140741F50` |
| Array allocator | `sub_143441F00` (not discovered — beyond the 50,000-function cap) |
| Copy helper | `sub_14215CBC0` |

The 20 values read straight out of `.rdata`:

```
76, 72, 68, 64, 60, 56, 52, 48, 44, 40, 36, 20, 17, 15, 5, 4, 3, 2, 1, 0
```

This confirms the ladder's exact order, length and element type from code, and
is why `position_penalty()` in the build can apply it with confidence.

### 9.2 The weights are compiled in as doubles

> **Corrected in §13.1.** This section originally concluded the weights were
> "loaded, not compiled in" because the int32 sequence 16, 4, 3 has no byte
> match. They are **IEEE-754 doubles**, which an int32 search cannot find. The
> settings constructor at `0x14074032C` stores 16.0, 4.0 and 3.0 directly, and
> the JSON values are the editable defaults that those setters overwrite.

The int32 sequence for `KeyAttributesWeight, PreferableAttributesWeight,
AvoidAttributesWeight` (16, 4, 3) has **zero** byte matches in the 122 MB image,
and neither does the `teamSelection.json` sequence (150, 80, 120, 90, 80, 35,
15). The original therefore reads them at run time from
`#/Assets/Ratings/rolesAndDuties.json` and `teamSelection.json` — which is
exactly why its settings screen exposes them as editable
(`RoleWeightsSettingsViewModel`, `KeyAttributesWeight!Getter/Setter`).

**Consequence for the build:** these are settings, not constants. Editing the
JSON changes the original's behaviour, and the build's values should be treated
the same way.

### 9.3 The read/write wrappers (`0x1408410A0`, `0x140841320`)

Both decompile to the same shape: four register arguments plus a fifth from the
stack, the fifth being a `uint32` out-parameter that is zeroed first, then a
computed jump.

| Item | Value |
|---|---|
| Read wrapper | `sub_1408410A0` @ `0x1408410A0` |
| Write wrapper | `sub_140841320` @ `0x140841320` |
| Read jump table | `0x14623E0D0` |
| Write jump table | `0x14623E100` |
| Read wrapper callers | `sub_1408422E0` @ `0x14084232C`, `sub_1408423D0` @ `0x140842406` |

The bodies are control-flow flattened behind those tables, so the argument
meanings are corroborated but the full bodies are not recoverable with the
built-in lifter. The build does not depend on them.

### 9.4 What is still not readable

- **The rating arithmetic itself.** The weights are JSON, and the function that
  combines them is reached only through virtual dispatch (NativeAOT), so no
  static caller chain reaches it. The formula in §2 therefore stays *derived and
  verified against the original's output*, not lifted from code.
- **String references.** Every string in this image is a NativeAOT frozen blob:
  a length byte followed by UTF-8 characters, so the address of the text is not
  the address of the string object. `xrefs` on a string address returns 0
  references even for strings that are certainly used. Data references do work
  (the ladder has one), which is how §9.1 was found.
- **Function discovery is capped** at 50,000 functions, so some callees
  (`sub_143441F00`) have no entry and cannot be decompiled. The cap also means
  the whole `0x140C…`–`0x1411…` region has no discovered functions, so the
  clamp call sites there can only be read as raw instructions.

### 9.5 The value ranges are enforced in code (`sub_1404446B0`)

> **Corrected in §13.5.** `0x1404446B0` is a real range-clamp helper, but the
> `mov r8d, 0x14 / 0xC8 / 0x2710` sites below do **not** call it — their `E8`
> targets are property setters at `0x1431F9760` / `0x1433F9760`, where the
> immediate is a *value being written*, not a maximum. The helper's only three
> callers clamp to 1…0x7FFFFFF5 and 0…1000. The 0…20 / 0…200 / 0…10000 ranges
> remain the right ranges to use (they match the original's settings and
> presets) but are **observed from settings, not proven from a clamp**.

The original does not merely *suggest* ranges in its settings files — it
clamps every editable player value in its own machine code. The clamp helper
is `sub_1404446B0` at **`0x1404446B0`**:

```c
int64_t sub_1404446b0(int64_t rcx, int64_t rdx, int64_t r8, int64_t r9)
{
    // rcx = value, rdx = minimum, r8d = maximum, r9 = ...
    rax = sub_142217810(&data_1472a67e0);      // resolve the virtual method
    ...
    rax = (*r14)(value, minimum, maximum, ...); // dispatch, clamped
    return rax;
}
```

Its callers pass the maximum in `r8d` and the minimum in `r9d`. The pattern
`mov r8d,<max>` / `xor r9d,r9d` (minimum 0) / `call` appears at these sites,
each verified by reading the bytes:

| Maximum | Hex | Call site | Applies to |
|---|---|---|---|
| **20** | `0x14` | `0x1411782B8` | every player attribute |
| **200** | `0xC8` | `0x1411D0A48` | current / potential ability |
| **10000** | `0x2710` | `0x14102D4E0` | condition, match sharpness, reputation |

The instruction sequence at `0x1411782B8`, read directly:

```asm
mov  rax, [rsp+0xA0]
mov  rdx, [rax+0x70]        ; the value
mov  rcx, [rsp+0x8B8]       ; the object
mov  r8d, 0x14              ; maximum = 20
xor  r9d, r9d               ; minimum = 0
call sub_1404446B0
```

and at `0x1411D0A48` the same shape with `mov r8d, 0xC8` (200), and at
`0x14102D4E0` with `mov r8d, 0x2710` (10000).

**Consequence for the build:** `ratings.clamp()` implements exactly these
ranges, and `Snapshot.set_value()` runs every integer player edit through it.
So typing 99 into an attribute stores 20, typing 250 into CA stores 200, and
typing 20000 into condition stores 10000 — the same result the original gives.

**Jadedness is the exception.** It is signed: the original's own Destroy
preset writes **1000** and the person's save holds **−125**, so the build
allows −10000…10000 for it rather than clamping at 0.

### 9.6 The rating's precision (`as_displayed`)

The 77.49-vs-77.50 question is settled by storage precision, not rounding:

| Step | Value |
|---|---|
| 124 / 160 in double precision | `0.775` |
| the same ratio as a **float32** | `0.7749999761581421` |
| × 100 | `77.49999761581421` |
| truncated to 2 decimals | **`77.49`** |
| rounded to 2 decimals | `77.50` ✗ |

The original's header reads **77.49 % / 77.49 %**, so it truncates. The build
now does the same in `ratings.as_displayed()`, and the window shows 77.49 %.

---

## 13. What the decompiler established (Ghidra, this session)

Section 9 said Ghidra could not open this image. **That was an environment fault,
not the image**, and it is now fixed. Ghidra 12.1.4 needs a JDK 21; this machine's
`JAVA_HOME` pointed at JDK 17, so every launch died before writing a log. With
`engines.ghidra.java_home` set to the JDK 21 that ships beside Ghidra, the image
imports in about a minute.

Full auto-analysis still does not converge on 122 MB (the handover measured 62
minutes with no output), so the working route is **`-noanalysis` import plus
targeted scripts**: decompile one address, scan for `E8` call sites, scan for
RIP-relative references, and find pointer slots. Every address below was read
that way and is reproducible.

### 13.1 The role weights are compiled in as doubles (`0x140740455` … `0x14074042C`)

This corrects §9.2, which concluded the weights were "loaded, not compiled in"
because the int32 sequence 16, 4, 3 has no byte match. They are **doubles**, so
an int32 search could never find them.

`FUN_14074032c` is the role-weights settings constructor. Decompiled:

**Address correction (this session).** `0x14074032C` is NOP padding
(`90 90 90 …`), not the constructor entry. The real stores sit further on, in a
body the engine never discovered as a function because it is reached only
through a jump table. Each is a `mov r64, imm64` followed by
`mov [rbx+disp8], r64`:

| Store address | Immediate | Double | Field |
|---|---|---|---|
| `0x140740455` | `0x4030000000000000` | **16.0** | `+0x20` KeyAttributesWeight |
| `0x1407403FB` | `0x4010000000000000` | **4.0** | `+0x28` PreferableAttributesWeight |
| `0x140740410` | `0x4008000000000000` | **3.0** | `+0x30` AvoidAttributesWeight |
| `0x14074042C` | `0x3FE0000000000000` | **0.5** | `+0x38` (unnamed) |

The enclosing body is reached from `sub_140740340`, which calls the **role
builder** `sub_140741FA0` at `0x14074035D` (not `sub_140741F50`; §16.1 has the
corrected call) and then dispatches through the table at `0x146234688`. The
dispatched body calls the ladder builder `sub_140741F50`, stores its result at
`+0x18`, and then writes the four weight doubles. The ladder pointer is
therefore installed at `+0x18`, and `sub_140740130` installs the 0.5 default
there (`0x140740139`–`0x140740140`) plus a fresh list at `+0x10`.

```c
uVar4 = FUN_140741f50();                       // build the 20-entry ladder
func_0x000143442410(param_1 + 0x18, uVar4);    // this->PositionRatingPenalties
*(undefined8 *)(param_1 + 0x20) = 0x4030000000000000;  // 16.0  KeyAttributesWeight
*(undefined8 *)(param_1 + 0x28) = 0x4010000000000000;  //  4.0  PreferableAttributesWeight
*(undefined8 *)(param_1 + 0x30) = 0x4008000000000000;  //  3.0  AvoidAttributesWeight
*(undefined8 *)(param_1 + 0x38) = 0x3fe0000000000000;  //  0.5  (see below)
```

| Field | Offset | Double | Value |
|---|---|---|---|
| `PositionRatingPenalties` | `+0x18` | — | the 20-entry ladder |
| `KeyAttributesWeight` | `+0x20` | `0x4030000000000000` | **16.0** |
| `PreferableAttributesWeight` | `+0x28` | `0x4010000000000000` | **4.0** |
| `AvoidAttributesWeight` | `+0x30` | `0x4008000000000000` | **3.0** |
| unnamed | `+0x38` | `0x3FE0000000000000` | **0.5** |

The accessors are one-liners, which is how the field map was confirmed:

| Offset | Getter | Setter |
|---|---|---|
| `+0x20` | `0x140740090` | `0x1407400A0` |
| `+0x28` | `0x1407400B0` | `0x1407400C0` |
| `+0x30` | `0x1407400D0` | `0x1407400E0` |
| `+0x38` | `0x140740110` | `0x140740120` |

Each is literally `return *(double *)(this + 0xNN);` / `*(double *)(this + 0xNN) = value;`.

**Consequence for the build:** the JSON values and the compiled values agree
(16 / 4 / 3), so the build's constants are right, but they are *defaults*: the
original can overwrite them at run time through those setters, which is why its
settings screen exposes them. The `0.5` at `+0x38` is not in any of the JSON
files and its role is not yet established.

### 13.1a The team-selection weights are immediates too (`0x1407417F0`)

`sub_1407417F0` is a six-instruction constructor that packs the squad-selection
weights into the object as **int32 pairs**, not doubles:

```asm
0x1407417F0  mov rax, 0x5000000096      ; 150, 80
0x1407417FA  mov [rcx+0x10], rax
0x1407417FE  mov rax, 0x5A00000078      ; 120, 90
0x140741808  mov [rcx+0x18], rax
0x14074180C  mov rax, 0x2300000050      ; 80, 35
0x140741816  mov [rcx+0x20], rax
0x14074181A  mov dword [rcx+0x28], 0xF  ; 15
0x140741821  ret
```

| Field | Offset | Value |
|---|---|---|
| `PositionAbilityWeight` | `+0x10` | **150** |
| `RoleWeight` | `+0x14` | **80** |
| `CurrentAbilityWeight` | `+0x18` | **120** |
| `PotentialAbilityWeight` | `+0x1C` | **90** |
| `FitnessWeight` | `+0x20` | **80** |
| `ReputationWeight` | `+0x24` | **35** |
| `MoraleWeight` | `+0x28` | **15** |

These are exactly the seven values in `Ratings/teamSelection.json`, so that file
and the compiled defaults agree. The getter at `0x140741830` is
`mov rax, [rcx+0x10]; ret` and the setter at `0x140741840` stores through
`lea rcx, [rcx+0x10]`, confirming the block starts at `+0x10`.

### 13.2 The penalty ladder, byte for byte (`0x14517BB38`)

`FUN_140741f50` allocates an `int[20]` (`0x14` elements) and copies `0x50` = 80
bytes from `0x14517BB38`. Reading those 80 bytes directly:

```
4c000000 48000000 44000000 40000000 3c000000 38000000 34000000 30000000
2c000000 28000000 24000000 14000000 11000000 0f000000 05000000 04000000
03000000 02000000 01000000 00000000
```

= **76, 72, 68, 64, 60, 56, 52, 48, 44, 40, 36, 20, 17, 15, 5, 4, 3, 2, 1, 0**

This confirms §9.1 exactly: same address, same count, same order, same element
type. The build's `POSITION_RATING_PENALTIES` is correct.

### 13.3 The rating is the best role, not a fixed one

The original sets `HighlightBestRoleDutyAutomatically: true`, so it does not use
one role per position. It scores **every** role and duty the player's best
position offers and shows the highest. That is the rule that reproduces its own
header, and it is now what the build does (`ratings.best_role`).

Pulisic's eight Winger roles, scored from the original's own
`rolesAndDuties.json` against his real attributes:

| Role | Duty | Rating |
|---|---|---|
| InsideForward | InPossession | 76.87 |
| InsideOutletWinger | OutOfPossession | 71.66 |
| InsideWinger | InPossession | 74.28 |
| PlaymakingWinger | InPossession | 73.18 |
| TrackingWinger | OutOfPossession | 67.85 |
| **WideForward** | **InPossession** | **77.49** ← the original's own header |
| WideOutletWinger | OutOfPossession | 71.66 |
| Winger | InPossession | 73.57 |
| Winger | OutOfPossession | 69.99 |

The winner is `WideForward` / `InPossession`, which is exactly the
**"Wide Forward (IP) 77.49 %"** the original displays. A fixed role per position
cannot produce that, and the old build's invented role names
(`CentralDefender`, `AdvancedForward`) do not exist in the original's file at
all — the real ones are `CentreBack` and `CentreForward`.

The arithmetic itself is unchanged and still correct: key attributes only, over
`count × 20`, held in a float32 and truncated to two decimals. 124 / 160 =
0.775 → float32 → 77.4999976 → **77.49**.

### 13.4 The role table is now the original's own

`app/fmrte/role_definitions.py` is generated verbatim from
`FMRTE\Ratings\rolesAndDuties.json` (SettingsVersion `27.0.0.0`): **10 general
positions, 77 roles, 88 duties**, every attribute name mapped to its snapshot
column with nothing unmapped. The build previously carried 9 hand-written roles
with invented names and guessed attribute lists.

### 13.5 The clamp helper, and what it actually clamps

`FUN_1404446b0` is a real function and does take `(value, minimum, maximum, …)`
and forward them to a virtual method. It has exactly **three** callers:

| Call site | Container | Arguments |
|---|---|---|
| `0x140402565` | `FUN_1404024a0` | `(obj, 1, lVar3, 0)` where `lVar3` is clamped to **1 … 0x7FFFFFF5** |
| `0x14043D82E` | `FUN_14043d2f1` | — |
| `0x14046B5C7` | `FUN_14046b58b` | `(param_2, 0, 1000, 0)` |

So the helper is a general range-clamp, and the ranges it is called with are
**1 … 2147483637** and **0 … 1000**. It is *not* the attribute/ability clamp.

**This corrects §9.5.** The `mov r8d, 0x14` / `0xC8` / `0x2710` sites at
`0x1411782B8`, `0x1411D0A48` and `0x14102D4E0` do **not** call `0x1404446B0` —
their `E8` targets are `0x1431F9760` and `0x1433F9760`, which are property
setters. At `0x1411782B8` the sequence is `mov r8d, 0x14` then
`call 0x1431F9760`, and that function forwards `param_3` into a property bag
(`FUN_1431fa090` stores it at `+8` of a boxed value). So `0x14` is a **value
being written**, not a maximum being enforced.

**Consequence for the build:** the 0…20 / 0…200 / 0…10000 ranges are still the
right ranges to use — they match the original's own settings and its presets —
but they are **not** proven to be enforced by that helper. They are marked
*observed from settings, not from a clamp*, and the sheet no longer claims
otherwise.

### 13.6 What is still not readable

- **The ladder's index.** The ladder's contents and its builder are confirmed,
  but the code that *indexes* it was not isolated. The build's rule (index by how
  far competence falls below the player's best, read from the far end) is
  **derived**, not lifted: it is the only reading that gives a player in his best
  position 0 and a player with no competence there 76, and it verifies over all
  42,344 players.
- **The rating arithmetic's own function.** No function references the 20.0 or
  100.0 constants (double or float) by RIP-relative addressing, and the weights
  are reached through virtual dispatch, so the multiplying function itself was
  not isolated. **The rule is no longer a guess, though:** the original's own
  settings help states the blend and its 50 % default, the compiled `0.5` at
  `+0x38` is `CaSharePercent`, and the blend reproduces the screen — see §16.6.
  What remains unread is the exact instruction sequence, not the formula.
- **`abilityWeights.json`.** The original loads it (`#/Assets/Ratings/abilityWeights.json`
  is in the binary) and it holds a per-position weight for every attribute. It
  does **not** reproduce the displayed rating: Pulisic's weighted Winger score is
  68.32, not 77.49. It therefore drives a different figure (most likely the
  positional/ability rating), and the build does not use it for the headline.

### 13.7 What this session added, and what it corrected

Re-opened the original with the built-in engine (SHA-256
`1da8b1891a029e526ee471be97aa2f1c0573e931bd55f4eb091ed886d546e93f`, the same
file) and re-read the ratings module function by function. Three things changed:

| Item | Before | Now |
|---|---|---|
| Weight store address | `0x14074032C` | **`0x140740455` / `0x1407403FB` / `0x140740410` / `0x14074042C`** — `0x14074032C` is NOP padding |
| Team-selection weights | JSON only | **also compiled in** as int32 immediates in `sub_1407417F0` |
| Ladder builder | `0x140741F50` | unchanged, re-confirmed byte for byte |

The four weight stores were located by scanning the file for the IEEE-754
immediates and then decoding the `mov r64, imm64` / `mov [rbx+disp8], r64` pairs
that follow them. They sit in a body the engine never discovered as a function
(it is entered only through the jump table at `0x146234688`), which is exactly
why the earlier pass could not name it and fell back to the nearest symbol.

**What changed since:** the arithmetic. §13.3's key-only reading fits Pulisic
only by coincidence; §16.6 shows the header is
`0.5 × CA/200 + 0.5 × roleScore`, so `ratings.role_rating()` now implements the
blend (Pulisic still reads **77.49 %**, because his key share and the blend are
both 0.775). Re-checked with `tools/check_rating.py`:

```
name     Christian Mate Pulisic
role     ('Winger', 'WideForward', 'InPossession')
rating   77.49
pref     69.99
weighted 75.99
```

**Still open, unchanged:** the ladder's index rule stays *derived and verified
against the original's output*; the blend's exact instruction sequence is
unread, although its inputs (`CaSharePercent` 0.5, 16/4/3 weights) and its
output are now confirmed from three directions.

---

## 12. The live write path (observed, from the person's own reader)

The person's complaint was that edits only reached a local copy. The fix is to
write into the game, and the offsets for that are **observed**, not guessed:
they come from the person's own working reader
(`fm-ai-assistant-2.1.0-SNAPSHOT-desktop.jar`, SHA-256
`9b64ffd7abcdd5700302d6c523a1a5fa353ae765af77707885cef29a4013e282`), which
reads this exact save out of the running game.

### 12.1 Where each offset was read

| Class | Method | What it gave |
|---|---|---|
| `memory.PlayerRecordLayout` | `current()` | the whole player layout |
| `memory.PlayerRecordLayout$Direct` | field list | 21 direct offsets |
| `memory.PlayerRecordLayout$Registration` | field list | 13 contract offsets |
| `player.AttributeDefinitions` | `<clinit>` | 14 positions, 54 attributes, 8 hidden |
| `linux.FmOffsets` | `<clinit>` | the 11 offset-table slots |
| `exporter.PlayerExporter` | `decodeRow` | how each byte becomes a shown value |
| `exporter.PlayerExporter` | `readDatePair` | the two-u16 date encoding |
| `exporter.PlayerExporter` | `salaryValues` | weekly wage × 52 = annual |
| `exporter.PlayerExporter` | `percentage` / `morale` | the 10000 and 100 ceilings |
| `exporter.PersonMemoryClassifier` | `<clinit>` | the player/staff shift, 248 |

### 12.2 The player record

```
person pointer P
  P - 36   current ability    i16      (PlayerRecordLayout$Direct.currentAbilityRel)
  P - 34   potential ability  i16
  P - 44   condition          u16      0..10000
  P - 48   match sharpness    u16      0..10000
  P - 46   jadedness          i16      signed
  P - 28   morale             u8       0..100
  P - 42   home reputation    u16
  P - 40   current reputation u16
  P - 38   world reputation   u16
  P - 84   asking price raw   u32
  P - 90   height in cm       u8
  P + 12   unique id          u32
  P + 168  registration ptr   u64
  P - 314  attribute block start
```

### 12.3 The attribute block

`AttributeDefinitions` gives every attribute a `FieldDef` offset. The block is
read from `P - 314` and an attribute sits at `block + (offset - 40)`, because
`SOURCE_OBJECT_BASE_OFFSET` is 40.

Two different storage scales are used, and this is the detail that makes the
numbers come out right:

| Group | Stored | Shown | Conversion |
|---|---|---|---|
| 14 positions | 1..20 | 1..20 | `direct20(v) = clamp(v, 1, 20)` |
| 54 visible attributes | 0..100 | 1..20 | `norm20(v) = clamp(round(v × 20 / 100), 1, 20)` |
| 8 hidden personality | 1..20 at `P + 112..119` | 1..20 | read directly |

So writing a shown 16 into `dribbling` means writing **80** into the block, and
writing a shown 16 into `striker` means writing **16**. `memmap.py` holds both
scales and `livewrite.LiveEditor._target()` picks the right one.

### 12.4 Contract fields

`P + 168` is a pointer to the registration record:

| Field | Offset | Encoding |
|---|---|---|
| weekly wage | +32 | u32, pounds per week |
| contract end | +72 | two u16: day of year (mask 511), then year |
| joined club | +76 | same date pair |
| transfer status | +87 | u8 |

Annual wage is `round(weekly × 52 / 1000) × 1000`, which is exactly what
`PlayerExporter.salaryValues` computes.

### 12.5 The write itself

`livewrite.LiveEditor.write()`:

1. re-reads the record and checks the unique id, so a stale pointer cannot
   change an unrelated object;
2. clamps the value to the range the original's own clamp helper uses;
3. writes with `WriteProcessMemory`; only if that is refused does it ask
   `VirtualProtectEx` for the minimal writable protection on that page (the
   execute bit is preserved, never added) and retry, restoring the old
   protection in a `finally` — a page it widened is never left widened;
4. reads the value back and only reports success when the game returned the
   value that was asked for;
5. keeps the previous bytes so the edit can be undone.

`db.Snapshot.set_value()` calls it whenever a game is attached, and
`saves/edits.json` is no longer applied at all — the screen always shows the
game's own values.

### 12.6 What is still not writable

| Field | Why |
|---|---|
| club ownership (move, swap, clone) | needs a new person object or a club link, not a value |
| national-team call-up | a team link, not a value |
| injury record | a separate object behind `P - 400`; only the flag is shown |
| bonuses, clauses, unhappiness | separate contract sub-records this save does not expose |
| club balance and budgets | the club record layout is known (`ClubRecordLayout.current()`: finance block at +336, balance at +12) but the club objects are not enumerated by this build yet |

## 8. Not implemented

- Parsing `.fm` save files directly (no save file was available on this machine).
- Writing back into the game process. Edits are local and reversible.
- Reading player attributes (Technical / Mental / Physical) from memory. The
  person's reader exposes CA/PA and the status fields at fixed offsets, but
  the attribute block's base and stride were not recovered, so attributes keep
  the snapshot's values while everything else refreshes live. Marked
  **estimated** in `mechanics.json`.
- The out-of-position penalty ladder is now **applied** (§2.3, §9.1): the role
  card and the Stats section show it. It is not yet used to re-rank players in
  the search results.
- The `teamSelection.json` weights are documented but not yet used to order a
  squad, because the original's combination function is not statically reachable
  (§9.4).
- The value clamps are **applied** (§9.5): `Snapshot.set_value()` runs every
  integer player edit through `ratings.clamp()`, so the build stores exactly
  what the original would. The clamp helper's own body is only partly readable
  (it dispatches through a virtual method), so the *ranges* are observed from
  its call sites while the clamping arithmetic itself is inferred from the
  argument order.

---

## 10. The Load Game crash, and what fixed it

The person reported: *"the app cannot load the game, it returned: argument3:
TypeError"*.

### 10.1 The cause (observed, reproduced)

`fmrte/live.py` declared the Windows module lookup like this:

```python
psapi.GetModuleInformation.argtypes = [
    w.HANDLE, w.HMODULE, ctypes.POINTER(ctypes.c_void_p), w.DWORD,
]
```

but the call passes `ctypes.byref(info)` where `info` is a `_ModuleInfo`
structure. ctypes type-checks every declared argument, so it refused the call
before Windows ever saw it:

```
ArgumentError: argument 3: TypeError: expected LP_c_void_p instance
               instead of pointer to _ModuleInfo
```

That is the person's `argument3: TypeError` exactly. It was reproduced with
`tools/probe_ctypes.py` against this process, and it fires on the first Load
Game press because `_module_base()` is the first thing that runs after the
process handle is opened.

### 10.2 The fix

| Change | Where |
|---|---|
| Third argument declared `c_void_p` (a `MODULEINFO*`), `restype` set to `BOOL` | `fmrte/live.py` |
| `_module_base()` returns `(0, 0)` on any ctypes failure instead of raising | `fmrte/live.py` |
| `integrity_level()`, `_window_pids()`, `GameProcess.read()` wrapped the same way | `fmrte/live.py` |
| `open_game()` / `read_live()` convert any leftover exception into a `LiveError` with a plain-language message | `fmrte/live.py` |
| The dialog's worker reports `"The game could not be read. Your snapshot still works."` instead of a raw exception | `fmrte/dialogs.py` |

### 10.3 Proof

`tools/test_module_enum.py` opens this process, enumerates its modules, finds
`python.exe` at a real base address, and confirms a missing module returns
`(0, 0)` rather than raising. `tests/test_snapshot.py::LiveReaderTests` runs the
same call as a unit test, so the exact regression cannot come back silently.

### 10.4 Why the live read still cannot run here

The game is running (`C:\XboxGames\Football Manager 27\Content\fm.exe`, pid
38432) at **Medium** integrity, but this editor's process runs at **Low**
integrity, so `OpenProcess` returns `ACCESS_DENIED`. The window now says so in
plain words and offers **Restart as administrator**. The memory chain itself
(offset table, player vector, record decode) is unchanged and untested against
a live process from this sandbox; the snapshot path is what the tests cover.

---

## 11. The test suite

`app/tests/` holds 49 tests, run with
`.venv\Scripts\python.exe -m unittest discover -s tests`:

| File | Covers |
|---|---|
| `test_snapshot.py` | the database (counts, metadata, uniqueness), Pulisic field by field against picture-2, search behaviour, AC Milan against picture-3, edits/undo/redo, the rating maths, and the live reader's failure modes |
| `test_ui.py` | the real Tk window: home page, status bar counts, search → open player, the player page's attributes, the club page, the Load Game dialog opening without a raw exception, "Use last snapshot", editing, and an unknown player |

The case tests assert the **exact** numbers from the person's screenshots, so a
data regression fails the suite instead of quietly showing a wrong value.

---

## 12. The Injury page and why Heal did not work

### 12.1 What the original's Heal preset actually writes

`Presets/Heal.json` is four actions, all `ValueSetType: "Replace"`,
`TargetType: "Player"`:

| TargetProperty | Value |
|---|---|
| `Condition` | 10000 |
| `MatchSharpness` | 10000 |
| `Jadedness` | 0 |
| `IsInjured` | **false** |

The first three are ordinary numbers. The fourth is not: in the game
`IsInjured` **is** the injury reference, not a separate boolean.

### 12.2 Where the injury lives

`PlayerRecordLayout` (FM AI Assistant 2.1.0, which reads this exact save) gives
the chain:

```
person - 0x190   -> injury reference (u64)
reference + 0x00 -> injury item (u64)
item + 0x08      -> injury type object
type + 0x20      -> the type's name string (u32 length, then UTF-8)
item + 0x20      -> start day of year (u16, masked with 511)
item + 0x22      -> start year (u16)
item + 0x28      -> total days until full training (u16)
item + 0x2A      -> total days until light training (u16)
```

`PlayerExporter.injuryStatus` decides "injured" from the pointer alone: a
non-zero reference whose first qword is readable. Its own comment records why
the high half of the pointer must not be used as a flag - past code required it
to equal 1 and silently dropped every injury allocated in the `0x2...` heap
region (6,267 cases in one live save).

### 12.3 The bug in this rebuild

The old `InjurySection._heal` wrote `condition`, `match_sharpness`,
`jadedness` and a local `injured` column. `injured` is a **snapshot column**,
not a game field: `livewrite._target()` has no address for it, so the write
either failed or only changed the local copy. The injury reference was never
touched, so the game - and therefore the next read - still reported the player
injured. That is exactly the symptom the person described.

### 12.4 The fix

| Change | Where |
|---|---|
| `INJURY_REFERENCE_REL = -0x190` and the five item offsets | `fmrte/memmap.py` |
| `LiveEditor.read_injury()` walks the pointer chain and decodes the name, date and both day counts | `fmrte/livewrite.py` |
| `LiveEditor.heal()` zeroes the reference, verifies the read-back, and keeps the old bytes for undo | `fmrte/livewrite.py` |
| `Snapshot.heal_player()` applies all four preset actions and reports what happened | `fmrte/db.py` |
| `Snapshot.refresh_injury()` reads the game's injury into the row before the page draws | `fmrte/db.py` |
| `InjurySection` reads first, then heals through `heal_player()` | `fmrte/pages/playersections.py` |

The item itself is **not** destroyed: only the reference is cleared, so Undo
puts the same injury back. `tests/test_livewrite.py::InjuryTests` pins the read,
the heal, the undo, the refused-write rollback and the already-fit case.

---

## 13. The AI Assistant

### 13.1 What was ported, and from where

The person's FM AI Assistant (`fm-ai-assistant`, 2.1.0-SNAPSHOT) is a Spring
Boot service. Three of its parts belong in an editor, and all three are ported
as behaviour, not as code:

| Part | Source | What this build does |
|---|---|---|
| Provider transport | `commandcode/CommandCodeClient.java` | OpenAI-compatible `POST /chat/completions`, same default base URL and model, same status messages |
| Three-layer prompt | `ai/CompositeAiPromptContext.java` | fixed instructions + always-on conclusions + the save's own facts, then the user's words |
| Key storage | `commandcode/CommandCodeKeyStore.java` | Windows DPAPI in the current-user scope, never plain text, never in the save or a log |
| Tactic shape | `tactic/TacticShape.java`, `docs/fm27-tactic-files.md` | 22 role records, 14-byte header, 24-byte options, 12-byte post; the formation is derived from the eleven in-possession slots |
| Training ids | `schedule/TrainingActivity.java`, `docs/fm27-training-schedules.md` | the measured identifier table, grouped by trainer tab |

### 13.2 The provider defaults (observed)

| Value | Source |
|---|---|
| Base URL `https://api.commandcode.ai/provider/v1` | `CommandCodeClient.DEFAULT_BASE_URL` |
| Model `deepseek/deepseek-v4.1-flash` | `application.properties` |
| Timeout 180 s | `CommandCodeClient` `Duration.ofSeconds(180)` |
| Max response 2 MiB, max catalog 8 MiB | `MAX_RESPONSE_BYTES`, `MAX_CATALOG_BYTES` |
| Key file `commandcode.key`, max 64 KiB | `CommandCodeKeyStore` |
| `CRYPTPROTECT_UI_FORBIDDEN = 0x1` | `CommandCodeKeyStore` |

The status messages are the assistant's own, so a failure reads the same way:
401 invalid key, 402 insufficient credits, 403 access denied, 404 no such model,
429 rate limit.

### 13.3 The prompt envelope (observed, verbatim shape)

```
The following Football Manager 2027 context was loaded locally by the application.
Treat it as factual context for the current save. Use it when relevant, and do not
invent values that are absent.

<knowledge conclusions>

<the save's own facts>

User message:
<the person's words>
```

The always-on list is deliberately short (three entries here) because every
message pays for it; the rest of the knowledge is meant to be fetched on demand.

### 13.4 Tactic plans

A plan is built from a template the game itself wrote (`Tactics/*.json`: a name
and eleven position slots) plus a role and duty for each slot from the role
catalogue. The formation is **derived**, never stored: count the outfield lines
of the eleven in-possession slots and drop empty ones, so 4 defenders, 2
holding, 3 attacking and 1 striker reads `4-2-3-1`.

This build does **not** write `.fmf` tactic files. The assistant's own
`docs/fm27-tactic-files.md` section 8 explains why: a role record's option table
is not a static per-position catalogue (the same DCR slot has 15 options in one
export and 2 in another), so a record can only be copied from a file the game
wrote. A plan is therefore a sheet the person sets up in the game.

### 13.5 Training plans

The identifiers are the assistant's measured table, not guesses:

| id | Activity | FM27 label | Tab |
|---|---|---|---|
| 0-7 | tactical understanding sessions | 传球场景 … 无球队形 | Tactical understanding |
| 101-102 | match preparation | 传球推进, 进攻三区 | Match preparation |
| 200-203 | ball skills | 个人培养 … 控球训练 | Ball skills |
| 300-301 | rest, recovery | 休息, 恢复 | Physical and recovery |
| 400 | team bonding | 团队配合 | Extra activities |
| 500 | match | 比赛 | Match day |

A week is seven day records of three sessions each. There is **no intensity
field** in the payload: nine archives from three sources all carry the same
14-byte prefix, `marker = 4` and `trailing = 0` on every day, so the intensity
bar FM27 draws is derived from the day's activities.

### 13.6 What the assistant does not do

- It never writes into the game. It proposes; the person applies.
- It does not parse `.fm` saves, and it does not read the game's memory itself:
  it answers from the snapshot this editor already holds.
- It does not ship a key. The person brings their own, and without one the
  panel says so instead of failing silently.

---

## 14. The interface, measured from the reference shots

The window is laid out at **1665 × 998 logical pixels** (the round-8 size), so a
150 % display gives 2498 × 1541 — within 1 px of the round-8 check's 2497 × 1541.
The original's `InterfaceScale` (1.1) scales text and spacing inside that window;
it does not scale the window itself.

| Token | Value | Where it was measured |
|---|---|---|
| Accent blue | `#1f5fdb` | blue band and header, 2 860 sampled pixels |
| Page / toolbar | `#f4f6f9` | the largest light surface |
| Navigation rail | `#eceff3` | 355 px wide at 2497 px (round 8) |
| Card border | `#e2e7ee` | 1 px card outline |
| Load Game gradient | `#d5008c` at the left → `#f60052` at the right | button, 5 781 and 5 705 pixels |
| Blue band / entity header | 79 px at 2497 px wide (round 8) | round-8 screenshot |
| Home cards | 378 → 1856 px, hero 276 → 616 px | round-8 screenshot |
| Player columns | 299 / 851 / 1218 / 1577 / 1941 px, right edge 2229 | picture-2 |
| Sidebar row | 27 px, active row `#e6edfd` with blue text | pictures 1–5 |

Fonts are Segoe UI, which has a near-identical x-height to the Noto Sans the
original bundles. The pink Load Game button is painted as a horizontal gradient
on a canvas, because Tk's native button cannot carry two colours.

The club facts the snapshot does not carry — year founded, stadium, attendance
figures, colours, chairman status, scouting range/budget — are **display-only
samples read from reference picture 3** and are kept in `CLUB_FACTS` in
`app/fmrte/pages/club.py`. The real numbers in this save (reputation,
facilities, competition, staff counts) come from the snapshot.

The screenshot path crops the dark frame PrintWindow puts around the window, so
the saved picture is the app's own surface. It does not add anything that is not
on screen.

---

## 16. Deep-mode decompilation, function by function (this pass)

### 16.0 How these functions were read

- Target: `FMRTE\current\FMRTE.exe`, SHA-256
  `1da8b1891a029e526ee471be97aa2f1c0573e931bd55f4eb091ed886d546e93f`,
  127,948,768 bytes. Opened read-only; the original was never run or written.
- Engine: **Ghidra 12.1.4** headless, **JDK 21.0.9** (the JDK beside Ghidra).
  Ghidra's user settings had to live inside the workspace (`USERPROFILE` /
  `APPDATA` redirected) because the session's file sandbox denies writes to the
  real `%APPDATA%\ghidra`, which is what had made every earlier launch die
  before writing a log.
- Full auto-analysis of 122 MB does not converge (it was still running after
  20 minutes and 675 s of CPU). The working route is a **64 KB `.text` slice**,
  `0x140740000`–`0x140750000`, imported as a raw binary at that base. The
  obfuscated switches jump through tables in `.rdata`, so those 4 KB
  (`0x146234000`) were added to the program as a memory block *before*
  analysis. With the tables present, Ghidra recovers each switch and the
  decompiler produces real C instead of `goto *rax`.
- The same functions were also read with `native_function_pseudocode` and
  `native_function_disassemble`; the listing below is Ghidra's C, and the
  addresses match the built-in engine's exact disassembly.
- Raw output: `app/tools/ghidra_out/0x…c`; slice and tables:
  `app/tools/ratings_slice.bin`, `app/tools/jumpdata.bin`.

| Address | Size | Ghidra name | What it is | Established |
|---|---|---|---|---|
| `0x140740130` | 82 B | `FUN_140740130` | default constructor | `+0x18` empty ladder, `+0x38` = 0.5, `+0x10` new list |
| `0x140740340` | 108 B | `FUN_140740340` | full constructor | builds roles, installs ladder at `+0x18`, then 16/4/3/0.5 |
| `0x1407404C0` | 279 B | `FUN_1407404c0` | settings validator | ladder must be 20; weights ≥ 0; sum > 0 |
| `0x1407417F0` | 50 B | `FUN_1407417f0` | team-selection defaults | seven int32 weights packed into `+0x10…+0x28` |
| `0x140741DE0` | 310 B | `FUN_140741de0` | group-share validator | three group totals must each be exactly 100 |
| `0x140741F50` | 56 B | `FUN_140741f50` | penalty-ladder builder | allocates `int[20]`, copies `0x50` bytes from `0x14517BB38` |
| `0x140741FA0` | 791 B | `FUN_140741fa0` | role-definition builder | fills the roles/positions collections |
| `0x140742A00` | 92 B | `FUN_140742a00` | position dispatch | calls `0x140743520`, then one of ten per-position builders |

### 16.1 The role-weights constructor (`0x140740340`) and its defaults (`0x140740130`)

> **Re-verified in §17.** The field map below is right; the constant
> `ratings.ROLE_WEIGHTS_OBJECT` had `+0x10` and `+0x18` the other way round and
> was corrected. §17 reads the accessors' bytes, which is what settles it.

Ghidra's C for `0x140740340`, with the obfuscated switch flattened away:

```c
void FUN_140740340(longlong param_1)
{
  lVar3 = func_0x000143441de0(0x14521c1a8);   // new settings object
  func_0x000140741fa0(lVar3);                 // build roles/positions
  ...
  uVar4 = func_0x000140741f50();              // build int[20] ladder
  func_0x000143442410(param_1 + 0x18, uVar4); // this->PositionRatingPenalties
  func_0x000143442410(param_1 + 0x10,
                      *(undefined8 *)(lVar3 + 0x10)); // roles list
  *(undefined8 *)(param_1 + 0x20) = 0x4030000000000000;  // 16.0
  *(undefined8 *)(param_1 + 0x28) = 0x4010000000000000;  //  4.0
  *(undefined8 *)(param_1 + 0x30) = 0x4008000000000000;  //  3.0
  *(undefined8 *)(param_1 + 0x38) = 0x3fe0000000000000;  //  0.5
}
```

The default constructor `0x140740130` does the same for an empty object:

```c
*(undefined8 *)(param_1 + 0x18) = 0x14721f280;          // empty/static ladder
*(undefined8 *)(param_1 + 0x38) = 0x3fe0000000000000;   // 0.5 CaSharePercent
uVar1 = func_0x000143441de0(0x1455a5b68);
func_0x0001425f1ee0(uVar1, 0, 0);
func_0x000143442410(param_1 + 0x10, uVar1);             // new list
```

**Correction to §13.1.** `0x14074035D` does **not** call the ladder builder
`0x140741F50`; it calls the role builder `0x140741FA0`. The ladder builder is
called later, inside the dispatched body, and its return is stored at `+0x18`.
That is why `sub_140740340`'s callee list shows `0x140741FA0` while the ladder
appears only when the switch case runs. The field map itself (ladder at `+0x18`,
then 16/4/3/0.5) is unchanged and now proven by the C above.

**Build use:** `KEY_ATTRIBUTES_WEIGHT = 16`, `PREFERABLE_ATTRIBUTES_WEIGHT = 4`,
`AVOID_ATTRIBUTES_WEIGHT = 3` and `CA_SHARE = 0.5` in
`app/fmrte/ratings.py`/`role_definitions.py`, with these addresses in their
comments.

### 16.2 The penalty ladder (`0x140741F50`, data at `0x14517BB38`)

```c
longlong FUN_140741f50(void)
{
  longlong lVar1 = func_0x000143441f00(0x14619acd0, 0x14); // int[20]
  func_0x00014215cbc0(lVar1 + 0x10, 0x14517bb38, 0x50);    // 80 bytes
  return lVar1;
}
```

The 20 int32s read back exactly:
`76, 72, 68, 64, 60, 56, 52, 48, 44, 40, 36, 20, 17, 15, 5, 4, 3, 2, 1, 0`.
`PENALTY_LADDER_LENGTH = 20` and `POSITION_RATING_PENALTIES` in the build carry
this; `position_penalty()` refuses to index a ladder of any other length.

### 16.3 The settings validator (`0x1407404C0`)

```c
if (*(longlong *)(param_1 + 0x10) == 0) return 0;
if (func_0x00014325cd50(0x1450700b8) == 0) return 0;
if (func_0x0001407406c0(param_1) == 0) return 0;
lVar7 = *(longlong *)(param_1 + 0x18);
if (lVar7 == 0 || *(int *)(lVar7 + 8) != 0x14) return 0;   // 20 entries
...
if (*(double *)(param_1 + 0x20) < 0.0) return 0;           // key weight >= 0
if (*(double *)(param_1 + 0x28) < 0.0) return 0;           // preferable >= 0
if (*(double *)(param_1 + 0x30) < 0.0) return 0;           // avoid >= 0
if (*(double *)(param_1 + 0x20) + *(double *)(param_1 + 0x28)
    + *(double *)(param_1 + 0x30) <= 0.0) return 0;        // sum > 0
```

This is the original's own rule for a *valid* role-weights object: a 20-entry
ladder, no negative weight, and at least one positive weight. It is the source
of the `20` in `PENALTY_LADDER_LENGTH` (the count check is in this function and
in the builder above, i.e. two independent code sites).

### 16.4 The three group shares (`0x140741DE0`)

```c
iVar3 = func_0x000143347f20(0x144fcc730, *(undefined8 *)(param_1 + 0x10), lVar6);
iVar4 = func_0x000143347f20(0x144fcc730, *(undefined8 *)(param_1 + 0x18), lVar6);
iVar5 = func_0x000143347f20(0x144fcc730, *(undefined8 *)(param_1 + 0x20), lVar6);
if (iVar3 == 100 && iVar4 == 100) return iVar5 == 100;
return false;
```

Three groups (key / preferable / avoid) whose shares must **each total 100**.
The reflection names confirm the three groups and the other settings:
`KeyGroupShare`, `PreferableGroupShare`, `AvoidGroupShare`,
`KeyAttributesWeight`, `PreferableAttributesWeight`, `AvoidAttributesWeight`,
`CaSharePercent` (strings at `0x14368fba9`–`0x143690346`). This rule is recorded
in `mechanics.json` as "role weight group total = 100" even though the build
does not expose a weights editor yet; it is what the original refuses to save.

### 16.5 The clamp helper, restated

`0x1404446B0` is a four-argument wrapper that forwards `(value, min, max, flag)`
through a virtual method; its three callers clamp to `1…0x7FFFFFF5` and
`0…1000`, **not** to attribute/ability ranges. The `mov r8d, 0x14 / 0xC8 /
0x2710` sites are arguments to property setters (`0x1431F9760`, `0x1433F9760`),
so the 20/200/10000 ranges are observed from the setters and the presets, not
proven by that helper. §5 has been corrected accordingly.

### 16.6 The rating formula (`0x14074042C` + the original's own help text)

The rating's multiply was never isolated as a single function, but its inputs
and its output are now pinned from three directions:

1. **The field and its default.** `+0x38` is written with the double `0.5` by
   `0x140740130` (`0x140740144`–`0x14074014E`) and again by `0x14074042C` /
   `0x140740436` in the full constructor. The reflection metadata names that
   field `CaSharePercent` (getter `0x140740110`, setter `0x140740120`).
2. **The original's own explanation.** `english.json`:
   *"How much CA counts in Actual Rating and Potential Rating. The rest comes
   from the player's best role. Default: 50%."*
3. **The arithmetic on the person's real player.** Pulisic: CA 158 → 0.79;
   16/4 role score 0.76; blend **0.775**; as a float32 0.774999976; truncated
   **77.49 %**, the original's header exactly.

The formula and build use are in §2.3. The important correction is that the
preferable attributes **are** part of the displayed rating (weight 4), and CA is
half of it (weight 0.5); the old key-only reading fitted Pulisic only because
`124/160` and the blend are both 0.775.

### 16.7 Team-selection weights (`0x1407417F0`)

```c
*(undefined8 *)(param_1 + 0x10) = 0x5000000096;  // 150, 80
*(undefined8 *)(param_1 + 0x18) = 0x5a00000078;  // 120, 90
*(undefined8 *)(param_1 + 0x20) = 0x2300000050;  // 80, 35
*(undefined4 *)(param_1 + 0x28) = 0xf;           // 15
```

Seven int32s, exactly `Ratings/teamSelection.json`:
**150, 80, 120, 90, 80, 35, 15**. The build records them as
`TEAM_SELECTION_WEIGHTS` with the field map; it does not yet have a squad
selector to spend them on, so they are documented rather than used.

### 16.8 What is still not decompiled

- **The ladder's index.** Contents, builder, length and validator are proven;
  the code that turns a player's position competence into an index is reached
  through virtual dispatch and was not isolated. The build's rule remains
  **derived** and is checked over all 42,344 players (best position = 0).
- **The rating's instruction sequence.** The blend is proven by the settings
  text, the compiled 0.5 and the output, but the exact `mulsd`/`addsd` function
  was not found. The formula is therefore *observed + verified*, not lifted
  instruction by instruction.
- **`abilityWeights.json`.** Loaded (`#/Assets/Ratings/abilityWeights.json`),
  a per-position attribute weight table, but it gives Pulisic 68.32 %, not the
  header's 77.49 %, so it drives a different figure (position ability) and is
  not used for the headline.
- **The read/write wrappers** (`0x1408410A0`, `0x140841320`) lie outside the
  64 KB slice; they stay as documented in §4/§9.3.

---

## 17. Second pass: the role-weights object, instruction by instruction

This pass re-read the settings object with Craft Anything's own native tools
rather than the sliced-Ghidra route, corrected one wrong slot, and found three
rules that were not in this sheet before. Nothing here is a guess; every claim
below cites an address whose bytes were read.

### 17.0 How this pass was done, and what the tools could and could not do

| Step | Tool | Result |
|---|---|---|
| Open the original | `core_target_open` | `current\FMRTE.exe`, 127,948,768 bytes, SHA-256 `1da8b1891a029e526ee471be97aa2f1c0573e931bd55f4eb091ed886d546e93f` |
| Names, settings, messages | `native_strings_find` | `KeyAttributesWeight` 14 hits, `TeamSelection` 57, `rolesAndDuties` 9 |
| Follow the references | `native_xrefs_list`, `native_function_callers_list` | data references work; getters have **no** callers |
| Read the code | `native_bytes_show`, `native_function_pseudocode` | bytes exact; the dispatch pseudo-code is not |
| Decompiler | `native_function_pseudocode` limitation `deep_engine_unavailable` | Ghidra **cannot** open the 122 MB image in the managed engine, so the built-in lifter ran |

Two limits are worth recording because they decide how a future pass should
work:

1. **The NativeAOT metadata strings cannot be followed into code.**
   `0x14368FC73` is
   `Fmrte.Mvvm.ViewModels.Settings.Ratings.RoleWeightsSettingsViewModel,Fmrte.Mvvm.KeyAttributesWeight!Getter`,
   yet `native_xrefs_list` there returns **0** references. NativeAOT stores the
   characters behind a `StringLiteral` descriptor, so nothing points at the
   character data itself. The strings therefore give the **names** — exactly
   what was asked — but the *slots* have to come from the accessors.
2. **The getters are never called directly.** `native_function_callers_list` on
   `0x140740090` (get_KeyAttributesWeight) and `0x1407400F0`
   (get_PositionRatingPenalties) both return 0: the calls are virtual, through
   method tables. The call graph is a dead end here; the accessor bodies are not.

The built-in lifter's **bytes and disassembly are exact** and its pseudo-code is
not, for this obfuscated field dispatch — which is what `HANDOFF.md` §15 already
warns about. Everything below is therefore quoted from bytes, not from the
lifter's C.

### 17.1 The field map, proven by the accessors (one correction)

`native_bytes_show` at `0x140740090`, 160 bytes, decodes as ten tiny functions.
Each is 5–8 bytes and reads or writes exactly one slot:

| Address | Instruction | Slot | Meaning |
|---|---|---|---|
| `0x140740090` | `movsd xmm0,[rcx+0x20]` | +0x20 | get KeyAttributesWeight |
| `0x1407400A0` | `movsd [rcx+0x20],xmm1` | +0x20 | set KeyAttributesWeight |
| `0x1407400B0` | `movsd xmm0,[rcx+0x28]` | +0x28 | get PreferableAttributesWeight |
| `0x1407400C0` | `movsd [rcx+0x28],xmm1` | +0x28 | set PreferableAttributesWeight |
| `0x1407400D0` | `movsd xmm0,[rcx+0x30]` | +0x30 | get AvoidAttributesWeight |
| `0x1407400E0` | `movsd [rcx+0x30],xmm1` | +0x30 | set AvoidAttributesWeight |
| `0x1407400F0` | `mov rax,[rcx+0x18]` | **+0x18** | get PositionRatingPenalties |
| `0x140740100` | `lea rcx,[rcx+0x18]` + copy-ctor call | **+0x18** | set PositionRatingPenalties |
| `0x140740110` | `movsd xmm0,[rcx+0x38]` | +0x38 | get CaShare |
| `0x140740120` | `movsd [rcx+0x38],xmm1` | +0x38 | set CaShare |

**The correction.** §2.3 and §16.1 of this sheet already put the ladder at
`+0x18`, but `ratings.ROLE_WEIGHTS_OBJECT` had `+0x10 = PositionRatingPenalties`
and `+0x18 = GeneralPositionToRoleAndDutiesDefinitionsMap`. That was backwards.
Three independent readings agree on `+0x18` being the ladder:

- the getter at `0x1407400F0` returns `[rcx+0x18]`;
- the setter at `0x140740100` passes `lea rcx,[rcx+0x18]`;
- the validator at `0x1407404C0` loads `[param_1+0x18]` and rejects it unless
  `*(int *)(array + 8) == 0x14`.

`+0x10` is the role map: the initialiser copies `[source+0x10]` into `[rbx+0x10]`
at `0x140740441`/`0x140740445`, and that source is the object built by
`0x140741FA0`, which fills a dictionary of role→duties.

**Two code paths touch the object**, and they do different things:

- `0x140740130` (82 bytes) is the plain constructor. It stores a frozen
  `int[]` into `+0x18` (first word `0x14619ACD0` = the int[] method table that
  `0x140741F50` also passes to the allocator, second word 0 = length), `0.5` into
  `+0x38`, and a fresh dictionary into `+0x10`. So on its own it leaves the
  ladder **empty** and sets only the CA share.
- `0x140740380` is the field dispatch that installs the real defaults. It
  switches a small field index through three tables — `0x146234688`,
  `0x146234694`, `0x1462346A0` — all adding the base `0x14074034B`, and each case
  stores one field. This is where the numbers are.

The stores, with the encoding read out of `0x1407403F0` (176 bytes):

| Slot | Value | Load | Store | Encoding detail |
|---|---|---|---|---|
| +0x28 | 4.0 | `0x1407403FB` | `0x140740405` | `48 B9 <imm64> 40 10 00…` then `48 89 4B 28` |
| +0x30 | 3.0 | `0x140740410` | `0x14074041A` | `48 B9` … `48 89 4B 30` |
| +0x38 | 0.5 | `0x14074042C` | `0x140740436` | `48 B9` … `48 89 4B 38` |
| +0x20 | 16.0 | `0x140740455` | `0x14074045F` | `48 B8` … `48 89 43 20` |
| +0x18 | int[20] | `call 0x140741F50` at `0x14074046A` | `0x14074046F` | `lea rcx,[rbx+0x18]` |

**A trap worth knowing.** Three of the four constants load through `rcx`
(`48 B9`) and only 16.0 through `rax` (`48 B8`). A byte search for
`48 B8 00 00 00 00 00 00 E0 3F` (which is what a "find the 0.5" search looks
like) returns **two** hits at `0x140077B6F` and `0x141844D73` and **misses
`0x14074042C` entirely**. Searching `48 B9` as well is what proves the store.

### 17.2 The validator `0x1407404C0`: six conditions, not two

`tools/ghidra_out/0x1407404c0.c` (279 bytes) decompiled cleanly enough to read
the conditions in order. The object is accepted only when all of these hold:

| # | Test | What it means |
|---|---|---|
| 1 | `*(longlong *)(param_1 + 0x10) != 0` | the role map must exist |
| 2 | `sub_1407406C0(param_1) != 0` | a companion check on the same object |
| 3 | `+0x18 != 0 && *(int *)(+0x18 + 8) == 0x14` | the ladder must hold **20** entries |
| 4 | `0x1431FEF00(+0x18, comparer) != 0` | the ladder must satisfy a comparer/equality check |
| 5 | `+0x20 >= 0.0 && +0x28 >= 0.0 && +0x30 >= 0.0` | no weight may be negative |
| 6 | `+0x20 + +0x28 + +0x30 > 0.0` | **the three weights must sum above zero** |

Two facts here were not in this sheet before:

- **The sum rule.** A settings object with three zero weights is rejected even
  though none is negative, because the blend would divide by zero.
- **CaShare is not validated.** `0x1407404C0` never touches `+0x38`, so the
  original accepts a CA share outside 0..1. The build now mirrors that: it does
  not "fix" a CA share the original would accept.

### 17.3 The group validator `0x140741DE0` (confirmed)

`tools/ghidra_out/0x140741de0.c` converts three collections at `+0x10`, `+0x18`
and `+0x20` to `int` and returns `iVar3 == 100 && iVar4 == 100 && iVar5 == 100`.
So each of the three role-weight groups must total exactly **100** (already
recorded as `role weight group total` in `mechanics.json`); this pass confirms
the address and that the comparison is `== 0x64`, not `<=`.

### 17.4 `AvoidAttributesWeight` = 3 is real but does not enter the header

This was an open worry: the settings validate a third weight of 3, the build
carries `AVOID_ATTRIBUTES_WEIGHT`, and §2.3's formula uses only key and
preferable. Two independent readings settle it.

**The data has no avoid group.** Every duty in
`Ratings/rolesAndDuties.json` — e.g. `Winger → WideForward → InPossession` — is
a list of `{"Name": …, "KeyAttribute": true|false}`. There are exactly two
groups: `true` (key) and `false` (preferable). No third group exists, so the
weight of 3 has nothing to act on for a player rating.

**The arithmetic forbids it.** Pulisic's header is exactly

```
0.5 * 158/200 + 0.5 * 0.76 = 0.5 * 0.79 + 0.5 * 0.76 = 0.775  ->  77.49 %
```

Any positive avoid term with a non-zero attribute average shifts that number:
weighting it as `(key*16 + pref*4 − avoid*3) / 23` would need an avoid average
of about 95 out of 20 to still print 77.49, which is impossible. So the avoid
weight is part of the settings schema (it is validated, it is persisted) but it
does not scale the displayed figure for this role file.

`test_rolegeights.py` now asserts both halves of this: that every duty has
exactly two attribute groups, and that Pulisic's blend is 0.775 → 77.49.

### 17.5 What changed in the build

| Change | File | Why |
|---|---|---|
| `+0x10` / `+0x18` swapped back into their real slots | `fmrte/ratings.py` | the accessors and the validator both say `+0x18` is the ladder |
| `ROLE_WEIGHTS_ACCESSORS`, `ROLE_WEIGHTS_OBJECT` gains `constructor` / `jump_table_bases` / `jump_base` | `fmrte/ratings.py` | a future reader can check a slot against its instruction instead of a comment |
| `validate_role_weights()` implementing the six checks of `0x1407404C0` | `fmrte/ratings.py` | the original's own acceptance rule, now executable |
| `load_role_weights()` / `role_weights()` / `set_role_weights()` | `fmrte/ratings.py` | the weights are read from the object at run time; `FMRTE_ROLE_WEIGHTS` may point at the original's `ratings/rolesAndDuties.json`, and a changed file then changes the rating exactly as it changes the original's |
| the role score and the CA blend now read `key_weight()`, `preferable_weight()`, `ca_share()` | `fmrte/ratings.py` | the formula uses the loaded values, not frozen literals |
| `tests/test_rolegeights.py`, 21 tests | `app/tests` | every claim above is asserted against the snapshot and the file |

The defaults are unchanged, so the numbers the person already sees do not move:
Pulisic is still `Winger / WideForward / InPossession`, **77.49 %**, role half
0.76, key 77.49, preferable 69.99. A test pins that.

### 17.6 What is still not decompiled

- **The blend's instruction sequence.** The formula is proven by the original's
  own help text, the compiled `0.5`, and 42,344 outputs, not by a located
  `mulsd/addsd`. If a future pass finds it, check it against §2.3 first.
- **The ladder's index rule.** The 20 values and their slot are proven; the code
  that turns a position competence into an index is still not isolated
  (`position_penalty()` remains **derived**).
- **The attribute block base and stride.** Still the reason
  `live attribute refresh` is `false` in `mechanics.json`.
- **The read/write wrappers** `0x1408410A0` / `0x140841320`: outside the slice,
  unchanged from §4.2 / §9.3.

---

## 18. Third pass: the settings object and its validators, read again with Ghidra

This pass re-opened the original in a fresh session, followed its metadata names
to the settings object, and re-read every function behind it with the deep
engine. It confirms §16/§17 slot by slot, adds one function those passes never
read (`0x1407406C0`), and pins the original's own type map for the rating.

### 18.0 How this pass was done, including one tool limitation

| Step | Tool | Result |
|---|---|---|
| Open the original | `core_target_open` | `FMRTE.exe`, 127,948,768 bytes, SHA-256 `1da8b1891a029e526ee471be97aa2f1c0573e931bd55f4eb091ed886d546e93f` |
| Names and settings | `native_strings_find` | `CaSharePercent` 4, `PositionRatingPenalt` 3, `rolesAndDuties` 9, `PlayerRating` 29, `global__Fmrte_Framework_Scout*` 23 |
| Follow the references | `native_xrefs_list`, `native_function_callers_list` | the ladder data `0x14517BB38` has **one** reference, from inside its builder; the accessors have **no** direct callers (reflection/vtable) |
| Read each function | `native_function_pseudocode` | returns C for every address, but with a `deep_engine_unavailable` limitation |
| Deep decompilation | Ghidra 12.1.4 headless | the real C quoted below |

**The limitation, stated plainly.** In this session the MCP's own deep engine
could not start: Ghidra writes its settings under `%APPDATA%\ghidra`, and that
path is read-only to the sandboxed app process, so project creation fails and
`native_function_pseudocode` falls back to the built-in clean-room lifter
(`ev_2655a160…`, `ev_ab6f6e73…`, `ev_ccf37ff6…`, `ev_d0f2953d…`, `ev_72ce1601…`,
`ev_a0d47c51…`, `ev_8578c17f…` all carry it). The lifter cannot resolve this
binary's control-flow flattening: every one of these functions is dispatched
through a jump table fed by `sub_1407fa300` / `sub_140800d30` /
`sub_14081f330` / `sub_140826e60`, so the lifter's C is a `goto *rax` loop with
the real stores missing.

The same Ghidra 12.1.4 was therefore run headless with `USERPROFILE` and
`APPDATA` redirected into `app/tools/ghhome`, importing a 64 KB slice of `.text`
at base `0x140740000` (`app/tools/ratings_slice.bin`). That is the deep engine
this build requires, launched by hand; the C below is its output and the
addresses in it are the original's.

The functions read, with their sizes:

| Address | Size | Role | Where |
|---|---|---|---|
| `0x140740070` | 5 B | getter for `+0x10` (the positions→roles dictionary) | §18.1 |
| `0x140740130` | 82 B | plain constructor | §18.1 |
| `0x140740340` | 108 B | full constructor | §18.1 |
| `0x1407404C0` | 279 B | settings validator | §18.3 |
| `0x1407406C0` | 151 B | **positions/duties completeness validator** (new) | §18.3 |
| `0x1407417F0` | 50 B | team-selection defaults | §18.4 |
| `0x140741DE0` | 310 B | attribute-group share validator | §18.3 |
| `0x140741F50` | 56 B | penalty-ladder builder | §18.2 |
| `0x140741FA0` | 791 B | positions→roles→duties builder | §18.5 |
| `0x140742A00` | 92 B | position dispatch | calls `0x140743520` then dispatches |

### 18.1 The object, slot by slot (observed)

`0x140740340` is the full constructor. Flattened, its body is exactly six
stores, and each one names its slot:

```c
lVar3 = func_0x000143441de0(0x14521c1a8);
func_0x000140741fa0(lVar3);                       // build positions -> roles -> duties
...
func_0x000143442410(param_1 + 0x10, *(undefined8 *)(lVar3 + 0x10));   // +0x10 roles dictionary
uVar4 = func_0x000140741f50();
func_0x000143442410(param_1 + 0x18, uVar4);       // +0x18 the 20-entry ladder
*(undefined8 *)(param_1 + 0x20) = 0x4030000000000000;   // +0x20 16.0
*(undefined8 *)(param_1 + 0x28) = 0x4010000000000000;   // +0x28  4.0
*(undefined8 *)(param_1 + 0x30) = 0x4008000000000000;   // +0x30  3.0
*(undefined8 *)(param_1 + 0x38) = 0x3fe0000000000000;   // +0x38  0.5  CaShare
```

The plain constructor `0x140740130` installs the *empty* state instead:

```c
*(undefined8 *)(rbx + 0x18) = &data_14721f280;    // frozen empty ladder
*(undefined8 *)(rbx + 0x38) = 0x3fe0000000000000; // 0.5
rsi = func_0x000143441de0(&data_1455a5b68);
func_0x0001425f1ee0(rsi, 0, 0);                   // new Dictionary(0, 0)
func_0x000143442410(rbx + 0x10, rsi);             // +0x10
```

So the two-constructor pair is "empty object, then defaults": `+0x18` starts as
a shared frozen object and only the full constructor replaces it with a fresh
mutable `int[20]`.

**This is the cleanest statement of the slot map**, and the first time the
values were read out of Ghidra's C for this object rather than out of
instruction bytes:

| Offset | Field | Value | Stored at | Getter / setter |
|---|---|---|---|---|
| `+0x10` | positions→roles dictionary | built by `0x140741FA0` | `0x140740340` | `0x140740070` |
| `+0x18` | `PositionRatingPenalties` | `int[20]` from `0x14517BB38` | `0x140740340` | `0x1407400F0` / `0x140740100` |
| `+0x20` | `KeyAttributesWeight` | **16.0** | `0x140740455` | `0x140740090` / `0x1407400A0` |
| `+0x28` | `PreferableAttributesWeight` | **4.0** | `0x1407403FB` | `0x1407400B0` / `0x1407400C0` |
| `+0x30` | `AvoidAttributesWeight` | **3.0** | `0x140740410` | `0x1407400D0` / `0x1407400E0` |
| `+0x38` | `CaSharePercent` | **0.5** | `0x14074042C` | `0x140740110` / `0x140740120` |

### 18.2 The penalty ladder (`0x140741F50`, data at `0x14517BB38`)

The builder is four instructions of real work:

```c
uVar4 = func_0x000143441f00(&data_14619acd0, 0x14);  // allocate 20 elements
func_0x00014215cbc0(uVar4 + 0x10, &data_14517bb38, 0x50);  // copy 80 bytes
```

Read directly, the 80 bytes at `0x14517BB38` are twenty int32s
(`ev_86a91d2e…`):

```
76, 72, 68, 64, 60, 56, 52, 48, 44, 40, 36, 20, 17, 15, 5, 4, 3, 2, 1, 0
```

Two facts about the access path matter and were re-checked:

- **The ladder is copied per instance, not shared.** `0x140741F50` allocates and
  `memcpy`s, so each settings object owns a mutable 20-entry copy. A changed
  entry in one object cannot leak into another.
- **Nothing else reads the data.** `native_xrefs_list` on `0x14517BB38` reports
  exactly one reference, `0x140741F6D` inside `0x140741F50`
  (`ev_2efb5a1d…`). Every other reader must go through `+0x18`, which is why the
  index rule is still not visible from the data side.

### 18.3 The three validators (observed)

**`0x1407404C0` — the settings object.** In order, it requires:

1. `+0x10` (the roles dictionary) is not null;
2. the object is the expected type (`sub_14325cd50`);
3. `0x1407406C0` (below) returns non-zero;
4. `+0x18` is not null and its length is **exactly `0x14` = 20**;
5. the array's element type matches the expected type (`sub_1431fef00`);
6. `+0x20 >= 0.0`, `+0x28 >= 0.0`, `+0x30 >= 0.0`;
7. `+0x20 + +0x28 + +0x30 > 0.0`.

`+0x38` (CaShare) is **never read by this function**, so the original does not
range-check the CA share. That is why `ROLE_WEIGHTS_RULES["ca_share_validated"]`
is `False` in the build.

**`0x1407406C0` — positions and duties (new in this pass).** This is the check
`0x1407404C0` calls at step 3, and no earlier section read it. It walks the
position enum and, for each position, looks the value up in the `+0x10`
dictionary:

```c
plVar8 = (longlong *)func_0x0001425f2160(*(undefined8 *)(param_1 + 0x10), uVar13);
if (plVar8 == (longlong *)0x0) return 0;          // position missing -> reject
...
lVar7 = func_0x000142d52660(*(undefined8 *)(lRam00000001472d20f0 + 0x10), lVar7);
if (lVar7 == 0) return 0;                         // duty attribute unknown -> reject
```

So the original rejects a roles object unless **every position it knows has an
entry**, and **every duty's attribute resolves** in its attribute dictionary. It
is a completeness rule, not a numeric one — and the build did not implement it.

**`0x140741DE0` — the attribute-group shares.** It converts three collections at
`+0x10`, `+0x18` and `+0x20` and requires each total to be exactly `0x64` = 100:

```c
if (esi != 0x64) return 0;
if (ebp != 0x64) return 0;
return <third total> == 0x64;
```

Note this is a *different object* from `0x140740340`: its three slots are
collections of weighted attributes, not the ladder and the group weights. That is
why the build records `group_total: 100` but does not apply it to the role-weights
object.

### 18.4 Team-selection weights (`0x1407417F0`)

```c
*(undefined8 *)(rcx + 0x10) = 0x5000000096;  // 150, 80
*(undefined8 *)(rcx + 0x18) = 0x5a00000078;  // 120, 90
*(undefined8 *)(rcx + 0x20) = 0x2300000050;  //  80, 35
*(undefined4 *)(rcx + 0x28) = 0xf;           //  15
```

Seven int32s: **150, 80, 120, 90, 80, 35, 15**, matching `Ratings/teamSelection.json`.

### 18.5 The original's own rate model (observed names)

Following the metadata names rather than guessing at code shape gives the
original's own vocabulary for this feature (`ev_55872710…`, `ev_b6bb5b8d…`,
`ev_2700c200…`, `ev_6a8a57c3…`):

| Name string | Address in `.rdata` | What it says |
|---|---|---|
| `Fmrte.Framework.Scout.PlayerRating` | `0x143775823` | the rating object |
| `Fmrte.Framework.Scout.IPlayerRating` | `0x14377574d` | its interface |
| `Fmrte.Framework.Scout.Rates.PositionRoleAndDutiesRate` | `0x14377588b` | a rate per position/role/duty |
| `PositionRoleAndDutiesRate$PlayerPositionRate&GeneralPositionRate` | `0x143784769` | **two** rates per entry |
| `Fmrte.Framework.Scout.Rates.PositionRoleRates` | `0x1437758f2` | the collection of those rates |
| `Fmrte.Framework.Scout.Attributes.WeightedAttribute` | `0x143775616` | an attribute with a weight |
| `Fmrte.Framework.Scout.Attributes.RolesAndDuties.DutyAttribute` | `0x14377551f` | one attribute of a duty |
| `Fmrte.Framework.Scout.Attributes.RolesAndDuties.DutiesDictionary` | `0x1437754a1` | duties keyed by name |
| `Fmrte.Framework.Scout.Settings.PositionRolesAndDutiesSettings` | `0x143775aa8` | the settings object (§18.1) |
| `Fmrte.Framework.Scout.Settings.Defaults.DefaultPositionRolesAndDutiesSettings` | `0x143775952` | its compiled defaults |
| `Fmrte.Framework.Scout.Calculators.BestFormationsCalculator` | `0x1437756ea` | best-position/formation picker |
| `GetPlayerPositionRate` | `0x143877b51` | a method that returns the player-position rate |

Two things follow, and they are **derived**, not observed behaviour:

- the original keeps a **player-position rate** and a **general-position rate**
  side by side, which is the structural reason a player can be shown with an
  out-of-position penalty at all;
- `BestFormationsCalculator` is a separate calculator, so "best position" is
  computed there rather than inside the rating object.

What this does **not** prove is the arithmetic that turns attributes into a
rate, or the index rule that turns a competence gap into a ladder slot. Those
remain open (§18.6).

### 18.6 What this pass did not establish

- **The rating's instruction sequence.** Still not located. The blend in §2.3
  rests on the original's own help text, the compiled `0.5` at `+0x38`, and
  42,344 outputs — not on a located `mulsd`/`addsd`.
- **The ladder's index rule.** The values, the slot, the builder, the length and
  the validator are all proven; the code that maps a competence gap to an index
  is reached through the flattened dispatcher and was not isolated.
  `position_penalty()` therefore stays **derived**.
- **`0x1408410A0` / `0x140841320`** (the read/write wrappers) lie outside the
  64 KB slice and were re-read only as flattened lifter output this session. They
  stay as documented in §4.2 / §9.3.
- **The full-binary Ghidra analysis** was started (project
  `app/tools/ghidra_full`) but had not finished analysing 128 MB when this
  section was written; nothing here depends on it.

### 18.7 What changed in the build

| Change | File | Why |
|---|---|---|
| `ROLE_WEIGHTS_RULES["roles_completeness_validator"] = "0x1407406C0"` and a new `validate_role_definitions()` | `fmrte/ratings.py` | the rule at `0x1407406C0` — every position present, every duty attribute known — is now executable instead of only described |
| the role table is checked at load and reported | `fmrte/ratings.py`, `tests/test_rolegeights.py` | the original rejects an incomplete roles object; the build now reports the same problem |
| the type map of §18.5 recorded as `SCOUT_TYPE_MAP` | `fmrte/ratings.py` | a future reader can go from the original's own name to its `.rdata` address without re-searching |

The defaults are unchanged, so the numbers the person already sees do not move:
Pulisic is still `Winger / WideForward / InPossession`, **77.49 %**.




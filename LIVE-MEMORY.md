# Why the running game could not be read, and what changed

The editor used to attach to the running game. Then it stopped, and only the
snapshot worked. This file records each cause that was found in the code and
the change that closes it, so a future failure can be narrowed instead of
guessed at.

The attach path is `dialogs.LoadGameDialog._worker` →
`livewrite.LiveEditor.attach` → `live.open_game` → `GameProcess.resolve_table`.
Five separate things could stop it, and any one of them produced the same
symptom: "the snapshot works, the game does not".

---

## 1. The scan read across unmapped pages and gave up

`GameProcess.scan_table` read **1 MiB** chunks straight out of `game_plugin.dll`
and skipped a chunk whenever `ReadProcessMemory` refused it. A 1 MiB chunk
almost always crosses a page that is `PAGE_NOACCESS` or simply not committed —
`.text`, `.rdata` and `.data` are laid out with gaps between them — so the scan
skipped most of the module and never met the table.

On a build that is **in the registry** this never showed, because the RVA lookup
succeeded first. It only bit the build that is actually installed here
(`2505117`), which is newer than the three registered ones.

**Changed:** `scan_table` now asks `VirtualQueryEx` for the committed, readable
ranges inside the module (`readable_regions`), checks **writable** regions first
(the offset table lives in `.data`), and scans each region separately. A 1 MiB
read that still fails is retried in 64 KiB slices, so an unmapped page costs one
slice rather than the whole megabyte. In the test harness — where there is no
real handle and `VirtualQueryEx` fails — the method falls back to the old bounded
linear scan, which is why `tools/test_table.py` still finds an unregistered
build.

## 2. The build number was truncated, so no RVA was ever tried

`game_build()` read `game_plugin.dll`'s version resource and computed
`build = product_ls & 0xFFFF`. FM writes the build in the **upper** 16 bits of
`dwProductVersionLS` (`27.1.0.2505117`), so the mask threw the build away and
produced `0`.

**Changed:** the version resource is now read the way the person's own reader
does it — `VerQueryValueW` on
`\StringFileInfo\<lang><codepage>\ProductVersion`, which holds the full string
`27.1.0+2505117`, parsed by `_parse_build_from_version`. The fixed-file-info
path is kept only as a fallback and uses the upper word.

## 3. Only three table RVAs were known

`BUILD_TO_TABLE_RVA` held three Steam builds. The Xbox / Game Pass image and
one newer Steam image have their own, and two of them are not derivable from a
build number at all.

**Changed:** `KNOWN_TABLE_RVAS` now carries the verified RVAs from the person's
`NativeModuleProfile`:

| Image | RVA |
|---|---|
| Steam 27.0.1 / 2494687 | `0x5820628` |
| Xbox / PC Game Pass 2494687 | `0x5683BD8` |
| Steam 26620374 | `0x587B6D0` |
| 2493426 | `0x57055E8` |
| 2486695 | `0x57035E8` |

`resolve_table` tries the build's own RVA, then every known RVA, and only then
scans — so a store build that shares a build number with the Steam image still
resolves.

## 4. The game was found only by its window title

`find_game_process` enumerated top-level windows and matched
`"football manager" in title or title == "fm"`. A localised title, a splash
screen, or a minimised game has neither, and the editor then said "Football
Manager 27 is not running" while it plainly was.

**Changed:** a Toolhelp32 snapshot (`CreateToolhelp32Snapshot`) now finds any
running `fm.exe` by executable name, which does not depend on the title at all.
The window enumeration is kept as a second source for the splash window, now
also matching a localised title (`足球经理`) and skipping this editor's own
windows. `_module_base` gained the same treatment: `EnumProcessModulesEx` first,
Toolhelp32 `Module32FirstW` as a fallback.

## 5. Write access was required even to read

`LiveEditor.attach` called `open_game(write=True)`, which opens the process with
`PROCESS_VM_WRITE | PROCESS_VM_OPERATION`. Against a game at a higher integrity
level (Xbox installs run High; this editor runs Medium) Windows refuses that
handle **even when `PROCESS_VM_READ` would be granted**, so nothing loaded at
all.

**Changed:** `attach` now catches `needs_admin` / `denied` from the write handle
and retries with `write=False`, marking the editor **read-only**. Read-only
attaches load every live value and refuse writes with a plain sentence
("Start this editor as administrator and press Load Game to write changes into
the game"), instead of the whole feature failing. The dialog reports which mode
it got:

```
Attached (read-only): 39,812 players read from the game.
Run as administrator to write edits.
```

## 6. Two smaller corrections found on the way

* `decode_record` read morale as a signed 16-bit value at `P - 28`; the person's
  own layout says **one byte** (`Direct.moraleRel = -0x1C`, `u8`). A `i16` read
  picked up the neighbouring byte, so morale came out in the thousands.
* The report carried no live game date or managed club, so the assistant's
  context always described the editor's default club. `read_game_date` and
  `read_human_manager_club` now fill `LiveReport.game_date` /
  `LiveReport.managed_club`, and `Snapshot.live_meta` exposes them through
  `meta()`, which is what `assistant.resolve_active_club` reads.

---

## How to tell which one it is now

`live.diagnose()` reports the reason in words, and the Load Game dialog shows it:

| `report.code` | What it means | What to do |
|---|---|---|
| `not_running` | no `fm.exe` and no matching window | start the game, load the save |
| `no_module` | the process is there, `game_plugin.dll` is not | wait for the save to finish loading |
| `needs_admin` | Windows refused the handle: integrity levels differ | use **Restart as administrator** |
| `denied` | Windows refused for another reason | same |
| `no_table` | the module was found, the table was not | the message names the best score and the threshold |
| `no_people` / `bad_count` | the table was found, its people vector is empty or impossible | the save has not finished loading |

## What is still not proven

The whole path is exercised against a fake process image
(`tests/test_livewrite.py`, `tests/test_fmmemory.py`, `tools/test_table.py`) and against the
real registry values, but **this machine cannot complete an end-to-end attach**: the game is
installed by Xbox under `C:\XboxGames` and runs at High integrity while the editor runs at
Medium. The read-only fallback is what makes that case usable partially rather than not at
all; a write-mode attach still needs the editor started as administrator.

---

## Two corrections found by review

### The date and the manager club must follow the table, not a stale build

The first version of `read_game_date` / `read_human_manager_club` tried the per-build RVA, then
**every** known build's RVA, and only then the address derived from the table it had just
resolved. For an unregistered build — which is exactly this machine, `27.1.0+2505117` — the
table is found by scanning, but the date was still read from the *old* builds' addresses. Those
addresses sit inside a 377 MiB image, so a 4-byte read landing on something that looks like
`day 1..366, year 2024..2100` is entirely possible, and the editor would show a fabricated date
with no way to tell.

The candidate order is now chosen by **pairing**:

| Situation | Order tried |
|---|---|
| the table came from `scan_table` | `table_base - TABLE_TO_*` first, then the build's own RVA, then the known ones |
| the build is registered | the build's own RVA first, then the known ones, then the paired fallback |

Every registered build behaves exactly as before; a scanned build no longer reads a stale
address at all. Pinned by tests that record *which address was read*, not just what came back.

### The write path asked for more than it needed

`WriteProcessMemory` was preceded by an unconditional `VirtualProtectEx(PAGE_EXECUTE_READWRITE)`
on the target page, and the restore was not in a `finally`. Data pages need `PAGE_READWRITE`,
not execute rights, and an exception between the protect and the restore left the page
executable-writable — the opposite of what the comment promised.

The path is now: write directly; only if that is refused, `VirtualProtectEx` to the **minimal**
writable protection (an already-executable page keeps its execute bit, a data page never gains
one), write, and restore the old protection in a `finally`.


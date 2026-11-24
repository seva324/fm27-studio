"""The live game reader - the public surface, kept where callers expect it.

This module used to be 1438 lines that mixed two unrelated jobs: raw Win32
process access (``ctypes`` declarations, process and module discovery,
``ReadProcessMemory``, ``VirtualQueryEx``, integrity probing, version
resources) and the Football Manager 27 memory layout (offset-table scoring,
the people vector, the record decode).  Because the second job was welded to
the first, the only way to test a decode was to subclass the process object
and override its reads, and anything that was not "read a block and decode it"
had no coverage at all.

The two halves now live apart:

:mod:`fmrte.procmem`
    The seam.  The :class:`~fmrte.procmem.ProcessMemory` interface, the Win32
    adapter (:class:`~fmrte.procmem.Win32ProcessMemory`), an in-memory test
    adapter (:class:`~fmrte.procmem.InMemoryProcessMemory`), process and module
    discovery, the integrity probe and the build id.
:mod:`fmrte.fmmemory`
    The FM27 memory module.  The offset table and its scan, the people vector,
    the record decode, the injury chain, the game date, the manager's club -
    everything that depends on the game and not on the operating system.

**This module still exports every name it exported before**, because callers,
tools and tests import them from here and none of them was changed: the names
are re-exported from the two new modules instead of being declared again (see
``tests/test_fmmemory.py::PublicSurfaceTests``, which fails if one disappears).
"""

from __future__ import annotations

from ctypes import wintypes as w  # noqa: F401 - re-exported; tests use live.w

from . import procmem
from .fmmemory import (  # noqa: F401 - re-exported public surface
    BLOCK_BEFORE,
    BLOCK_SIZE,
    BUILD_TO_ALTERNATE_DATE_RVA,
    BUILD_TO_CURRENT_DATE_RVA,
    BUILD_TO_CURRENT_SCREEN_RVA,
    BUILD_TO_HUMAN_MANAGER_RVA,
    BUILD_TO_TABLE_RVA,
    CLUB_NAME_LIMIT,
    CLUB_NAME_RELS,
    CURRENT_SCREEN_ENV,
    CurrentScreen,
    DATE_DAY_MASK,
    EMPLOYMENT_TEAM_REL,
    FmMemory,
    GAME_MODULE,
    GAME_PLUGIN_SCAN_RANGE,
    HOT_BAND_END,
    HOT_BAND_START,
    INJURY_TYPE_NAME_LIMIT,
    KNOWN_DATE_RVAS,
    KNOWN_HUMAN_MANAGER_RVAS,
    KNOWN_TABLE_RVAS,
    LEGACY_TABLE_RVA,
    LiveError,
    LiveInjury,
    LivePlayer,
    LiveReport,
    MANAGER_EMPLOYMENT_REL,
    MAX_ABILITY,
    MAX_DATE_YEAR,
    MAX_JADEDNESS,
    MAX_MORALE,
    MAX_PEOPLE,
    MAX_REPUTATION,
    MAX_SCAN_SPAN,
    MIN_DATE_YEAR,
    MIN_GAME_YEAR,
    MIN_PEOPLE,
    MIN_VALID_TABLE_SCORE,
    REL_CA,
    REL_CONDITION,
    REL_CURRENT_REP,
    REL_DATE_OF_BIRTH,
    REL_HISTORY_COPY,
    REL_HOME_REP,
    REL_JADEDNESS,
    REL_MORALE,
    REL_PA,
    REL_SHARPNESS,
    REL_UNIQUE_ID,
    REL_WORLD_REP,
    SCAN_CHUNK_BYTES,
    SLOTS,
    STRING_CODE_UNITS,
    STRING_UTF16,
    STRING_UTF8,
    SUB_SLICE_BYTES,
    SUBTYPE_HUMAN_MANAGER,
    SUBTYPE_PLAYER,
    SUBTYPE_PLAYER_STAFF,
    SUBTYPE_STAFF,
    TABLE_TO_CURRENT_DATE_RVA,
    TABLE_TO_CURRENT_SCREEN_RVA,
    TABLE_TO_HUMAN_MANAGER_RVA,
    TABLE_VECTOR_PTR,
    TEAM_CLUB_REL,
    decode_date,
    decode_record,
    diagnose,
    open_game,
    read_live,
)
from .procmem import (  # noqa: F401 - re-exported public surface
    INTEGRITY_NAMES,
    LIST_MODULES_ALL,
    MAX_PATH,
    MEM_COMMIT,
    PAGE_EXECUTE_READ,
    PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY,
    PAGE_GUARD,
    PAGE_NOACCESS,
    PAGE_READONLY,
    PAGE_READWRITE,
    PAGE_WRITECOPY,
    PROCESS_QUERY_INFORMATION,
    PROCESS_QUERY_LIMITED_INFORMATION,
    PROCESS_VM_OPERATION,
    PROCESS_VM_READ,
    PROCESS_VM_WRITE,
    READABLE_PROTECTIONS,
    TH32CS_SNAPMODULE,
    TH32CS_SNAPMODULE32,
    TH32CS_SNAPPROCESS,
    WRITABLE_PROTECTIONS,
    InMemoryProcessMemory,
    ProcessMemory,
    Win32ProcessMemory,
    _is_windows,
    _MemoryBasicInformation,
    _ModuleEntry32W,
    _ModuleInfo,
    _module_base,
    _module_base_toolhelp,
    _parse_build_from_version,
    _process_path,
    _ProcessEntry32W,
    _toolhelp_fm_pids,
    _window_pids,
    find_game_process,
    game_build,
    integrity_level,
    integrity_name,
    open_process,
    self_elevated,
)

if _is_windows():
    # The DLL objects only exist on Windows, exactly as they did before the
    # split: importing this module elsewhere is harmless and binds no DLL.
    from .procmem import (  # noqa: F401 - re-exported for tools and tests
        _enum_modules,
        _enum_processes,
        _module_base_name,
        advapi32,
        kernel32,
        psapi,
        user32,
    )


class GameProcess(FmMemory):
    """The FM27 decoder over one Win32 process handle.

    Kept because two tools and the end-to-end test attach this way: they
    already hold a handle from ``OpenProcess`` and hand it over.  The
    production path (:func:`fmrte.fmmemory.open_game`) builds an
    :class:`~fmrte.fmmemory.FmMemory` around a
    :class:`~fmrte.procmem.Win32ProcessMemory` directly; this is the same
    object with a constructor that takes the handle.  ``can_write`` defaults to
    True because a caller that opened the handle itself already chose the
    rights it asked ``OpenProcess`` for - pass ``can_write=False`` for a
    read-only handle.
    """

    def __init__(self, pid: int, handle: int, module_base: int,
                 module_size: int, exe: str = "", *, can_write: bool = True):
        super().__init__(procmem.Win32ProcessMemory(
            pid=pid, handle=handle, module_base=module_base,
            module_size=module_size, exe=exe, can_write=can_write))

    @property
    def handle(self) -> int:
        """The raw Win32 handle, 0 once :meth:`close` has run."""
        return int(getattr(self.memory, "handle", 0) or 0)

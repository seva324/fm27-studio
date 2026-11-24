"""The seam between this editor and an operating system's process memory.

Why this module exists
----------------------
``live.py`` used to be two modules welded together: the Win32 plumbing (DLL
declarations, process and module discovery, ``OpenProcess``,
``ReadProcessMemory``, ``VirtualQueryEx``, integrity probing, version
resources) and the Football Manager 27 memory layout (offset-table scoring,
the people vector, the record decode).  Because the second half was a method
of the object that owned the first half, the only way to test it was to
subclass the process object and override its reads.  Anything that was not
"read a block and decode it" - the live game date, the manager's club, the
readable-region scan, the read-only attach - was untested.

This module is the seam.  It owns everything that is about an *operating
system process* and nothing that is about Football Manager:

* the ``ctypes`` declarations for kernel32, psapi, advapi32 and user32, the
  structures they need, and the guarded binding so importing this file on a
  non-Windows interpreter is harmless;
* process discovery (:func:`find_game_process`), module enumeration
  (:func:`_module_base` and its Toolhelp32 fallback) and the build id from the
  game plugin's version resource (:func:`game_build`);
* the mandatory-integrity probe that explains *why* Windows refused
  (:func:`integrity_level`, :func:`integrity_name`, :func:`self_elevated`);
* the :class:`ProcessMemory` interface and its two adapters.

The interface
-------------
:class:`ProcessMemory` is a small ordinary class (not a ``Protocol``: this
build supports one Python, and an explicit base class documents itself and can
be ``isinstance``-checked).  It has exactly the methods the FM27 decoder in
:mod:`fmrte.fmmemory` calls, and nothing else on purpose: if the decoder ever
needs something the interface does not offer, the seam has been broken and
that shows up as an ``AttributeError`` in a test rather than as another
untestable ctypes call.

Two adapters implement it:

:class:`Win32ProcessMemory`
    The production adapter: one ``HANDLE`` from ``OpenProcess``, with
    ``ReadProcessMemory``/``WriteProcessMemory`` and a ``VirtualQueryEx`` walk.
:class:`InMemoryProcessMemory`
    A test adapter: a dictionary of mapped byte blocks that answers exactly
    like the Win32 calls do (a read that leaves a mapped block returns
    ``None``, a write into an unmapped or read-only block returns ``False``).
    This is what lets the FM27 decoder be tested on any machine.

The same ``ctypes`` calls are wrapped the way ``live.py`` always wrapped them:
a wrong declaration or a refused call must never escape as a raw
``TypeError``/``ArgumentError``, because the window can only explain a
``LiveError`` in plain words.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
from ctypes import wintypes as w

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
MEM_COMMIT = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
LIST_MODULES_ALL = 0x03
MAX_PATH = 260

# The protections ``VirtualQueryEx`` reports that a read may touch.  Split out
# of the walk so the two sets are readable next to the constants they name.
READABLE_PROTECTIONS = frozenset({
    PAGE_READONLY, PAGE_READWRITE, PAGE_WRITECOPY,
    PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY,
})
WRITABLE_PROTECTIONS = frozenset({
    PAGE_READWRITE, PAGE_WRITECOPY,
    PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY,
})
# The protections that already carry the execute bit.  A write never *grants*
# it (see :func:`write_page_protection`); it can only preserve one that a page
# already has.
EXECUTABLE_PROTECTIONS = frozenset({
    PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY,
})


def write_page_protection(current: int | None = None) -> int:
    """The protection to ask ``VirtualProtectEx`` for when a write is refused.

    ``PAGE_READWRITE`` is the minimal protection that lets bytes land, and it
    is exactly what Football Manager's data pages already carry.  The old code
    asked for ``PAGE_EXECUTE_READWRITE`` unconditionally, which handed another
    process an executable page for the duration of one four-byte edit - more
    privilege than the write needs, on pages that never execute.

    A page that already executes keeps executing: taking the bit away under a
    caller that is writing code would be worse than leaving it, so the execute
    bit is *preserved* for such a page and never granted to any other.
    ``current`` is the page's protection when the caller knows it; ``None``
    means "an ordinary data page", which is every page this editor writes.
    """
    base = 0 if current is None else int(current) & 0xFF
    if base in EXECUTABLE_PROTECTIONS:
        return PAGE_EXECUTE_READWRITE
    return PAGE_READWRITE


class LiveError(RuntimeError):
    """An attach or read failed, with a reason the window can show in words.

    ``hint`` is the actionable sentence the dialog prints under the reason and
    ``code`` is the stable identifier the window branches on (``not_running``,
    ``needs_admin``, ``denied``, ``open_failed``, ``no_module``, ``no_table``,
    ``no_people``, ``bad_count``, ``read_failed``, ``no_match``, ``platform``).
    It lives here, in the lowest layer, because both the attach and the FM27
    decode raise it and neither may import the other's module.
    """

    def __init__(self, message: str, hint: str = "", code: str = ""):
        super().__init__(message)
        self.hint = hint
        self.code = code


def _is_windows() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------------------
# ctypes declarations
# ---------------------------------------------------------------------------
# The WinDLL objects only exist on Windows.  Importing this module elsewhere
# leaves the constants and the structures above and below in place (so tests
# and documentation can read them) but binds no DLL, exactly like live.py did.

if _is_windows():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    kernel32.OpenProcess.restype = w.HANDLE
    kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel32.CloseHandle.argtypes = [w.HANDLE]
    kernel32.GetCurrentProcess.restype = w.HANDLE
    kernel32.ReadProcessMemory.restype = w.BOOL
    kernel32.ReadProcessMemory.argtypes = [
        w.HANDLE, w.LPCVOID, w.LPVOID, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.WriteProcessMemory.restype = w.BOOL
    kernel32.WriteProcessMemory.argtypes = [
        w.HANDLE, w.LPVOID, w.LPCVOID, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.VirtualProtectEx.restype = w.BOOL
    kernel32.VirtualProtectEx.argtypes = [
        w.HANDLE, w.LPVOID, ctypes.c_size_t, w.DWORD,
        ctypes.POINTER(w.DWORD),
    ]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.VirtualQueryEx.argtypes = [
        w.HANDLE, w.LPCVOID, ctypes.c_void_p, ctypes.c_size_t,
    ]
    kernel32.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD),
    ]
    _enum_processes = getattr(kernel32, "K32EnumProcesses", None) or psapi.EnumProcesses
    _enum_processes.argtypes = [
        ctypes.POINTER(w.DWORD), w.DWORD, ctypes.POINTER(w.DWORD),
    ]
    _enum_modules = (getattr(kernel32, "K32EnumProcessModulesEx", None)
                     or psapi.EnumProcessModulesEx)
    _enum_modules.restype = w.BOOL
    _enum_modules.argtypes = [
        w.HANDLE, ctypes.POINTER(w.HMODULE), w.DWORD,
        ctypes.POINTER(w.DWORD), w.DWORD,
    ]
    _module_base_name = (getattr(kernel32, "K32GetModuleBaseNameW", None)
                         or psapi.GetModuleBaseNameW)
    _module_base_name.argtypes = [w.HANDLE, w.HMODULE, w.LPWSTR, w.DWORD]
    # The third argument is a MODULEINFO*, not a void**. Declaring it as
    # POINTER(c_void_p) made ctypes reject the byref(_ModuleInfo) call with
    # "argument 3: TypeError", which is the error the person saw on Load Game.
    psapi.GetModuleInformation.argtypes = [
        w.HANDLE, w.HMODULE, ctypes.c_void_p, w.DWORD,
    ]
    psapi.GetModuleInformation.restype = w.BOOL
    advapi32.OpenProcessToken.argtypes = [
        w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
        ctypes.POINTER(w.DWORD),
    ]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(w.DWORD)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, w.DWORD]


class _ModuleInfo(ctypes.Structure):
    _fields_ = [
        ("lpBaseOfDll", ctypes.c_void_p),
        ("SizeOfImage", w.DWORD),
        ("EntryPoint", ctypes.c_void_p),
    ]


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", w.DWORD),
        ("PartitionId", w.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", w.DWORD),
        ("Protect", w.DWORD),
        ("Type", w.DWORD),
    ]


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", w.LONG),
        ("dwFlags", w.DWORD),
        ("szExeFile", w.WCHAR * MAX_PATH),
    ]


class _ModuleEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("th32ModuleID", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("GlblcntUsage", w.DWORD),
        ("ProccntUsage", w.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", w.DWORD),
        ("hModule", w.HMODULE),
        ("szModule", w.WCHAR * 256),
        ("szExePath", w.WCHAR * MAX_PATH),
    ]


if _is_windows():
    kernel32.Process32FirstW.restype = w.BOOL
    kernel32.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel32.Process32NextW.restype = w.BOOL
    kernel32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel32.Module32FirstW.restype = w.BOOL
    kernel32.Module32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(_ModuleEntry32W)]
    kernel32.Module32NextW.restype = w.BOOL
    kernel32.Module32NextW.argtypes = [w.HANDLE, ctypes.POINTER(_ModuleEntry32W)]


# ---------------------------------------------------------------------------
# Integrity levels: the reason a read is refused
# ---------------------------------------------------------------------------
# Football Manager 27 runs at High integrity (it is installed under
# C:\XboxGames and started by the Xbox app).  A normal, non-elevated program
# runs at Medium, and Windows refuses to hand it a memory-read handle to a High
# process.  The only fix is to run this editor elevated as well; these three
# functions are what let the window say so in plain words.

INTEGRITY_NAMES = {
    0x0000: "Untrusted",
    0x1000: "Low",
    0x2000: "Medium",
    0x3000: "High",
    0x4000: "System",
}


def integrity_level(pid: int) -> int | None:
    """The mandatory integrity RID of a process, or None if it can't be read."""
    if not _is_windows() or pid <= 0:
        return None
    try:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            token = w.HANDLE()
            if not advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
                return None
            try:
                size = w.DWORD(0)
                got = w.DWORD()
                advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
                if not size.value:
                    return None
                buffer = ctypes.create_string_buffer(size.value)
                if not advapi32.GetTokenInformation(
                        token, 25, buffer, size.value, ctypes.byref(got)):
                    return None
                sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
                count = advapi32.GetSidSubAuthorityCount(sid)[0]
                return int(advapi32.GetSidSubAuthority(sid, count - 1)[0])
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, TypeError, ValueError, AttributeError):
        return None


def integrity_name(rid: int | None) -> str:
    if rid is None:
        return "unknown"
    return INTEGRITY_NAMES.get(rid, f"0x{rid:x}")


def self_elevated() -> bool:
    """True when this program runs at High or System integrity."""
    level = integrity_level(os.getpid())
    return level is not None and level >= 0x3000


# ---------------------------------------------------------------------------
# Finding the game
# ---------------------------------------------------------------------------

def _process_path(handle) -> str:
    size = w.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(1024)
    if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return buffer.value
    return ""


def _window_pids() -> dict[int, str]:
    """Process ids that own a top-level window, with its title.

    Football Manager started by the Xbox app runs at High integrity, so it is
    invisible to a Medium-integrity process enumeration.  Its window is still
    enumerable, which is how the game is found in that case.
    """
    found: dict[int, str] = {}
    if not _is_windows():
        return found
    try:
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        def visit(hwnd, _param):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value.strip()
                if title:
                    pid = w.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value:
                        found.setdefault(int(pid.value), title)
            return True

        user32.EnumWindows(callback_type(visit), 0)
    except (OSError, TypeError, ValueError, AttributeError):
        return found
    return found


def _toolhelp_fm_pids() -> list[tuple[int, str]]:
    """Find running fm.exe processes via Toolhelp32 snapshot."""
    found: list[tuple[int, str]] = []
    if not _is_windows():
        return found
    self_pid = os.getpid()
    try:
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return found
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                pid = int(entry.th32ProcessID)
                exe_name = (entry.szExeFile or "").strip().lower()
                if pid and pid != self_pid and exe_name == "fm.exe":
                    handle = kernel32.OpenProcess(
                        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    path = _process_path(handle) if handle else ""
                    if handle:
                        kernel32.CloseHandle(handle)
                    found.append((pid, path or entry.szExeFile))
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(w.HANDLE(snap))
    except (OSError, TypeError, ValueError, AttributeError):
        return found
    return found


def find_game_process() -> tuple[int, str]:
    """Return (pid, exe path) of the running Football Manager 27."""
    if not _is_windows():
        raise LiveError("Reading the game's memory only works on Windows.",
                        code="platform")
    self_pid = os.getpid()
    candidates: list[tuple[int, str]] = []
    seen_pids: set[int] = set()

    for pid, path in _toolhelp_fm_pids():
        if pid not in seen_pids:
            seen_pids.add(pid)
            candidates.append((pid, path))

    titles = _window_pids()
    for pid, title in titles.items():
        if pid == self_pid or pid in seen_pids:
            continue
        lowered = title.lower()
        if "real time editor" in lowered or "fmrte" in lowered:
            continue
        if ("football manager" in lowered or lowered == "fm"
                or "足球经理" in lowered):
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            path = _process_path(handle) if handle else ""
            if handle:
                kernel32.CloseHandle(handle)
            seen_pids.add(pid)
            candidates.append((pid, path))

    if not candidates:
        raise LiveError(
            "Football Manager 27 is not running.",
            "Start the game, load your save and wait until it has finished "
            "loading, then press Load Game again.",
            code="not_running")
    # Prefer a real fm.exe over anything else that happens to be titled so.
    for pid, path in candidates:
        base = os.path.basename(path or "").lower()
        if base == "fm.exe" or (path or "").lower().endswith("fm.exe"):
            return pid, path
    return candidates[0]


# ---------------------------------------------------------------------------
# Module enumeration
# ---------------------------------------------------------------------------

def _module_base_toolhelp(pid: int, name: str) -> tuple[int, int]:
    """Fallback module lookup using CreateToolhelp32Snapshot."""
    if not _is_windows() or pid <= 0:
        return 0, 0
    try:
        snap = kernel32.CreateToolhelp32Snapshot(
            TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return 0, 0
        try:
            entry = _ModuleEntry32W()
            entry.dwSize = ctypes.sizeof(_ModuleEntry32W)
            ok = kernel32.Module32FirstW(snap, ctypes.byref(entry))
            target = name.lower()
            while ok:
                mod_name = (entry.szModule or "").strip().lower()
                mod_path = os.path.basename((entry.szExePath or "").strip()).lower()
                if mod_name == target or mod_path == target:
                    return int(entry.modBaseAddr or 0), int(entry.modBaseSize or 0)
                ok = kernel32.Module32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(w.HANDLE(snap))
    except (OSError, TypeError, ValueError, AttributeError):
        return 0, 0
    return 0, 0


def _module_base(handle, name: str, pid: int = 0) -> tuple[int, int]:
    """Base address and image size of one loaded module.

    Every ctypes call here is wrapped: a wrong declaration or a refused read
    must never escape as a raw TypeError/ArgumentError, because the window can
    only explain a LiveError in plain words.
    """
    try:
        modules = (w.HMODULE * 2048)()
        needed = w.DWORD()
        if _enum_modules(handle, modules, ctypes.sizeof(modules),
                         ctypes.byref(needed), LIST_MODULES_ALL):
            count = min(2048, needed.value // ctypes.sizeof(w.HMODULE))
            for index in range(count):
                buffer = ctypes.create_unicode_buffer(MAX_PATH)
                _module_base_name(handle, modules[index], buffer, MAX_PATH)
                if buffer.value.lower() != name.lower():
                    continue
                info = _ModuleInfo()
                if psapi.GetModuleInformation(handle, modules[index],
                                              ctypes.byref(info),
                                              ctypes.sizeof(info)):
                    return int(info.lpBaseOfDll or 0), int(info.SizeOfImage or 0)
    except (OSError, TypeError, ValueError, AttributeError):
        pass
    if pid > 0:
        return _module_base_toolhelp(pid, name)
    return 0, 0


# ---------------------------------------------------------------------------
# The game build, from the plugin's version resource
# ---------------------------------------------------------------------------

def _parse_build_from_version(text: str) -> int | None:
    if not text:
        return None
    match = re.search(r"[+.\s](\d{6,8})\b", text.strip())
    if match:
        return int(match.group(1))
    return None


def game_build(exe: str = "") -> int | None:
    """The FM build id, read from game_plugin.dll's version resource.

    FMRTE does the same thing through VERSION.dll rather than keeping the
    number in its binary.  The build id is the number after the '+', e.g.
    ``27.1.0+2505117`` -> 2505117.

    ``exe`` is the game executable the caller has *already* found
    (:func:`find_game_process`); passing it stops this function from running
    the whole process discovery a second time, which ``open_game`` used to do
    on every attach.  An empty ``exe`` still looks the game up, so existing
    callers keep working.
    """
    if not _is_windows():
        return None
    if not exe:
        try:
            _pid, exe = find_game_process()
        except LiveError:
            return None
    if not exe:
        return None
    plugin = ""
    for candidate in (
        os.path.join(os.path.dirname(exe), "fm_Data", "Plugins", "x86_64", "game_plugin.dll"),
        os.path.join(os.path.dirname(exe), "game_plugin.dll"),
    ):
        if os.path.exists(candidate):
            plugin = candidate
            break
    if not plugin:
        return None
    try:
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.restype = w.DWORD
        version.GetFileVersionInfoSizeW.argtypes = [w.LPCWSTR,
                                                    ctypes.POINTER(w.DWORD)]
        version.GetFileVersionInfoW.restype = w.BOOL
        version.GetFileVersionInfoW.argtypes = [
            w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p]
        version.VerQueryValueW.restype = w.BOOL
        version.VerQueryValueW.argtypes = [
            ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(w.UINT)]
        size = version.GetFileVersionInfoSizeW(plugin, None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(plugin, 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = w.UINT()
        # Try StringFileInfo ProductVersion first (holds full 7-digit build).
        if version.VerQueryValueW(
                buffer, "\\VarFileInfo\\Translation",
                ctypes.byref(pointer), ctypes.byref(length)) and length.value >= 4:
            trans = ctypes.string_at(pointer, 4)
            lang, codepage = struct.unpack("<HH", trans)
            subblock = f"\\StringFileInfo\\{lang:04X}{codepage:04X}\\ProductVersion"
            str_ptr = ctypes.c_void_p()
            str_len = w.UINT()
            if version.VerQueryValueW(
                    buffer, subblock, ctypes.byref(str_ptr),
                    ctypes.byref(str_len)) and str_len.value:
                text = ctypes.wstring_at(str_ptr.value, str_len.value).rstrip("\x00")
                parsed = _parse_build_from_version(text)
                if parsed:
                    return parsed
        if version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer),
                                  ctypes.byref(length)):
            raw = ctypes.string_at(pointer, 52)
            _product_ms, product_ls = struct.unpack_from("<II", raw, 16)
            build = product_ls & 0xFFFF
            return build or None
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class ProcessMemory:
    """One attached process, as the FM27 decoder needs to see it.

    This is deliberately small: every member below exists because some part of
    the FM27 decoding in :mod:`fmrte.fmmemory` calls it, and nothing exists
    that only Win32 needs.  That is the whole point of the seam - the decoder
    can be driven by a dictionary of bytes instead of a live game.

    Attributes
    ----------
    pid
        The operating system's process id.  Informational: the decoder never
        uses it to reach memory.
    module_base, module_size
        Where the module the offsets are relative to is loaded, and how big
        its image is.  FM27's offset tables live inside ``game_plugin.dll``, so
        every RVA from the registry in :mod:`fmrte.fmmemory` is resolved
        against ``module_base``.
    can_write
        True only when this attachment may write.  A read-only attachment is
        the normal case when the game runs at a higher integrity level than
        this editor, and it must still be able to read *everything*: only the
        write path may refuse.
    """

    pid: int = 0
    module_base: int = 0
    module_size: int = 0

    # -- the attachment ------------------------------------------------
    @property
    def can_write(self) -> bool:
        """True when :meth:`write` may be called on this attachment."""
        raise NotImplementedError

    def close(self) -> None:
        """Release whatever this attachment holds.  Idempotent."""
        raise NotImplementedError

    # -- bytes ---------------------------------------------------------
    def read(self, address: int, size: int) -> bytes | None:
        """Read ``size`` bytes at ``address``, or None if any byte is unreadable.

        Returning None for a *partly* readable range is not a limitation: it is
        ``ReadProcessMemory``'s own behaviour, and the FM27 scanner relies on
        it.  A 1 MiB probe that crosses an unmapped page must fail outright so
        the scanner retries that neighbourhood in smaller slices.
        """
        raise NotImplementedError

    def write(self, address: int, data: bytes) -> bool:
        """Write ``data`` at ``address``; True only when every byte was written."""
        raise NotImplementedError

    # -- the address space --------------------------------------------
    def readable_regions(self, start: int, limit: int) -> list[tuple[int, int, bool]]:
        """Committed readable ranges inside ``[start, limit)``.

        Each item is ``(start, end, writable)`` with ``end`` exclusive, in
        ascending address order.  Ordering *policy* - which region to try
        first - is the FM27 scanner's business, not the adapter's, because the
        hot band it prefers is a fact about Football Manager, not Windows.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Adapter one: the real process
# ---------------------------------------------------------------------------

class Win32ProcessMemory(ProcessMemory):
    """The production adapter: one ``HANDLE`` from ``OpenProcess``.

    ``handle`` is kept public because module enumeration
    (:func:`_module_base`) and the tests take the raw handle; everything else
    goes through the interface.
    """

    def __init__(self, pid: int, handle: int, module_base: int = 0,
                 module_size: int = 0, exe: str = "",
                 can_write: bool = True):
        self.pid = int(pid)
        self.handle = int(handle or 0)
        self.module_base = int(module_base or 0)
        self.module_size = int(module_size or 0)
        self.exe = exe
        self._write_rights = bool(can_write)

    @property
    def can_write(self) -> bool:
        return self._write_rights and self.handle > 0

    def read(self, address: int, size: int) -> bytes | None:
        if not self.handle or address <= 0 or size <= 0:
            return None
        try:
            buffer = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t(0)
            ok = kernel32.ReadProcessMemory(
                w.HANDLE(self.handle), ctypes.c_void_p(address),
                buffer, size, ctypes.byref(got))
            if not ok or got.value < size:
                return None
            return buffer.raw
        except (OSError, TypeError, ValueError, AttributeError):
            return None

    def _write_once(self, address: int, data: bytes) -> bool:
        """One ``WriteProcessMemory``; True only when every byte landed."""
        written = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(
            w.HANDLE(self.handle), ctypes.c_void_p(address), data,
            len(data), ctypes.byref(written))
        return bool(ok) and written.value == len(data)

    def _protection_at(self, address: int) -> int | None:
        """The current protection of the region holding ``address``, or None.

        Only used to pick the protection for a fallback write
        (:func:`write_page_protection`): a data page gets ``PAGE_READWRITE``
        and a page that already executes keeps its execute bit.  A query that
        fails is not an error - the caller then assumes a data page.
        """
        if not self.handle or not _is_windows():
            return None
        try:
            mbi = _MemoryBasicInformation()
            got = kernel32.VirtualQueryEx(
                w.HANDLE(self.handle), ctypes.c_void_p(address),
                ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not got:
                return None
            return int(mbi.Protect or 0)
        except (OSError, TypeError, ValueError, AttributeError):
            return None

    def write(self, address: int, data: bytes) -> bool:
        """Write bytes into the game.

        The game's heap is already writable, so the direct write is tried
        first: one ``WriteProcessMemory`` and no page ever changes protection.
        Only when Windows refuses it is the page opened for that single write,
        and then:

        * the protection asked for is :func:`write_page_protection`'s - the
          minimal writable one, so a data page never gains execute rights;
        * the page's previous protection is restored in a ``finally``, so an
          exception between the protect and the restore can no longer leave a
          page writable behind us (the old code restored without ``finally``,
          which is the opposite of what its comment promised).
        """
        if not self.handle or address <= 0 or not data:
            return False
        if not self.can_write:
            return False
        if not _is_windows():
            return False
        try:
            if self._write_once(address, data):
                return True
            old = w.DWORD(0)
            protection = write_page_protection(self._protection_at(address))
            if not kernel32.VirtualProtectEx(
                    w.HANDLE(self.handle), ctypes.c_void_p(address), len(data),
                    protection, ctypes.byref(old)):
                return False
            try:
                return self._write_once(address, data)
            finally:
                restored = w.DWORD(0)
                kernel32.VirtualProtectEx(
                    w.HANDLE(self.handle), ctypes.c_void_p(address),
                    len(data), old, ctypes.byref(restored))
        except (OSError, TypeError, ValueError, AttributeError):
            return False

    def readable_regions(self, start: int, limit: int) -> list[tuple[int, int, bool]]:
        """Walk ``VirtualQueryEx`` and report the committed readable ranges.

        Only committed, non-guarded pages with a readable protection are
        reported, clipped to ``[start, limit)``.  The walk stops at the first
        address the kernel will not describe, which is the end of the process's
        address space as far as this query can tell.
        """
        if not _is_windows() or not self.handle or self.handle <= 0:
            return []
        regions: list[tuple[int, int, bool]] = []
        cursor = start
        mbi = _MemoryBasicInformation()
        mbi_size = ctypes.sizeof(mbi)
        try:
            while cursor < limit:
                got = kernel32.VirtualQueryEx(
                    w.HANDLE(self.handle), ctypes.c_void_p(cursor),
                    ctypes.byref(mbi), mbi_size)
                if not got:
                    break
                base = int(mbi.BaseAddress or cursor)
                size = int(mbi.RegionSize or 0)
                if size <= 0:
                    break
                region_end = min(limit, base + size)
                prot = int(mbi.Protect or 0)
                base_prot = prot & 0xFF
                if (mbi.State == MEM_COMMIT
                        and not (prot & PAGE_GUARD)
                        and base_prot in READABLE_PROTECTIONS
                        and region_end > max(start, base)):
                    regions.append((max(start, base), region_end,
                                    base_prot in WRITABLE_PROTECTIONS))
                next_addr = base + size
                if next_addr <= cursor:
                    break
                cursor = next_addr
        except (OSError, TypeError, ValueError, AttributeError):
            return []
        return regions

    def close(self) -> None:
        if self.handle:
            try:
                kernel32.CloseHandle(w.HANDLE(self.handle))
            except Exception:
                pass
            self.handle = 0


def open_process(pid: int, write: bool = False, exe: str = "") -> Win32ProcessMemory:
    """Open one process for reading, and for writing when ``write`` is set.

    Raises :class:`LiveError` with the code the window branches on:
    ``needs_admin`` when the game runs at a higher integrity level than this
    editor (the normal Xbox/Game Pass case), ``denied`` when Windows refuses
    for another reason, ``open_failed`` for everything else.
    """
    access = PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
    if write:
        access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    try:
        handle = kernel32.OpenProcess(access, False, pid)
        error = ctypes.get_last_error()
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise LiveError(
            "Windows would not open the game's memory.",
            "Start this editor as administrator and try again.",
            code="open_failed") from exc
    if not handle:
        if error == 5:
            level = integrity_level(pid)
            if level is not None and level >= 0x3000 and not self_elevated():
                raise LiveError(
                    "Football Manager is running as administrator, and this "
                    "editor is not, so Windows will not let it read the game.",
                    "Close this editor and start it again with "
                    "\"Run as administrator\" (right-click the launcher and "
                    "choose that). The game can stay open.",
                    code="needs_admin")
            raise LiveError(
                "Windows refused access to the game's memory.",
                "Start this editor as administrator and try again.",
                code="denied")
        raise LiveError(
            f"Could not open the game process (Windows error {error}).",
            "Try starting this editor as administrator.",
            code="open_failed")
    return Win32ProcessMemory(pid=pid, handle=int(handle), exe=exe,
                              can_write=write)


# ---------------------------------------------------------------------------
# Adapter two: memory that lives in this Python process
# ---------------------------------------------------------------------------

class InMemoryProcessMemory(ProcessMemory):
    """The test adapter: the same interface over mapped byte blocks.

    ``map`` registers a block of memory; ``read``, ``write`` and
    ``readable_regions`` then behave the way the Win32 calls do:

    * ``read`` returns None when any byte of the range is not inside one mapped
      block, which is exactly ``ReadProcessMemory`` failing on an unmapped
      page;
    * ``write`` returns False when the range is not inside one *writable* block
      or when the attachment has no write rights;
    * ``readable_regions`` reports the mapped, readable spans in ascending
      address order, so a decoder that scans regions sees the same shape it
      sees in a real process - including two spans with an unmapped gap
      between them.

    It also counts ``writes``, which is how a test can prove that a refused
    edit wrote nothing at all.
    """

    def __init__(self, pid: int = 1, module_base: int = 0,
                 module_size: int = 0, can_write: bool = True):
        self.pid = int(pid)
        self.module_base = int(module_base or 0)
        self.module_size = int(module_size or 0)
        self.exe = ""
        self._write_rights = bool(can_write)
        self.blocks: dict[int, bytearray] = {}
        self.writable: dict[int, bool] = {}
        self.regions: list[tuple[int, int, bool]] = []
        self.writes = 0
        self.closed = False

    @property
    def can_write(self) -> bool:
        return self._write_rights and not self.closed

    # -- building a fake address space ---------------------------------
    def map(self, address: int, data: bytes = b"", size: int = 0,
            writable: bool = True) -> int:
        """Map one block at ``address`` and return the address.

        Either pass ``data`` or a ``size`` to allocate zeroes.  ``writable``
        decides both whether :meth:`write` accepts it and what
        :meth:`readable_regions` reports.
        """
        payload = bytearray(data) if data else bytearray(size)
        self.blocks[int(address)] = payload
        self.writable[int(address)] = bool(writable)
        return int(address)

    def declare_region(self, start: int, end: int,
                       writable: bool = True) -> None:
        """Report ``[start, end)`` from :meth:`readable_regions` as committed.

        The range may be wider than the bytes actually mapped inside it.  That
        is not a fiction: ``VirtualQueryEx`` reports a whole committed region,
        and ``ReadProcessMemory`` can still refuse a large range inside one, so
        the FM27 scanner has to retry in 64 KiB slices.  Declaring a region is
        how a test reproduces that shape; when no region is declared,
        :meth:`readable_regions` reports the mapped blocks themselves.
        """
        self.regions.append((int(start), int(end), bool(writable)))

    def put(self, address: int, data: bytes) -> None:
        """Write ``data`` into whichever block holds ``address``.

        No block is holding it yet, so one is created - this is the "just put
        these bytes at this address" helper the tests use to lay out a record.
        """
        address = int(address)
        for start, block in self.blocks.items():
            if start <= address and address + len(data) <= start + len(block):
                block[address - start:address - start + len(data)] = data
                return
        self.blocks[address] = bytearray(data)
        self.writable.setdefault(address, True)

    # -- the interface -------------------------------------------------
    def read(self, address: int, size: int) -> bytes | None:
        if self.closed or address <= 0 or size <= 0:
            return None
        for start, block in self.blocks.items():
            if start <= address and address + size <= start + len(block):
                offset = address - start
                return bytes(block[offset:offset + size])
        return None

    def write(self, address: int, data: bytes) -> bool:
        if self.closed or not data or address <= 0 or not self.can_write:
            return False
        for start, block in self.blocks.items():
            if (self.writable.get(start, True)
                    and start <= address
                    and address + len(data) <= start + len(block)):
                offset = address - start
                block[offset:offset + len(data)] = data
                self.writes += 1
                return True
        return False

    def readable_regions(self, start: int, limit: int) -> list[tuple[int, int, bool]]:
        if self.closed:
            return []
        if self.regions:
            # Declared regions win, so a test can report a committed range
            # whose interior read fails (the scanner's 64 KiB retry).
            spans = [(max(start, low), min(limit, high), writable)
                     for low, high, writable in self.regions
                     if min(limit, high) > max(start, low)]
            return sorted(spans)
        spans: list[tuple[int, int, bool]] = []
        for block_start, block in self.blocks.items():
            block_end = block_start + len(block)
            low = max(start, block_start)
            high = min(limit, block_end)
            if high > low:
                spans.append((low, high, self.writable.get(block_start, True)))
        spans.sort()
        merged: list[tuple[int, int, bool]] = []
        for low, high, writable in spans:
            if merged and merged[-1][1] == low and merged[-1][2] == writable:
                merged[-1] = (merged[-1][0], high, writable)
            else:
                merged.append((low, high, writable))
        return merged

    def close(self) -> None:
        self.closed = True

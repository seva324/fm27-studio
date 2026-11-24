# FM27 Studio — Live Memory Editor & AI Tactical Assistant

A pure-Python 3.12 (zero third-party dependency) desktop studio for **Football Manager 27**, combining real-time RAM reading/writing, snapshot browsing, player radar comparisons, FM27 facepack rendering, and an integrated multi-provider AI Tactical & Squad Assistant.

## Highlights

- **Zero Third-Party Dependencies**: Runs entirely on Python 3.12 standard library (`sqlite3`, `http.server`, `ctypes`, `urllib`). Local loopback server (`127.0.0.1`) with per-launch random token authentication, launched in a standalone Edge/Chrome `--app=` desktop window.
- **Live FM27 Memory Integration (Steam & Xbox / PC Game Pass)**:
  - Works out of the box with **Steam FM27** (standard Medium-integrity `fm.exe` — full read/write without needing Admin!) as well as **Xbox / PC Game Pass FM27** (run as Administrator if Windows runs Xbox games at High integrity).
  - Pre-registered Steam & Xbox RVAs (`27.0.x` / `27.1.x`) **plus** automatic hot-band + 160 MiB Denuvo-aware region scanner (`game_plugin.dll`) with table-relative date & human-manager resolution so new Steam patches continue working automatically.
  - Reads and writes player attributes, CA/PA, morale (`0–100`), condition, match sharpness, jadedness, contracts, display names, and live injuries directly in running `fm.exe` memory (with automatic Undo/Redo journaling).
- **Enhanced Fuzzy & Diacritic-Insensitive Search**: Searches across 42,000+ players, 23,000+ staff, and 42,000+ clubs with Unicode accent folding (`Modric` → `Luka Modrić`, `Mbappe` → `Kylian Mbappé`) and automatic typo-tolerant fallback (`Modrics` → `Luka Modrić`).
- **Player Comparison & 8-Axis Radar**: Interactive SVG radar chart, Best Role Rating, CA/PA, Market Value, and Weekly Wage comparisons by default, with one-click switch to full attribute-by-attribute diffs.
- **Local Facepack Auto-Detection**: Automatically loads portraits from your FM27 `graphics/faces` folder (configurable in Settings), with clean initials fallback.
- **Docked AI Assistant**:
  - Real-time SSE streaming with collapsible **Thinking Process** and **Tool Calls** drawers
  - Frosted-glass `/` Command Palette (`/analyze`, `/compare`, `/bestxi`, `/shortlist`, `/train`, `/heal`, `/inspire`)
  - Built-in Best XI Tactics pitch optimizer & 7-day Training schedule builder
  - Built-in providers: **Command Code**, **DeepSeek (Official API)**, **OpenCode Go**, **OpenRouter**, **OpenAI-compatible**, **Google Gemini**, **Claude Code CLI**, **Codex CLI**, **Antigravity CLI**, and **GitHub Copilot CLI**
  - One-click **Fetch Models** (`/models`) directly inside Settings
  - API keys encrypted locally via Windows DPAPI (`~/.fmrte-assistant/provider.key`) — never stored in the repository.
- **Clean Save & Reload Cache Management**: Temporary reload staging folders live strictly inside `saves/reloads/` and are pruned automatically on startup, on save, and periodically in the background.

## Quick Start

Double-click **`start_studio.bat`**, or run:

```powershell
python -S launch_studio.py
# or:
python -S main.py
```

### Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl + K` | Global omnibox search |
| `Ctrl + S` | Save / flush staged edits |
| `Ctrl + Z` | Undo last edit |
| `Ctrl + Y` / `Ctrl + Shift + Z` | Redo |
| `/` (in AI Chat) | Open AI Command Palette |

## Running Tests

```powershell
python -S -m unittest discover -s tests
```

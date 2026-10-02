# ableton-mcp (Julian's fork)

Fork of ableton-mcp 1.4.5 for the sounddesign knowledge base (`D:\dev\sounddesign`, roadmap sub-project 3).
Fork version `1.4.5+jh.N`, Remote Script version `1.7.1-jh.N` (must equal `EXPECTED_REMOTE_SCRIPT_VERSION` in
`MCP_Server/remote_script_install.py`).

## Fork changes

| Version | Change |
|---|---|
| `1.4.5+jh.1` / script `1.7.1-jh.1` | tool `listen(bars, source)` with Remote Script commands `listen_*` (2026-10-02) |

## Layout

- `AbletonMCP_Remote_Script/__init__.py` - runs inside Live 10.1 on **Python 2**: no f-strings, no type hints.
  The deployed copy needs `# -*- coding: utf-8 -*-` as its first line.
- `MCP_Server/` - the MCP server (Python 3). Flows that wait (e.g. `listen.py`) run here, never in the Remote Script.
- `MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py` - copy of the Remote Script that the tests load.

## Adding a capability

1. Remote Script: handler method, dispatch entry (main-thread list for anything that changes Live),
   `SCRIPT_CAPABILITIES`, bump `SCRIPT_VERSION` and `EXPECTED_REMOTE_SCRIPT_VERSION`.
2. Server: tool in `server.py`; logic that can be tested without Live in its own module.
3. Sync the bundle: `uv run --frozen python -m MCP_Server.remote_script_install --sync-bundle`.
4. Tests: `uv run --frozen --with pytest pytest -q` (fake LOM objects, see `tests/test_listen.py`).
5. Deploy: copy the Remote Script with the coding line to
   `D:\Programme\Ableton\Resources\MIDI Remote Scripts\AbletonMCP\__init__.py`; restart Live; reconnect the MCP.

## Registration (user scope)

`claude mcp add ableton -s user -e ABLETON_MCP_DISABLE_TELEMETRY=true -e ABLETON_MCP_DISABLE_DATASET=true --
uv --directory D:/dev/ableton-mcp run --frozen --no-sync ableton-mcp` - runs the working copy (editable install),
so a server change needs only an MCP reconnect. `--no-sync` matters: without it `uv run` reinstalls the package
after a version bump and fails while another session's server holds `.venv/Scripts/ableton-mcp.exe`. After a
version or dependency change run `uv sync --frozen` once with every Claude session closed.

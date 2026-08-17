@echo off
REM Manual one-off trigger for daily_brief — runs OUTSIDE the Claude Code
REM session env so claude.exe spawn (BUSINESS_ANALYST analyst path) works.
REM
REM Boss usage: double-click from Windows Explorer, OR run from a fresh
REM cmd.exe window (NOT from inside Claude Code's own terminal — env will
REM clash and brief falls back to pure-Python template).
REM
REM Output: instances/_TEMPLATE/runtime/briefs/queue/pending_<date>.md
REM brief_send_loop polls every 5 min and DMs boss via P01.

cd /d "%~dp0.."

REM Strip Claude Code parent env that breaks claude.exe spawn auth
set CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=
set CLAUDECODE=
set CLAUDE_CODE_ENTRYPOINT=
set CLAUDE_CODE_EXECPATH=
set CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH=
set CLAUDE_AGENT_SDK_VERSION=
set ANTHROPIC_API_KEY=
set ANTHROPIC_BASE_URL=

py processors\daily_brief.py daily

echo.
echo Brief composed. brief_send_loop will DM boss within 5 minutes.
pause

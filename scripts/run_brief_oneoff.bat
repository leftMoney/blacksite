@echo off
REM One-off non-interactive trigger for daily_brief — used by schtasks.
REM Logs to fixed path so we can verify execution unambiguously.

cd /d "%~dp0.."

set CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=
set CLAUDECODE=
set CLAUDE_CODE_ENTRYPOINT=
set CLAUDE_CODE_EXECPATH=
set CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH=
set CLAUDE_AGENT_SDK_VERSION=
set ANTHROPIC_API_KEY=
set ANTHROPIC_BASE_URL=

set LOG=%~dp0..\instances\_TEMPLATE\runtime\briefs\one_off.log

echo === oneoff start %date% %time% === >> "%LOG%"
echo cwd=%cd% >> "%LOG%"
echo PATH first 200=%PATH:~0,200% >> "%LOG%"

REM Use full path to python.exe to avoid PATH issues under Task Scheduler.
set "PYTHON="
if defined BLACKSITE_HOST_PYTHON if exist "%BLACKSITE_HOST_PYTHON%" set "PYTHON=%BLACKSITE_HOST_PYTHON%"
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not defined PYTHON (
  echo python executable not found >> "%LOG%"
  exit /b 2
)
"%PYTHON%" processors\daily_brief.py daily >> "%LOG%" 2>&1
echo === oneoff end exit=%ERRORLEVEL% %date% %time% === >> "%LOG%"

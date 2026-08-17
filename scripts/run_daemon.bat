@echo off
setlocal EnableDelayedExpansion

pushd "%~dp0.." & set "ROOT=%CD%" & popd
set "PYTHON="
set "PYTHONW="
if defined BLACKSITE_HOST_PYTHON if exist "%BLACKSITE_HOST_PYTHON%" set "PYTHON=%BLACKSITE_HOST_PYTHON%"
if defined BLACKSITE_HOST_PYTHONW if exist "%BLACKSITE_HOST_PYTHONW%" set "PYTHONW=%BLACKSITE_HOST_PYTHONW%"
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not defined PYTHONW if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PYTHONW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PYTHONW if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYTHONW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PYTHONW set "PYTHONW=%PYTHON%"
if not defined PYTHON (
    echo Blacksite python executable not found
    endlocal & exit /b 2
)
set "PIDFILE=%ROOT%\instances\_TEMPLATE\runtime\daemon.pid"
set "CODEX_EXE=%LOCALAPPDATA%\OpenAI\Codex\bin\codex.exe"

if exist "%CODEX_EXE%" (
    set "PATH=%LOCALAPPDATA%\OpenAI\Codex\bin;%PATH%"
)

"%PYTHON%" "%ROOT%\scripts\daemon_process_guard.py" ensure-one
if not errorlevel 1 (
    endlocal & exit /b 0
)

cd /d "%ROOT%"
start "" /B "%PYTHONW%" "%ROOT%\scripts\blacksite_daemon.py" >nul 2>nul
for /L %%I in (1,1,15) do (
    timeout /t 1 /nobreak >nul
    "%PYTHON%" "%ROOT%\scripts\daemon_process_guard.py" ensure-one
    if not errorlevel 1 (
        endlocal & exit /b 0
    )
)

echo Blacksite daemon launch timed out waiting for daemon.pid
endlocal & exit /b 1

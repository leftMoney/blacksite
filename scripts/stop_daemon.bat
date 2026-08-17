@echo off
setlocal EnableDelayedExpansion

pushd "%~dp0.." & set "ROOT=%CD%" & popd
set "PYTHON="
if defined BLACKSITE_HOST_PYTHON if exist "%BLACKSITE_HOST_PYTHON%" set "PYTHON=%BLACKSITE_HOST_PYTHON%"
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not defined PYTHON (
    echo Blacksite python executable not found
    endlocal & exit /b 2
)

"%PYTHON%" "%ROOT%\scripts\daemon_process_guard.py" stop-all
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

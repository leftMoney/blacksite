@echo off
REM Launch real Chrome with remote debugging port + chosen profile.
REM Engine then connects via CDP (chromium.connect_over_cdp) — no Playwright
REM browser launch, so no automation flags Google detects, and Chrome handles
REM all v20 / app-bound cookie encryption natively.
REM
REM Usage:
REM   scripts\launch_chrome_debug.bat                 -- defaults to Profile 3
REM   scripts\launch_chrome_debug.bat "Profile 1"     -- pick profile
REM
REM IMPORTANT: close ALL Chrome windows first. A second Chrome instance with
REM a different debug port will refuse to share user_data_dir lockfiles.

set PROFILE=%~1
if "%PROFILE%"=="" set PROFILE=Profile 3

set CHROME_EXE="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %CHROME_EXE% set CHROME_EXE="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

echo Launching Chrome with debug port 9222, profile=%PROFILE% ...
start "" %CHROME_EXE% ^
  --remote-debugging-port=9222 ^
  --remote-allow-origins=* ^
  --user-data-dir="%LOCALAPPDATA%\Google\Chrome\User Data" ^
  --profile-directory="%PROFILE%" ^
  --no-first-run ^
  --no-default-browser-check ^
  https://www.bigo.tv/

echo.
echo Chrome launched. CDP endpoint: http://localhost:9222
echo Next: py agents/bigo/login_via_cdp.py --persona P03

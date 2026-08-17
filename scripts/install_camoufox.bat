@echo off
REM Blacksite — Camoufox install (Windows).
REM Per fb_ig_strategy.md Phase 0 + Q2 default: engine writes the install script.
REM
REM Camoufox = anti-detect Firefox fork (MPL-2.0). Used by FB/IG sock-puppet
REM personas (P03/P04/P05). Pinned version 0.4.x to avoid breakage from
REM upstream fingerprint changes; quarantine new versions on test persona first.
REM
REM Usage: scripts\install_camoufox.bat
REM Idempotent: safe to re-run.

setlocal enableextensions

echo [install_camoufox] Step 1/3: pip install camoufox[geoip]^=0.4 (pinned)
py -m pip install --upgrade "camoufox[geoip]>=0.4,<0.5"
if errorlevel 1 (
    echo [install_camoufox] FAILED at pip install. Check Python ^>= 3.10.
    exit /b 1
)

echo.
echo [install_camoufox] Step 2/3: download Camoufox Firefox build
py -m camoufox fetch
if errorlevel 1 (
    echo [install_camoufox] FAILED at camoufox fetch. Check disk space ^(~200MB^).
    exit /b 1
)

echo.
echo [install_camoufox] Step 3/3: verify import
py -c "from camoufox.async_api import AsyncCamoufox; print('[install_camoufox] OK')"
if errorlevel 1 (
    echo [install_camoufox] FAILED at import verify.
    exit /b 1
)

echo.
echo [install_camoufox] DONE. Camoufox ready for FB+IG agents.
echo [install_camoufox] Next: py agents/facebook/register.py --persona P03
endlocal

# Blacksite — daemon watchdog (Task Scheduler every 5 min)
#
# Boss 5/6 directive (a): replace fragile Startup folder shortcut with
# Task Scheduler dual-trigger (at logon + every 5 min watchdog).
#
# Logic:
#   1. Check runtime/daemon.heartbeat freshness (≤ 5 min old)
#   2. Cross-verify PID alive
#   3. If either fails → run scripts/run_daemon.bat to spawn new daemon
#   4. Log every tick to runtime/logs/watchdog_<date>.log
#
# Idempotency: relies on run_daemon.bat new PID-check (won't double-spawn
# if existing daemon found alive at /pid file).

$ErrorActionPreference = "Stop"
$ROOT = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$HB = Join-Path $ROOT "instances\_TEMPLATE\runtime\daemon.heartbeat"
$CRON_ACTIVITY = Join-Path $ROOT "instances\_TEMPLATE\runtime\daemon.cron_activity"
$PID_FILE = Join-Path $ROOT "instances\_TEMPLATE\runtime\daemon.pid"
$LOG_DIR = Join-Path $ROOT "instances\_TEMPLATE\runtime\logs"
$RUN_BAT = Join-Path $ROOT "scripts\run_daemon.bat"
$STOP_BAT = Join-Path $ROOT "scripts\stop_daemon.bat"
$CODEX_BIN = Join-Path $env:LOCALAPPDATA "OpenAI\Codex\bin"
$CODEX_EXE = Join-Path $CODEX_BIN "codex.exe"

if (Test-Path $CODEX_EXE) {
    $env:CODEX_EXE = $CODEX_EXE
    $env:Path = "$CODEX_BIN;$env:Path"
}

if (-not (Test-Path $LOG_DIR)) { New-Item -ItemType Directory -Path $LOG_DIR | Out-Null }
$today = Get-Date -Format "yyyy-MM-dd"
$LOG = Join-Path $LOG_DIR "watchdog_$today.log"

function W($msg) {
    $ts = (Get-Date).ToUniversalTime().AddHours(7).ToString("yyyy-MM-ddTHH:mm:ss+07:00")
    "[$ts] [watchdog] $msg" | Add-Content -Path $LOG -Encoding utf8
}

function Resolve-HostPython {
    $candidates = @(
        $env:BLACKSITE_HOST_PYTHON,
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }
    throw "Blacksite python executable not found"
}

W "tick start"

# Health check requires BOTH (a) heartbeat fresh AND (b) pid file exists AND
# (c) pid points to live process. Heartbeat file alone is insufficient because
# Windows preserves file mtime even when the writing process dies (verified
# 5/6 17:55 — boss kill -9 left stale heartbeat 1.6m old).

$hb_fresh = $false
$cron_fresh = $false
$pid_ok = $false
$reason = ""

# (a) heartbeat freshness
if (Test-Path $HB) {
    $mtime = (Get-Item $HB).LastWriteTime
    $age_min = [math]::Round(((Get-Date) - $mtime).TotalMinutes, 1)
    W "heartbeat: age=$($age_min)m mtime=$mtime"
    if ($age_min -lt 5) {
        $hb_fresh = $true
    } else {
        $reason += "heartbeat stale $($age_min)m; "
    }
} else {
    $reason += "heartbeat file missing; "
    W "heartbeat: file missing"
}

# Cron activity freshness. Heartbeat only proves APScheduler can fire one tiny
# in-process job; 2026-05-11 showed heartbeat healthy while run_script jobs
# stopped. The daemon updates this file whenever a real scheduled script fires.
# Keep threshold wider than normal low-density windows (e.g. 19:30 -> 19:55).
if (Test-Path $CRON_ACTIVITY) {
    $cron_mtime = (Get-Item $CRON_ACTIVITY).LastWriteTime
    $cron_age_min = [math]::Round(((Get-Date) - $cron_mtime).TotalMinutes, 1)
    W "cron_activity: age=$($cron_age_min)m mtime=$cron_mtime"
    if ($cron_age_min -lt 40) {
        $cron_fresh = $true
    } else {
        $reason += "cron activity stale $($cron_age_min)m; "
    }
} else {
    $reason += "cron activity file missing; "
    W "cron_activity: file missing"
}

# (b)+(c) pid file + live process
if (Test-Path $PID_FILE) {
    $pid_str = (Get-Content $PID_FILE -Raw).Trim()
    if ($pid_str -match '^\d+$') {
        $pid_num = [int]$pid_str
        $proc = Get-Process -Id $pid_num -ErrorAction SilentlyContinue
        if ($proc) {
            $pid_ok = $true
            W "PID $pid_num alive (process: $($proc.ProcessName))"
        } else {
            $reason += "PID $pid_num not alive; "
            W "PID $pid_num NOT alive"
        }
    } else {
        $reason += "pid file corrupt; "
    }
} else {
    $reason += "pid file missing; "
    W "pid file missing"
}

$alive = $hb_fresh -and $cron_fresh -and $pid_ok

if ($alive) {
    W "daemon HEALTHY — skip ($reason)"
} else {
    W "daemon UNHEALTHY ($reason) — issuing restart"
    try {
        W "stopping existing daemon/listener/runner processes before restart"
        try {
            & $STOP_BAT | ForEach-Object { W "stop: $_" }
        } catch {
            W "stop before restart failed: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 2
        # Spawn via `python` (NOT pythonw) with stdout/stderr redirected to files.
        # 5/6 verified: pythonw under Hidden window dies silently; python with
        # redirects stays alive. The redirects give print() a real handle.
        $python = Resolve-HostPython
        $script = Join-Path $ROOT "scripts\blacksite_daemon.py"
        $today = Get-Date -Format "yyyy-MM-dd"
        $stdoutLog = Join-Path $LOG_DIR "daemon_stdout_$today.log"
        $stderrLog = Join-Path $LOG_DIR "daemon_stderr_$today.log"
        $proc = Start-Process -FilePath $python `
                              -ArgumentList $script `
                              -WorkingDirectory $ROOT `
                              -WindowStyle Hidden `
                              -RedirectStandardOutput $stdoutLog `
                              -RedirectStandardError $stderrLog `
                              -PassThru
        W "spawned daemon PID=$($proc.Id) (python.exe with stdout->$($stdoutLog | Split-Path -Leaf))"
        Start-Sleep -Seconds 10
        # Verify new daemon by checking heartbeat freshness post-spawn
        if (Test-Path $HB) {
            $new_age = [math]::Round(((Get-Date) - (Get-Item $HB).LastWriteTime).TotalMinutes, 2)
            if ($new_age -lt 1) {
                W "post-restart: heartbeat fresh $($new_age)m -- restart OK"
            } else {
                W "post-restart: heartbeat still stale $($new_age)m — restart may have failed"
            }
        } else {
            W "post-restart: heartbeat file still missing — restart failed"
        }
    } catch {
        W "RESTART FAILED: $($_.Exception.Message)"
    }
}

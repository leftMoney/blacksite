"""Bulk smoke-test all 12 LIVE Field Agents — verify_only mode.

Reads persona_warmup_schedule.yaml, runs each agent's warmup_session.py
sequentially (anti-overlap), reports pass/fail.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import subprocess
import time
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# platform → script directory (most are same as platform name; X is named "twitter")
PLATFORM_TO_DIR = {
    "twitter_x": "twitter",
    # others: match dir name = platform name
}

def script_path(platform: str) -> Path:
    plat_dir = PLATFORM_TO_DIR.get(platform, platform.lower())
    return ROOT / "agents" / plat_dir / "warmup_session.py"


def main():
    schedule = yaml.safe_load(
        (ROOT / "instances/_TEMPLATE/policy/persona_warmup_schedule.yaml").read_text(encoding="utf-8"))
    windows = schedule["daily_windows"]
    print(f"loaded {len(windows)} agents from schedule\n")

    results = []
    for w in windows:
        aid = w["agent_id"]
        persona = w["persona"]
        platform = w["platform"]
        script = script_path(platform)
        if not script.exists():
            print(f"❌ {aid:<22} script missing: {script.relative_to(ROOT)}")
            results.append((aid, "missing_script", None))
            continue

        print(f"--- {aid} (persona={persona} platform={platform}) ---")
        cmd = [sys.executable, str(script), "--persona", persona, "--mode", "verify_only"]
        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            elapsed = time.time() - t0
            # Look for verify result lines
            verified = "✅ verified logged in" in r.stdout
            not_logged = "❌ NOT logged in" in r.stdout
            other_err = r.returncode != 0 and not (verified or not_logged)
            if verified:
                # extract marker
                line = next((l for l in r.stdout.splitlines() if "verified logged in" in l), "")
                print(f"  ✅ PASS in {elapsed:.1f}s — {line.split('via marker')[-1].strip() if 'via marker' in line else '(marker)'}")
                results.append((aid, "PASS", elapsed))
            elif not_logged:
                print(f"  ❌ NOT-LOGGED-IN in {elapsed:.1f}s — markers may need update or cookies expired")
                results.append((aid, "NOT_LOGGED_IN", elapsed))
            else:
                tail = "\n  ".join(r.stdout.splitlines()[-5:] + r.stderr.splitlines()[-3:])
                print(f"  ⚠ ERR rc={r.returncode} in {elapsed:.1f}s: {tail[:300]}")
                results.append((aid, f"err_rc{r.returncode}", elapsed))
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            print(f"  ⚠ TIMEOUT after {elapsed:.0f}s")
            results.append((aid, "timeout", elapsed))
        except Exception as e:
            print(f"  ⚠ EXC {type(e).__name__}: {e}")
            results.append((aid, f"exc_{type(e).__name__}", None))
        # Cooldown between launches (anti-detection + Camoufox cleanup)
        time.sleep(2)

    print("\n=== SUMMARY ===")
    print(f"{'agent':<25} {'result':<18} {'sec':>6}")
    for aid, status, elapsed in results:
        sec = f"{elapsed:.1f}" if elapsed else "-"
        print(f"{aid:<25} {status:<18} {sec:>6}")
    pass_count = sum(1 for _, s, _ in results if s == "PASS")
    print(f"\n{pass_count}/{len(results)} PASSED")

if __name__ == "__main__":
    main()

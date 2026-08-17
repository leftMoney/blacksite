"""One-shot: ingest handoff_agent/logs/register_log.jsonl into system_history.

RETIRED 2026-05-06: ingest completed (system_history #663-676). handoff_agent/
removed by boss directive same day. Script kept as ingest-pattern reference;
re-running will fail with FileNotFoundError on the LOG path.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import json
from pathlib import Path
from collections import defaultdict
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from processors.history_log import log_event

LOG = Path("handoff_agent/logs/register_log.jsonl")
lines = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
print(f"loaded {len(lines)} log lines")

# Final status per (persona, platform)
final = {}
for ev in lines:
    key = (ev["persona"], ev["platform"])
    if ev["event"] == "register" and ev["status"] in ("success", "failed", "abandoned_opsec"):
        final[key] = ev

print()
print("=== final per-persona-platform ===")
for (p, plat), ev in sorted(final.items()):
    flag = "✅" if ev["status"] == "success" else ("❌" if ev["status"] == "failed" else "⚠")
    print(f"  {flag} {p:>3} / {plat:<10} {ev['status']:<18} @ {ev['ts']}  {ev['detail'][:60]}")

# Bulk insert: 1 milestone per final success/failure (skip noisy launch/step events)
print()
print("=== ingesting final events into system_history ===")
parent_root = log_event(
    actor="hotel_cc", kind="milestone", scope="persona",
    title="Hotel Kit handoff merge ingested — register_log 93 events, 12/13 platforms",
    body="hotel CC 5/4-5/6 in-country residential IP register batch. 13 platform×persona scope per scope.yaml. 11 success at handoff time + P03 TikTok recovered 5/6 11:19 (12/13). P05 FB abandoned_opsec; P05 LocalForum pending cooldown.",
    refs=["handoff_agent/HANDOFF.md", "handoff_agent/logs/register_log.jsonl",
          "handoff_agent/scope.yaml"])
print(f"  parent: history#{parent_root}")

count = 0
for (p, plat), ev in sorted(final.items()):
    kind = "milestone" if ev["status"] == "success" else "warning"
    title = f"register {p}/{plat}: {ev['status']}"
    body = f"persona={p} platform={plat} status={ev['status']} ts={ev['ts']}\ndetail: {ev['detail']}"
    hid = log_event(
        actor="hotel_cc", kind=kind, scope="persona",
        title=title, body=body,
        parent_id=parent_root,
        ts=ev["ts"],
        refs=[f"personas/{p}/state/{plat}_storage_state.json", "handoff_agent/HANDOFF.md"])
    count += 1
print(f"  ingested {count} final events")

# also log VPN config decision
hid_vpn = log_event(
    actor="boss", kind="directive", scope="opsec",
    title="FlyVPN default OFF — local diaspora cover story acceptable (5/6)",
    body=("Boss 2026-05-06 directive: 接手 hotel-registered 帳號桌機這側不必每次掛 FlyVPN target-country; "
          "在操作所在地的 target-country 僑民 (local diaspora) 為合理 cover story. .env 加 FLYVPN_BINARY_PATH "
          "+ FLYVPN_DEFAULT_MODE=off + FLYVPN_FALLBACK_REGIONS=<target-country-code>. "
          "Fallback only when geo-walled content needs in-country IP. FlyVPN.exe (PID 18924) running but "
          "tunnel 未連; current public IP <redacted> / <operator-city> (<ISP>)."),
    refs=[".env", "handoff_agent/HANDOFF.md"])
print(f"  VPN config decision: history#{hid_vpn}")

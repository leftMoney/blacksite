"""One-shot audit: parse today's daemon log, find which cron jobs ran/ok/fail."""
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
import re
from collections import defaultdict

log = Path('instances/_TEMPLATE/runtime/logs/daemon_2026-05-04.log').read_text(encoding='utf-8', errors='replace')
lines = log.splitlines()
print(f'lines: {len(lines)}, first ts: {lines[0][:30]}, last ts: {lines[-1][:30]}')
print()

job_runs = defaultdict(lambda: {'run': 0, 'ok': 0, 'fail': 0})
for ln in lines:
    m_run  = re.search(r'\] run [^\s]*?([\w_]+\.py)', ln)
    m_ok   = re.search(r'\] ok [^\s]*?([\w_]+\.py)', ln)
    m_fail = re.search(r'\] FAIL [^\s]*?([\w_]+\.py): rc=', ln)
    if m_run:  job_runs[m_run.group(1)]['run'] += 1
    if m_ok:   job_runs[m_ok.group(1)]['ok'] += 1
    if m_fail: job_runs[m_fail.group(1)]['fail'] += 1

print(f"{'job':<40} {'run':>5} {'ok':>5} {'fail':>5}")
for job, d in sorted(job_runs.items()):
    if sum(d.values()) > 0:
        print(f"{job:<40} {d['run']:>5} {d['ok']:>5} {d['fail']:>5}")

print()
print('=== silent-cron audit: lines mentioning archive/ocr/brief/lifecycle 5/4 ===')
keywords = ['archive_daily', 'ocr_gemini', 'daily_brief', 'lead_lifecycle', 'run_ocr',
            'asr_whisper', 'tg_pattern_miner', 'entity_decay', 'run_archive']
hits = defaultdict(list)
for ln in lines:
    for kw in keywords:
        if kw in ln:
            hits[kw].append(ln[:150])
            break
for kw in keywords:
    print(f'\n[{kw}] {len(hits[kw])} lines')
    for ln in hits[kw][:3]:
        print(f'  {ln}')
    if len(hits[kw]) > 3:
        print(f'  ... +{len(hits[kw])-3} more')

print()
print('=== daemon up/down events 5/4 ===')
for ln in lines:
    low = ln.lower()
    if any(t in low for t in ['daemon up', 'shutdown', 'shutting down']):
        print(f'  {ln[:140]}')

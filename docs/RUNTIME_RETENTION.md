# Runtime Retention Contract

Active instance runtime data is short-lived operational evidence unless it has
already been promoted into SQLite / KB / cards.

## Canonical Layout

| Path | Purpose | Retention |
|---|---|---|
| `instances/<id>/runtime/media/<platform>/<agent>/<YYYY-MM-DD>/...` | Collected media blobs used by OCR/ASR and media audit | 7 days |
| `instances/<id>/runtime/raw/<source>/.../<YYYY-MM-DD>.jsonl` | Raw collector events used by indexer and sampling | 7 days |
| `instances/<id>/runtime/screenshots/...` | Browser/account/debug evidence screenshots | 7 days |
| `instances/<id>/runtime/artifacts/root_scratch/<YYYY-MM-DD>/...` | Manually generated OCR/crop/debug files moved out of repo root | 7 days |
| `instances/<id>/runtime/artifacts/root_scratch_dirs/<YYYY-MM-DD>/...` | Manually generated temp/crop directories moved out of repo root | 7 days |
| `instances/<id>/runtime/archive/*.gz` | Legacy compressed raw/log archives from old archiver | 7 days |
| `instances/<id>/runtime/reports/retention/*.json` | Dry-run/commit reports for audits | retained with reports policy |

## Rules

- `scripts/retention_sweep.py` defaults to dry-run and writes a JSON report.
- `--commit` is required for any move or delete.
- `--dry-run` forces audit-only mode even when `.env` enables commit.
- Daemon runs daily at `02:40 GMT+7`, before `archive_daily`; it is dry-run unless
  `BLACKSITE_RETENTION_COMMIT=1` is set in `.env`.
- The script only operates inside the repository root and known runtime paths.
- It never touches `.env`, DB files, code, `personas/*/state`, or `System/`.
- Root scratch files are moved, not deleted, so humans can recover recent OCR
  experiments from `runtime/artifacts` for seven days.
- Runtime media/raw/screenshots older than seven days are deleted on commit; the
  indexed DB metadata remains the long-term query surface.

## Manual Commands

Dry-run:

```powershell
python scripts/retention_sweep.py --retain-days 7
```

Commit after boss approval:

```powershell
python scripts/retention_sweep.py --retain-days 7 --commit
```

Enable daily automatic commit after boss approval:

```dotenv
BLACKSITE_RETENTION_COMMIT=1
```

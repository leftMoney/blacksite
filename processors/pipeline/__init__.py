"""3-stage hybrid OCR/KB-decision pipeline (CLAUDE.md §2.1, boss 2026-05-08).

Stage 1 — stage1_qwen_filter   — Qwen2.5-VL 7B local Ollama   — 100% media, ~75% rejected
Stage 2 — stage2_haiku_precision — Claude Haiku 4.5 OAuth     — ~25% (Stage1 signal only)
Stage 3 — stage3_sonnet_strategic — Claude Sonnet via claude.exe — ~5% (Stage2 score>=70)

Plus:
  audit_sonnet  — daily 06:00 N=20 + weekly Mon 07:00 N=100, drives improvement loop
  improvement   — auto-proposal generator on audit warning/critical
  promote_to_kb — promotes Stage2-admitted rows into kb_documents/kb_chunks

Each stage is independent and resume-safe. Run them as separate cron jobs.
"""

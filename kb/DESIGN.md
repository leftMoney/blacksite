# Blacksite KB (L5) — Design v1 [DRAFT — pending boss approval]

> 範圍：覆蓋 boss 2026-05-02 提的 7 個需求；架構 instance-agnostic（country × domain）。
> 狀態：**DRAFT — 等 boss 批准後才動工**。
> 起跑時機：(a) SQLite messages > 100K 或 (b) OCR backlog > 5K 或 (c) 跨模態 / 跨平台 query 需求出現 或 (d) boss 直接 green-light。

---

## 0. TL;DR

L5 KB v2 = **Onyx fork (Docker)** + **Qdrant** vector + **embedded graph** + **時序強制** + **價值閘 + 稽查 sweeper**，cadence 分 realtime/daily/weekly/monthly 自動跑。新增群/帳號**零程式碼**（adapter pattern + yaml seed）。每條 KB chunk 帶 `observed_at + event_at + valid_window + provenance + signal_score`，agent query 必含 time-window 防混時序。

**5/2 boss directive 加層**：§21 **Manager Context Pack**（cadence-pre-compiled briefing, 30K/70K/100K tokens, manager agent 進場直接吃）+ §22 **Long-Range Task Ledger**（task lifecycle yaml + auto-monitoring + due-date alerts）+ §23 **Full-Auto Pipeline**（§15/§20 共 10 題改 engine auto-default + boss 7-day TG `/revert` 回退；EEI engine auto-propose 24h auto-approve）。

Phase 0/1/2/2.5-7/2.8/3 階段化共 **29-37 天**工程量，v1 SQLite 並行跑 6 週 shadow 才 cutover。

---

## 1. 為什麼 v1 還不上 L5

當前 SQLite-only 撐得住理由：~10K msgs / 2K entities / 1.9K media / ~330 MB。query 都 SQL `WHERE/JOIN` 亞秒回。所有 v1 工作（agent fleet / persona / cron）優先把情報源側補齊（P04/P05 上線、FB 14 KOL Pages 抓回來），KB 是後段消費端。**情報源 first，KB 等 volume 到了再升級**。

---

## 2. KB 核心承諾（對應 boss 7 點）

| Boss 點 | KB 對應承諾 |
|---|---|
| 1 擴充性 | 兩維度擴充：(a) **平台**：adapter pattern 每平台一個；(b) **帳號池**：同平台多 persona / multi-platform 跨 persona / burn-and-replace 不破壞 KB。entity 是 platform-level 不綁 persona |
| 2 進場 KPI + 稽查 | 6 維度 signal_score + 進場閾值 + 抽樣 LLM auto-grade precision/recall |
| 3 遺漏稽查 | Coverage map per (platform × topic × time-window) + Negative-space detector + cross-platform reconciliation |
| 4 圖書館規模 query | Qdrant vector + embedded graph + Onyx hybrid search + agent query API 走 graph traversal 而非 keyword match |
| 5 時間是核心 | 三種時戳強制（observed_at / event_at / valid_from-to）+ time-aware query default 7 天 rolling + per-entity-kind 半衰期 |
| 6 「沒想到」策略 | Shadow ingest / Adversarial honey signal / Cross-instance KB share / Multimodal 早期投資 / Provenance ledger |
| 7 短中長 cadence | Realtime / Daily 19:00 / Weekly 週日 21:00 / Monthly 月 1 號 03:00；每 cadence 自帶 audit checkpoint |
| 🆕 5/2 goal #1 | §11 cadence + §21 Manager Pack 三 cadence 自動 build + audit |
| 🆕 5/2 goal #2 經理人 single-context | §21 Manager Context Pack 30K/70K/100K self-sufficient briefing |
| 🆕 5/2 goal #3 中長期排程+完成監控 | §22 Long-Range Task Ledger（lifecycle + auto metrics_query + due-date alerts + brief 整合） |
| 🆕 5/2 全程自動化 directive | §23 Full-Auto Pipeline：§15/§20 改 auto-default + 7-day TG `/revert` window |

---

## 3. Stack 選型

| 層 | 元件 | 為什麼選 | 替代品 + 不選原因 |
|---|---|---|---|
| Search engine | **Onyx (fork from danswer-ai)** | OSS、enterprise-search 等級、自帶 hybrid search (BM25+vector)、支援 connector 抽象 | LlamaIndex 純 lib 太低層；Quivr、Verba less mature |
| Vector DB | **Qdrant** | Rust、單機快、payload filtering 強（time-window 必用）、Onyx 有官方 connector | Weaviate / Milvus 都行；Qdrant 部署最輕 |
| Graph DB | **FalkorDB**（embedded 跑進 Redis） | embedded 不增 ops 負擔，TG funnel edges 已是 graph-native data | Neo4j 太重；NetworkX in-memory 撐不住 100K+ |
| Embedding | text: **bge-m3** (multilingual, 含 target-market 語言)；image: **CLIP ViT-L** + **Qwen2.5-VL** for caption；audio: faster-whisper 已有 | 在地語文本必選 multilingual，bge-m3 是 SOTA OSS | OpenAI ada-002 不支援多數在地語 / 收錢 |
| Orchestrator | LangGraph (per §2 framework choice) | KB ingestion DAG 的 retry / dedup / value-gate 樂趣寫成 nodes | Airflow / Prefect 太重 |
| Blob | **SeaweedFS** (per §2) | media file scale 用，hash-keyed | S3 / MinIO 都行；SeaweedFS 部署最簡 |

---

## 4. 資料模型

### 4.1 三層 abstraction

```
RawObservation (raw JSONL line / media file blob)
    ↓ ingest
Document (canonical: 1 message OR 1 media OR 1 page-snapshot)
    ↓ chunk + embed
Chunk (語意片段 + vector + payload metadata)
    ↓ extract
Entity (brand / KOL / channel / domain / promo_code / lottery_number / phone)
    ↓ relate
Relationship (TG channel pushes URL ; KOL mentions number ; brand owns domain)
    ↓ infer
Card (boss-readable insight, actionability scored, decay-aware)
```

### 4.2 強制 schema 欄位（每層都有）

| 層 | 必有時戳 | 必有 provenance | 必有 signal score | 必有 valid window |
|---|---|---|---|---|
| RawObservation | observed_at | persona_id + platform | — | — |
| Document | observed_at + event_at | source_blob_hash + persona_id | raw_signal_score | 觀察期間 |
| Chunk | inherit | inherit | aggregated | inherit |
| Entity | first_seen / last_seen | originating_chunks[] | actionability + decay_class | structural / 7d / 14d / 30d |
| Relationship | first_observed / last_observed | edge_evidence_chunks[] | confidence | inherit |
| Card | last_built_at + window_covered | based_on_entities[] + based_on_relationships[] | actionability | decay_class |

### 4.3 反向 link

每個 Chunk → Document → RawObservation 的 chain：
- Chunk.source_doc_id
- Document.source_blob_hash → SeaweedFS file id
- 任何 boss 看到的 KB output 都能一鍵跳回原始 raw（image / message / livestream timestamp）

---

## 5. Ingestion pipeline（boss 第 1 點 — 擴充性）

### 5.1 Source adapter pattern

```python
# kb/adapters/<platform>.py
class TelegramAdapter(SourceAdapter):
    platform = "telegram"
    def yield_documents(self, since: datetime) -> Iterator[Document]:
        # read raw/<persona>/<date>.jsonl since cursor
        # yield Document per message
```

每平台一個 adapter，遵循統一接口。**新增平台 = 寫一個 adapter** + register 進 `kb/adapters/__init__.py`。

### 5.2 新增群 / 帳號 zero-code 流程

| 動作 | Boss 改哪裡 |
|---|---|
| 加新 TG channel keyword 搜尋 | `instances/_TEMPLATE/policy/tg_search_seeds.yaml` 加一行 |
| 加新 KOL FB Page 監控 | `instances/_TEMPLATE/policy/facebook_pages.yaml` 加一行 |
| 加新 persona | `personas/P0X/profile.yaml` + `.env` 補 cred；adapter 自動抓新 raw |
| 加新整個平台 | 寫一個 `kb/adapters/<new_platform>.py` + register（**唯一需碰程式**） |

### 5.3 Idempotency + cursor

- `ingestion_runs` table 已有 last_offset（v1 SQLite 已用），v2 沿用
- Document 用 `source_blob_hash + observed_at` 為 idempotency key
- Re-ingest 全 batch 不會 dup chunks

### 5.4 帳號池擴充（multi-persona / multi-platform / burn-and-replace）

🔴 **核心紀律**：entity 是 **platform-level**，**不綁 persona**。同個 channel `examplebrand` 不論 P01 還是 P02 看到都是同個 entity_id（不會因 persona 不同而 dup）。

#### 5.4.1 Persona-agnostic entity model

| 表 | 綁 persona？ | 為什麼 |
|---|---|---|
| `RawObservation` (raw JSONL line) | ✅ 綁 persona_id | 知道誰看到 |
| `ingestion_runs.cursor` | ✅ per-persona | 各自接續 |
| `Document.observed_by[]` | ✅ list 多 persona | 同訊息可能多 persona 看到 |
| `Chunk` | ✅ via Document | inherit |
| **`Entity`** | ❌ **platform-level** | examplebrand = examplebrand，誰看到都同 |
| **`Relationship`** | ❌ **platform-level** | 同上 |
| **`Card`** | ❌ **synthesized**, persona-agnostic | boss 看到的不該分 P01/P02 |

#### 5.4.2 帳號池場景

| 場景 | 處理 |
|---|---|
| **同平台多 persona 看不同 segment** (P03 蛋黃 folk-belief / P04 蛋白 sports) | adapter 並行 ingest 多 persona raw；entity 自動 merge（同 channel 各 persona 看到的 messages 合併進同個 Document chain） |
| **同訊息被多 persona 看到** (P01+P02 都在 @ExampleFunnelChat) | Document.observed_by = [P01, P02] + corroboration_count +1（信號分自動上去） |
| **跨平台同 brand entity** (TG `examplebrand` + web `examplebrand.com` + FB `examplebrand.official`) | entity_resolution 走 promo-code / sender / domain pattern → 同 entity_id |
| **新 platform 上線** (例：未來 LINE OpenChat / 其他在地社群平台 解鎖) | 寫一個 `kb/adapters/<new_platform>.py` + register；entity 自動跨平台 merge 既有 brand |
| **新 persona pool** (例：P06-P10 上線) | adapter 自動掃 `personas/P0*/` dir；無需改 KB code，純資料層擴充 |
| **persona burn**（P03 burn → P03b 替換） | (a) 已 ingest 的 RawObservation 仍合法情報，留 KB 不變；(b) future ingestion 自動切到 P03b cursor；(c) entity 不影響（persona-agnostic） |
| **多 instance 共用 brand**（例：_TEMPLATE + 未來 PH-XX 都看到 examplebrand） | §10.3 cross-instance entity share — entity 帶 cross_instance_id 全 framework 共用 |

#### 5.4.3 Discovery / registration 自動化

新 persona / 新群 / 新平台**完全 yaml-driven**：

```
personas/P0X/profile.yaml       ← 新 persona drop-in，daemon 下次 cron 自動掃
instances/<X>/policy/<plat>.yaml ← 新 keyword / 新群 / 新 KOL Page 自動 ingest
kb/adapters/<plat>.py            ← 新 platform 才需動程式（一個檔，標準 interface）
```

#### 5.4.4 Cap & rate limit per persona pool

- 每 persona 有獨立 ingestion budget（cron schedule + listener event rate）
- KB 不限制 persona 數，但 daemon level 有 max_concurrent_ingest 防爆 CPU/IO
- multi-persona 並行 ingest 對 entity merge 採 last-writer-wins（observed_at 時序 deterministic）

#### 5.4.5 帳號池 OPSEC（per §9.1a + persona axis isolation）

- KB schema **不存 persona axis（email/phone/handle）** — 那些只在 yaml + .env
- KB 只存「persona_id 標籤」 + 該 persona 看到的 chunks
- 即使 KB 外洩 → 只露 persona_id（如「P03」）不露真名 / phone / email
- entity 跨 persona / 跨 platform 但永不暴露 axis

---

## 6. Value gate — 進 KB KPI（boss 第 2 點）

### 6.1 Signal score（6 維度，每維 0-1，加權後 sum）

| 維度 | 算法 | 權重 |
|---|---|---|
| **entity_density** | (P0-tagged entity count in chunk) / (chunk token count / 100) | 0.25 |
| **amplification** | log10(views + forwards*5 + replies*10) / 4，clamp [0,1] | 0.15 |
| **novelty** | 1 - max(cosine_sim vs existing chunks in last 7 days)；高 novelty = 沒重複 | 0.20 |
| **corroboration** | 多源獨立確認，跨 platform 數，max=5 | 0.15 |
| **intent_polarity** | promo=1.0 / bait=0.8 / infomercial=0.6 / other=0.3 / noise=0 | 0.15 |
| **source_trust** | from_chat 信任分（cards.actionability 或 baseline 0.5） | 0.10 |

### 6.2 進場閾值

| Tier | signal_score 門檻 | 自動進 KB | 留 review |
|---|---|---|---|
| 🟢 yolk-grade | ≥ 0.65 | yes | — |
| 🟡 white-grade | 0.40 - 0.65 | yes (lower priority) | — |
| 🔴 below noise floor | < 0.40 | no | — |
| 🟠 borderline | 0.40 ± 0.05 OR 含 §11 elevated-risk venue keyword | no | yes (boss queue) |

### 6.3 棄置機制

- 每 chunk 進 KB 帶 `decay_class`（structural / 30d / 14d / 7d）
- Monthly compaction 跑 entity_decay 邏輯（v1 已有 entity_decay.py）— 把過期 chunk 從 hot tier 移 cold tier
- Cold tier 仍 query-able 但 default time-window 不抓

### 6.4 稽查 — precision / recall sample

每 daily 19:00 brief 跑前：
1. 抽樣 100 條當日進場 chunks
2. 抽樣 100 條被 reject 的 chunks（signal_score 低於閾值）
3. 丟給 Gemini DR 用「_TEMPLATE 蛋黃/白/殼 scope」prompt 評是否該進
4. 算 precision = (LLM 認可的 ÷ 100 進場) / recall = (LLM 認可的 reject 中 真該進 / 100 reject)
5. 寫進 daily brief + dashboard，連續 7 天 precision < 0.85 或 recall miss > 5% → boss alert 調權重

---

## 7. 遺漏稽查（boss 第 3 點 — 確保沒漏）

### 7.1 Coverage map

每 weekly sweep：build (platform × topic × week) 矩陣，每 cell 記 chunks_count。對每 cell 比對歷史 baseline，超 ±50% flag「異常 over/under」。

### 7.2 Negative-space detector

| 信號 | 閾值 | 動作 |
|---|---|---|
| P0 KOL ≥ 7 天無 fresh chunk | weekly 跑 | flag「KOL 沉默 — 真死還是 listener miss」交叉 web crawl 驗證 |
| 平台 24h 0 訊息 | daily 跑 | 比對 baseline，flag listener 健康問題 |
| 已知 brand entity 30 天無 push | monthly 跑 | flag「brand 是否退場 / 改名 / 收押」 |
| Funnel-mouth chat 7 天無 outbound edge | weekly 跑 | flag「mouth 失能 / 操作員換群」 |

### 7.3 Cross-platform reconciliation

對每個高分 entity（actionability ≥ 0.7）跑「mirror discovery」：
- TG channel `examplebrand` → 自動 web crawl `examplebrand.*` domain → FB search `examplebrand.official` → IG / TikTok / X handle search
- 缺哪邊就 flag 進 next-week brief「需補抓平台 X 上 entity Y」

### 7.4 Periodic completeness sweep

| Cadence | 對象 | 邏輯 |
|---|---|---|
| Weekly | P0 KOL 名單（facebook_pages.yaml + 同類） | 每人最近一週是否 ≥1 fresh chunk |
| Weekly | 17 個 brand seed | 每 brand 是否有 fresh push 紀錄 |
| Monthly | 44 canonical brands (M8) | 全 brand coverage map vs yolk/white 清單；漏哪個 escalate boss |

---

## 8. 海量 query 關聯（boss 第 4 點 — 圖書館規模）

### 8.1 三層 query stack

```
Agent question
  ↓
Onyx hybrid retriever (BM25 + Qdrant vector + payload filter)
  → top-50 chunks
  ↓
Graph expansion (FalkorDB)
  → 對每 chunk 取出 mentioned entities → 1-hop neighbours → relevant chunks
  ↓
Re-rank by (signal_score × time_decay × graph_proximity)
  → top-10 chunks back to agent
```

### 8.2 為什麼要 graph

純 vector retrieval 答不出「examplebrand 跟 examplebrand 是不是同一個 operator?」。Graph 答得出（兩 channel 共享 sender_external_id 或 共享 promo CODE pattern → 同 operator entity）。

### 8.3 Agent query API

```python
kb.query(
    question="哪些 KOL 在過去 30 天推薦了 lottery 號碼 7 / 8 / 14?",
    time_window=("2026-04-01", "2026-04-30"),  # 強制 time-window
    scope=["yolk", "white"],
    include_provenance=True,  # 每條 chunk 附 raw blob link
)
→ {
    chunks: [...],
    entities: [...],
    timeline: [...],   # 時序排列，不是 random order
    sources: [{blob_hash, platform, persona}, ...]
}
```

### 8.4 Default 行為

- 任何 query 沒給 time_window → 自動套 7 天 rolling
- 任何 query 沒給 scope → 默認 yolk + white（殼預設不抓，除非顯式要）
- LLM agent 用 KB 必透過這 API，不能自由 SQL（防止跨時序混淆）

---

## 9. 時間維度（boss 第 5 點 — 時間是核心）

### 9.1 三種時戳

| 時戳 | 意義 | 例 |
|---|---|---|
| `observed_at` | engine 觀察到此資料的時點 | 「TG listener 04-30 18:42 收到這條 message」 |
| `event_at` | message / event 真實發生時點 | 「KOL livestream 04-30 19:00 開的 714」 |
| `valid_from / valid_to` | fact 仍有效的時間範圍 | 「examplebrand promo CODE 4T8QS77R 於 04-30 09:45 至 05-01 09:45 有效」 |

每 KB chunk **三個都要有**（缺則 fallback observed_at = 唯一可信值）。

### 9.2 Time-aware query default

```
默認 time-window = past 7 days rolling (per §9.1 observed_at)
查詢「上半年彩券大事」 → 必顯式給 time_window
時序排列預設 newest-first；對比兩段時間用 split window
```

### 9.3 半衰期 per entity_kind

| entity_kind | decay_class | half-life | 例 |
|---|---|---|---|
| KOL persona | structural | 永久 | ExampleKOL 直到她退場 |
| Brand | structural | 永久 | examplebrand 直到 enforcement 收掉 |
| Promo CODE | 7d | 7 天 | `EXAMPLECODE-1234` 隔週就無效 |
| Lottery 號碼 hint | 14d | 14 天 | KOL 推「714」涵蓋當期+下期開獎 |
| Regulatory weather | 30d | 30 天 | 政策訊號 |
| Funnel push edge | 14d | 14 天 | TG 推銷文案 |

decay 後 chunk 進 cold tier，hot tier query 預設不撈。boss 顯式查歷史可全 tier scan。

### 9.4 Snapshot index

每週日 23:55 snapshot 一份「KB state-at-week」 → 累積成 timeline。月底 / 季末 boss 可 query「KB 在 4/15 那天的 view 跟今天差什麼」（用於回溯：看當時情報藏哪裡 / 為什麼錯過）。

---

## 10. 「沒想到」策略（boss 第 6 點）

### 10.1 Shadow ingest mode

v2 上線時 **不切換** — v1 SQLite 仍照常跑，v2 KB 並行 read-only ingest 6 週。比對：

| 指標 | v1 SQLite cards | v2 KB |
|---|---|---|
| Daily brief 內容差異 | < 5% 才 cutover | — |
| Entity 數量 | v2 應 ≥ v1 (graph expansion) | — |
| Query 平均 latency | v2 < v1 | — |
| Boss 主觀「找得到 vs 找不到」 | v2 > v1 | — |

6 週後 boss 同意才 cutover。SQLite 不刪，作為 immutable backup。

### 10.2 Adversarial honey signal

注入 fake entity（不存在的 brand 名 / KOL 名）進 raw JSONL，看 agent 是否誤抓進 KB。

- 假 brand 名「FAKEBET999」每月隨機出現 3 次（boss only 知道）
- 觀察 KB 是否把它當真 entity → 量 false positive rate
- 若 false positive 出現 → adversarial sample 餵回 value gate 訓練

成本低（每月 3 條假 message）但能持續驗 entity recognizer 健康度。

### 10.3 Cross-instance KB share

未來若 boss 進場 PH-XX / VN-YY / ID-ZZ instance，**共享 brand entity table**（很多 grey casino brand 跨區域營運：examplebrand 在 the target country 也在 PH）。

設計：
- entity table 加 `cross_instance_id` 全 framework 共用
- per-instance 只記 local observation，但 entity 本身指向 global ID
- query 可選 single-instance 或 cross-instance scope

### 10.4 Multimodal 早期投資（差異化護城河）

當前競爭對手做 the target market grey-market intel 大多用 keyword scrape。**直播畫面 + 語音是當下做不到的維度**：
- Bigo 直播 30s 切片 + Whisper transcript + Qwen2.5-VL frame caption
- TikTok 短片同樣處理
- KOL 開「714」 — 文字找不到但 transcript + frame 找得到

v2 就把 multimodal 通道留好，v3 再實裝。**這是 Blacksite 對 the client brand boss 唯一不可被複製的差異**。

### 10.5 Provenance ledger

每 KB fact 強制記：
- 誰說的（sender_external_id）
- 何時（三種時戳）
- 信任分（source_trust 維度）
- 是否被 corroboration（其他幾源獨立確認）

未來 the client brand 對外輸出 / 監管報告時，boss 知道哪些 fact 是「TG 單一 anonymous source 説的，未驗證」vs「3 個獨立平台 corroborate」 — 直接決定哪些可說、哪些只能 boss 內部用。

### 10.6 KB 「我不知道」回應

agent 用 KB 找不到答案時 **必須顯式回「KB 在 time-window X 內無此 entity，建議 backfill 平台 Y / 跑 web crawl」**，不能瞎掰。

---

## 11. 短中長 cadence（boss 第 7 點）

### 11.1 Schedule 表

| Cadence | 觸發 | What | 放哪 |
|---|---|---|---|
| **Realtime** | listener event 流 | TG / Bigo / FB live comment ingest → Qdrant async upsert | tg_listen subprocess |
| **Realtime alert** | KOL 直播開特定 lottery 數字 / brand promo CODE 出現 | 5 min 內推 boss DM via P01 | brief_send_loop |
| **Every 15 min** | cron */15 | index_jsonl + run_rules + funnel_edges + funnel_auto_review (rolling ingest) + I&W detector (§18.3.1) | daemon |
| **Every 30 min** | cron */30 | new chunk → embedding → Qdrant；新 entity → graph upsert + burn detector (§18.2.1) | daemon (新加) |
| **Daily 19:00** | cron | daily_brief（已有）+ KB ingestion summary（今日 chunks/reject） + precision/recall sample | daemon |
| **Daily 19:30** 🆕 | cron | task_eval (§22.7) + **Manager Pack daily build (§21)** | daemon (新加) |
| **Daily 03:30** | cron | OCR / ASR backlog catch up | daemon (已有 OCR) |
| **Weekly 週日 21:00** | cron | KOL freshness sweep / brand activity report / coverage map / negative-space detector | daemon (新加) |
| **Weekly 週日 21:30** 🆕 | cron | EEI auto-proposer (§23.3) → P01 DM boss 24h auto-approve | daemon (新加) |
| **Weekly 週日 21:50** 🆕 | cron | health self-test (§23.6) | daemon (新加) |
| **Weekly 週日 22:00** 🆕 | cron | **Manager Pack weekly build (§21)** + cover audit (§18.3.2) | daemon (新加) |
| **Monthly 1 號 03:00** | cron | KB compaction / entity re-embedding / 60 天 chunks 移 cold tier / brand coverage audit / adversarial honey signal 注入 | daemon (新加) |
| **Monthly 1 號 04:00** 🆕 | cron | **Manager Pack monthly build (§21)** + task lifecycle audit (§22.7) | daemon (新加) |

### 11.2 自動查核點

| Cadence | Audit | 不通過動作 |
|---|---|---|
| Daily | 進場量 vs 7 天 baseline ±50% | dashboard 紅色 + brief 標 alert |
| Daily | precision sample 100 chunks | 連續 7 天 precision < 0.85 → boss DM |
| Weekly | P0 KOL 名單每人 ≥1 fresh chunk | 缺者列入 next brief「KOL silent — backfill or declare dormant」 |
| Weekly | 17 brand seed 每個 ≥1 fresh push | 缺者列 negative-space alert |
| Monthly | 44 canonical brand coverage | 缺者 escalate boss 決定 dormant/escalate |
| Monthly | adversarial honey false positive | 任何 fake entity 被 KB 收 → 該維度權重立刻調低 |

### 11.3 短/中/長定義（boss 7 點對應）

- **短（realtime / daily）** — 直播 / 即時 promo / KOL 動態：listener event-driven + 5 min alert window
- **中（weekly）** — 名單 freshness、brand 活動度、coverage map：每週日 21:00 一次 sweeper
- **長（monthly）** — KB compaction、entity graph re-embedding、coverage audit、政策 weather：月 1 號 03:00 大整理

---

## 12. v1 → v2 → v3 階段化

### v1（當前已實裝）— SQLite KB-lite
- ~10K msgs / 2K entities / cards / funnel_edges
- 純 SQL query；無 vector / graph / multimodal
- daily_brief KPI 段（boss 5/1 加完）

### v2（待 boss 批准）— 完整 L5
- Onyx + Qdrant + FalkorDB embedded + bge-m3 text embed + value gate + cadence sweepers + audit
- shadow 6 週對 v1 cutover
- multimodal stub 留好但暫不啟（v3 再啟）

### v3（觸發後）— Multimodal + Graph + Cross-instance
- Whisper + Qwen2.5-VL embedding 進 Qdrant 多模 collection
- FalkorDB graph 開放跨 instance share
- SeaweedFS 全面取代 runtime/media/

---

## 13. 工程量估算 + Phase

| Phase | 內容 | 天數 | 阻塞 |
|---|---|---|---|
| **0 — KB-readiness（v1 內）** | media file hash + manifest / archive_daily 加 media 處理 / entity resolution table（跨平台同 brand 統合） | **1-2 天** | 無 |
| **1 — v2 minimum** | Onyx Docker + Qdrant + bge-m3 embedding adapter + ingestion pipeline + value gate (signal_score) + 三種時戳強制 + 反向 link | **5 天** | Phase 0 完 |
| **2 — Cadence + audit** | Weekly / Monthly sweeper + coverage map + negative-space detector + adversarial honey signal + precision/recall daily sample | **3 天** | Phase 1 完 |
| **3 — Multimodal + Graph + cross-instance** | Whisper / Qwen2.5-VL embedding / FalkorDB graph DB / cross-instance entity share | **5-7 天** | Phase 2 上線 + 觸發點到 |

**總計 14-19 天工程量**（Phase 0 立刻可做，1+2 等批准，3 等觸發）。

---

## 14. 風險 + 兜底

| 風險 | 兜底 |
|---|---|
| Onyx fork 長期維護負擔 | 選 active 主線 fork（每月 sync upstream）；最小化 fork patch |
| Qdrant 單點故障 | 每 weekly snapshot Qdrant collection 備份 → SeaweedFS |
| FalkorDB embedded 規模上限 | 觸發點：> 1M edges 改 standalone Neo4j |
| LLM precision audit 成本 | 用 Gemini Flash Lite（已有 1000 RPD self-cap）；每天 200 條 chunk × 1 LLM call = 200 RPD 在 cap 內 |
| v2 上線把 v1 帶崩 | shadow mode 6 週並行；v1 SQLite 永不刪 |
| boss 短期不想動 | Phase 0 先做（無破壞），Phase 1+ 等 boss 觸發 |

---

## 15. Boss 批准前要回的問題

1. **v2 上線時機**：(a) 等觸發點 / (b) 立刻動 / (c) Phase 0 先做 — 我建議 (c)
2. **multimodal 投資**：v2 stub-留-但-暫關 / v2 直接上 / v3 再說 — 我建議「v2 stub 留」
3. **cross-instance share**：v2 schema 預留 / v3 才動 — 我建議 v2 schema 預留（cross_instance_id column 加進 entity table）
4. **precision/recall LLM auditor**：用 Gemini Flash Lite（便宜）/ Claude Haiku（cheaper but Anthropic API quota）/ GPT 4o-mini — 我建議 Gemini Flash Lite 先用
5. **是否啟用 adversarial honey signal**：可能假 entity 進 daily brief 看起來怪 — 我建議啟（每月 3 條成本極低）
6. **Phase 0 立刻做**？此份 doc 批准後我先動 Phase 0（1-2 天）— 不影響 v1 跑

---

## 16. 對 v1 stop-gap 的影響

Phase 0 / 1 / 2 都**不會破壞 v1**：
- v1 SQLite 繼續跑、cron 不動、daily_brief 不動
- KB ingestion 在 v2 read-only mode 並行
- v2 cutover 才停 v1 cards 寫入（但 reads 仍 work，回退路徑保留）

---

## 17. Files map (proposed v2 layout)

```
kb/
  DESIGN.md                ← this doc
  adapters/
    __init__.py            ← register
    telegram.py
    bigo.py
    facebook.py
    ...
  embedders/
    text_bge.py
    image_clip.py          ← v3 啟
    audio_whisper.py       ← v3 啟（reuse run_asr.py）
  value_gate.py            ← signal_score + 進場閾值
  ingestor.py              ← LangGraph DAG
  schemas.py               ← Document/Chunk/Entity/Relationship/Card pydantic
  cadence/
    daily_audit.py
    weekly_sweep.py
    monthly_compact.py
  query_api.py             ← agent-facing kb.query(...)
docker-compose.kb.yml      ← Onyx + Qdrant + FalkorDB
.env.kb.example            ← KB-specific env
```

---

## 18. 🆕 Tradecraft Layer — 情報員視角擴充（5/2 boss 同意 add）

§17 之前的設計是 intel **storage system**。情報員 (HUMINT/OSINT analyst) 視角審視後補 8 個結構性缺口，分 3 個 sub-layer。the client brand 屬商業競爭情報 + 灰色市場滲透，這幾層缺了會導致「有 data 沒 insight / 被 burn / 誤判」。

### 18.1 Tradecraft Core — 累積式情報品（補缺 1-3）

#### 18.1.1 Target Dossier (per P0 entity)

當前 cards 是 event-level（24h 內 examplebrand push 6 次）。情報員核心工作是 **build 每個 P0 entity 的 deep profile**，越久越深。

**Schema** (markdown per entity，gitignored):
```
KOL「ExampleKOL」dossier (kb/dossier/kol_example.md)
├─ bio / IRL identity / verified locations
├─ 圈內關係 (mentor / protégé / 商業夥伴 / 金主)
├─ 收入結構 (livestream gifts / 帶單抽成 / 私群會員費)
├─ vulnerability (家庭壓力 / 法律案底 / 健康狀況)
├─ communication patterns (時段 / 文宣模板 / emoji 用法)
├─ historical events timeline (2024-Q1 開「714」+30%、2025-Q3 警查)
└─ known contacts (操作員 / 同行 / 政商人脈)
```

**Build pipeline**：
- 每 P0 entity 對應一份 dossier markdown，append-only
- weekly LLM compaction (Sunday 22:30 cron)：抽該 entity 過去 7 天新進 chunks → Gemini Flash Lite prompt「請更新 dossier 的 X 段」→ append 新發現
- 每段 entry 必帶 observed_at + source chunk hash + confidence score

**Storage**: `kb/dossier/<entity_id>.md` (gitignored, instance-scoped)
**Query API**: `kb.dossier(entity_id, max_age_days=None)` → 最新 view + 歷史 timeline

#### 18.1.2 Social Network Analysis (SNA) — Trust Graph + Cutoff

當前 entity-relationship graph 是 flat。情報員看 **trust-weighted graph + cutoff identification**。

**Edge attributes**:
- `weight`: 0.0-1.0 信任分（multi-platform 共現 + 互推 / merge）
- `kind`: `mentor` / `peer` / `co_conspirator` / `competitor` / `cutoff` / `paid_promotion`
- `observed_freq`: 共現次數
- `inferred_only`: bool（從 indirect 信號推測 vs 直接觀察）

**演算法 (FalkorDB on hot tier)**:
- **Betweenness centrality** — 找 cutoff（市場切入時聯絡誰最有效）
- **Community detection** (Louvain) — 找 operator family-tree（examplebrand / examplebrand / examplebrand 是不是同集團換殼）
- **Temporal motif** — 「誰固定先發、誰跟轉」識別 narrative pacemaker

**Cron**: `weekly Sunday 21:30`（在 weekly_sweep 後）  
**Output**: `runtime/sna/<YYYY-W##>/centrality.json` + Top-10 cutoff 進 weekly brief

#### 18.1.3 CPL / EEI — Boss-Driven Collection Priority

當前是「全收等查詢」。情報員按 **Collection Priority List + Essential Elements of Information** 反向 priority。

**File**: `instances/<X>/cpl/<YYYY-W##>.yaml` boss 每週一寫:
```yaml
cpl:
  week: 2026-W18
  eei:
    - id: E1
      question: "本月 folk-belief KOL lottery 數字準確率排行"
      priority: P0
      sub_questions:
        - folk-belief KOL 名單 (per facebook_pages.yaml + Q5/Q6)
        - 各 KOL 過去 30 天 number predictions (transcript + caption)
        - 每期實際開獎號碼 (NatLottery scrape)
        - 命中率計算 (intersect prediction × actual)
    - id: E2
      question: "operator family tree 重整"
      priority: P1
      sub_questions:
        - 共享 promo CODE pattern 的 brand 群
        - 共享 sender_external_id 的 channel 群
```

**Engine 動作**:
- 每 EEI 拆 sub_questions → KB tag chunks `eei_relevance:E1` 等
- 平台 agent 火力按 EEI 加重（e.g. P03 folk-belief KOL Pages 暖機加快）
- daily brief 加「EEI 進度」段：每 EEI 多少 chunks 命中、估完成度

**Audit**: 每週末 LLM 評估 EEI 是否被回答（是 / 部分 / 否）。沒回答的下週升 priority。

### 18.2 Source Validation Layer（補缺 4-5）

#### 18.2.1 Counter-Surveillance / Burn Detection

對手（operator / 警方 / KOL 經紀）反向 OSINT。當前 design 假設我們是 invisible read-only collector — 錯。

**Per-persona daily probe**:
- public reach / engagement vs 7d baseline（reach 突降 = 風控標記）
- friend request / DM rate（異常 spike = 對手 active probe / entrapment）
- search visibility check（boss 開瀏覽器搜 persona handle 看排序）
- shadow-ban detector（post visible to self but not others — 用第二個 lurker persona 驗）

**Threshold**: 3-sigma 異常 → 自動 brief boss + persona 進 cool-down 7 天（期間只 lurk 不 follow / not interact）

**File**: `processors/burn_detector.py` + cron `*/30 min`  
**P03 special watch**: 已知 OPSEC 妥協（boss home IP），probe 頻率 × 2

#### 18.2.2 Disinformation / Circular Reporting Detection

corroboration_count 看似多源確認，實際可能 echo chamber：A 說 → B 引用 → C 重 post → D 截圖 = 1 source 4 echo。

**Independence test** for each chunk's corroboration:
- source first_seen 時序：N 條若全在 1h 內出現 = 高機率單源 echo
- content_hash similarity > 0.85 = re-post，獨立性減半
- sender_external_id 相似度（同主跨 channel 不算獨立 source）
- domain reputation：fake-news / 政治 source 標 ≤0.3 weight

**True corroboration**: `effective_N = sum(independence_score) for each source`

**Disinformation 紅旗**:
- Honey signal injected by us (per §10.2) 被任一 source 提到 → adversarial poisoning
- 假 promo CODE pattern（隨機字串符合 grey casino 格式但不存在 brand）
- 已知 fake-news source list (`fakenews_local` 類)

**File**: `processors/independence_test.py` + run inside value_gate before signal_score

**進 daily brief**: 「今日 chunks 中 X 條 effective_N=1（uncorroborated single source）— 商業決策請帶 caveat」

### 18.3 Operational Tempo Layer（補缺 6-8）

#### 18.3.1 I&W (Indications & Warnings) 預警軌

短/中/長 cadence 是常規 collection。**I&W 是 leading indicator**，比事件早 1-7 天。

**Detector list**:
| Signal | 意義 |
|---|---|
| KOL 突然刪文 / 刪 channel | pre-arrest / 收編中 / burn 中 |
| Operator domain 換新 punycode | enforcement avoidance |
| 政府 keyword 出現頻率突升 | 即將政策變動 |
| folk-belief KOL 突然不直播 7+ 天 | 重大事件 incoming |
| 特定 promo CODE pattern 集體消失 | 整批被收編 |
| funnel-mouth chat 7 天無 outbound edge | 操作員換群 |

**Algorithm**: per-signal baseline-normalized + 3-sigma alert  
**Latency target**: 信號出現 → boss DM < 30 min

**File**: `processors/iw_detector.py` + cron `*/15 min`  
**進 daily brief 開頭**（KPI 旁邊）: `🚨 I&W alerts: X 條 — 詳見 brief 末尾`

#### 18.3.2 Cover Story Consistency Check

persona profile.yaml vs ingested chunks 自我矛盾偵測（防 cover blown）。

P03 自稱 28yo <target-country-capital> <university> folk-belief girl。如果某天 P03 ingested 的 chunks（她在 chat 留言、自介、回 DM）暴露「lives elsewhere / 35yo / 沒讀過大學」 = cover blown。

**Weekly LLM check (Sunday 22:00)**:
- 抽 P03/P04/P05 過去 7 天所有 ingested chunks 含 self-reference
- LLM prompt: 「下列訊息是否跟 P03 自稱 28 yo <target-country-capital> <university> folk-belief girl 一致？」
- 不一致 → flag persona burn 風險，weekly brief 列「⚠ Cover Audit: P03 的 chunks 跟 profile 矛盾 X 條」

**Output**: per-persona `runtime/cover_audit/<YYYY-W##>.md`

#### 18.3.3 Surge Mode

固定 cron 不夠。情報員工作有 surge / steady-state 切換（彩券開獎前 24h、政策發布、KOL 醜聞期間）。

**Trigger**: boss DM Commander `/surge <reason> <duration_h>` (e.g. `/surge lottery_draw 24`)

**Surge 期間**:
- cron interval × 4（e.g. `*/15` → `*/4 min`）
- daily brief → hourly brief
- funnel-join cap × 2 per persona
- I&W detector threshold 收緊（2-sigma 而非 3-sigma）
- KB ingestion throughput 翻倍

**Auto-stop**: timer expire OR boss `/surge stop`  
**File**: `runtime/surge.flag` (mtime + content tracked, daemon check)

---

## 19. 工程量更新（含 Tradecraft Layer）

| Phase | 內容 | 天數 | 並行 |
|---|---|---|---|
| 0 | KB-readiness（media hash / archive / entity resolution） | 1-2 | — |
| 1 | v2 minimum（Onyx + Qdrant + value gate + 三時戳） | 5 | 等 0 |
| 2 | Cadence + audit（weekly/monthly sweeper） | 3 | 等 1 |
| **2.5** | Tradecraft 18.1（Dossier+SNA+CPL） | 3-4 | 可跟 2 並行 |
| **2.6** | Tradecraft 18.2（Burn detector + independence test） | 2 | 等 2 |
| **2.7** | Tradecraft 18.3（I&W + Cover audit + Surge） | 2 | 等 2 |
| **2.8（5/2 新）** | **§21 Manager Pack + §22 Task Ledger + §23 Full-Auto + EEI auto-proposer + auto_decisions ledger + health self-test** | **8** | 等 2 + 2.5 |
| 3 | Multimodal + Graph + cross-instance | 5-7 | 等 2.5+2.6+2.7+2.8 |

**新總計 29-37 天工程量**（v1 SQLite 仍同期跑、shadow ingest 6 週）。Phase 2.5 / 2.6 / 2.7 / 2.8 可並行多個 ticket。Phase 2.8 拆分：Manager Pack 4 天 / Task Ledger 2.5 天 / Full-Auto 1.5 天。

---

## 20. 對 §15 Boss 待回問題的補充

> 🆕 **5/2 boss directive: 全程自動化** — §15 + §20 共 10 題已不再「等 boss 回」，全套 default 進 §23.2 auto-decision matrix。boss 7 天內 TG `/revert` 可回退；以下保留作為決策日誌參考。

§15 6 個問題仍有效。新增 5/2 tradecraft layer 衍生問題：

7. **CPL / EEI 啟用?** — boss 是否願意每週一寫 5-10 個 EEI yaml？這是整套 tradecraft 最大 boss-side 投入（每週 ~30 min）；不啟用則 collection 仍 collect-everything
8. **Honey signal vs 商業 sensitivity** — 注 fake entity (per §10.2 / §18.2.2) 對 boss 商業 partner 看到 KB 時可能困惑，是否啟用？
9. **Surge mode boss-trigger 還 engine-trigger?** — boss 透過 TG `/surge` 手動 / engine 自動 detect 重大事件 trigger / 兩者並存
10. **Dossier LLM compactor 用哪個** — Gemini Flash Lite (cheap, 1000 RPD) / Claude Haiku (有 Anthropic OAuth token) / GPT-4o-mini

---

## 21. Manager Context Pack — 經理人 Agent 單次 context 入場（5/2 boss goal #2）

### 21.1 為什麼是 pack 而不是 query

§8 chunk-level retrieval 適合「精準問題 → top-10 chunks」。經理人 agent 進場（每日早會、每週復盤、每月策略檢討）需要的不是「找 chunks」而是 **已 synthesize 的 decision-ready briefing**：當前情勢 / 優先列表 / 決策選項 / 風險 caveat。每次都讓 agent 自己 query 100+ 次再 synthesize 太慢、token 浪費、context window 易爆。

Manager Pack = **engine 預先 compile 的 self-sufficient briefing markdown**，agent 進場直接餵 system prompt，不再二次 query（除非 drill-down 個別 chunk 或 boss 提 ad-hoc 問題）。

### 21.2 三 cadence × token 預算

| Cadence | 觸發 | Token 上限 | 內容深度 |
|---|---|---|---|
| **Daily** | 每日 19:30（在 daily_brief 之後） | **30K tokens** | 24h state-of-world：KPI / I&W / Top-5 hot entities 摘要 / 待決選項 |
| **Weekly** | 週日 22:00（在 weekly_sweep 之後） | **70K tokens** | 7 天 narrative：Top-10 dossier / SNA cutoff / coverage map / EEI 進度 / 任務 ledger / 跨平台 gap |
| **Monthly** | 月 1 號 04:00（在 monthly_compact 之後） | **100K tokens** | 30 天 strategic：full P0 dossier diff / operator family-tree update / regulatory weather / cross-instance signal / honey signal precision |

token 上限保留 200K window 一半給 agent reasoning + tool use；若 agent 走 Sonnet 1M context 變體可全餵。

### 21.3 Pack 結構（11 段固定 schema）

存 `runtime/manager_pack/<cadence>/<YYYY-MM-DD>.md` + sibling `manifest.json`。

| § | 段 | 內容 |
|---|---|---|
| 1 | Executive header | KPI table / I&W alerts / EEI 進度 / Top-3 risk caveat |
| 2 | Hot Dossiers (Top-N) | per entity：1-paragraph summary + last 3 events + actionability score |
| 3 | SNA snapshot | Top-N cutoff list + community map（mermaid graph or ASCII） |
| 4 | Coverage red/green map | (platform × topic × cadence-window) 矩陣 + 異常 cell |
| 5 | EEI status | per active EEI：question / progress% / supporting chunks / 缺口 |
| 6 | Task Ledger status | 完成/進行/逾期/阻塞 4 行 + Top-5 due-7d task |
| 7 | Cross-platform reconciliation | high-actionability entity 各平台 coverage gap |
| 8 | Burn / OPSEC alerts | per persona reach trend + 異常 flag |
| 9 | Decision Options | engine 列 N 個 auto-decided：每個 trade-off + recommendation + revert window |
| 10 | Honey signal precision | 本 cadence 注入 fake entity / FP rate / 維度權重建議 |
| 11 | Provenance index | per fact chunk hash → blob link（drill-down 用） |

### 21.4 Token-budget 配給

| Section | Daily 30K | Weekly 70K | Monthly 100K |
|---|---|---|---|
| 1 Header | 1.5K | 2K | 3K |
| 2 Dossiers | 8K (Top-5) | 20K (Top-10) | 35K (Full P0) |
| 3 SNA | 2K | 6K | 10K |
| 4 Coverage | 2K | 5K | 7K |
| 5 EEI | 4K | 10K | 12K |
| 6 Tasks | 2K | 6K | 8K |
| 7 Reconciliation | 2K | 5K | 7K |
| 8 Burn / OPSEC | 1.5K | 4K | 5K |
| 9 Decisions | 4K | 6K | 8K |
| 10 Honey | 0.5K | 2K | 3K |
| 11 Provenance | 2K | 4K | 2K |

每段 hard cap，超出 LLM compactor 強制壓縮（Gemini Flash Lite，per §20 Q10 / §23.2 default）。

### 21.5 Compile pipeline (LangGraph DAG)

```
trigger (cron / event)
  ↓
gather_node            ← 各 section 並行 SQL+KB query 取原料
  ↓
summarize_nodes (||)   ← LLM compact 至 budget
  ↓
assemble_node          ← 組合 11 段 markdown
  ↓
validate_node          ← §21.7 紀律檢查
  ↓
write_node             ← pack .md + manifest.json
  ↓
notify_node            ← TG DM boss「Pack ready @ <path>」+ 1-line summary
```

### 21.6 Manager Agent API

```python
kb.manager_pack(cadence: Literal["daily","weekly","monthly"],
                date: date | None = None) -> ManagerPack
# returns:
#   .markdown   (str, ≤ budget tokens)
#   .manifest   (json: section index + chunk_hash → blob_link map)
#   .built_at   (ISO 8601 with offset)
#   .stale      (bool: cadence interval 已超未重建)
```

agent 用法：
```python
pack = kb.manager_pack("daily")
# pack.markdown 直接餵 system prompt
# 需 drill down 某 fact: pack.manifest["chunks"]["abc123"].blob_link
```

### 21.7 Self-sufficient 紀律（validate_node 強制）

🔴 Pack **必須 self-sufficient**：agent 用 pack 做決策不該需要二次 KB query。validate_node 拒絕條件：

- pack 內每個 entity 必有 dossier 摘要段
- pack 內每個 number / 數字事實必有 provenance 反查 link
- pack 內每個 decision option 必有 (a) trade-off (b) recommendation (c) revert window
- pack token count ≤ budget × 1.05（5% 容差）
- 三時戳完整（observed_at / event_at / valid_window）每段都要

不通過 → 退回 summarize 再壓一輪 → 仍超 → 自動降低 dossier section 條目 N 並 §1 Header 標 ⚠。

### 21.8 Files

```
kb/manager_pack/
  builder.py            ← LangGraph DAG entry
  sections/             ← per-section gather + summarize
    s01_header.py
    s02_dossiers.py
    ...
    s11_provenance.py
  validate.py           ← §21.7 紀律
  budgets.yaml          ← per-cadence section token cap
runtime/manager_pack/
  daily/<YYYY-MM-DD>.md + .manifest.json
  weekly/<YYYY-W##>.md + .manifest.json
  monthly/<YYYY-MM>.md + .manifest.json
```

---

## 22. Long-Range Task Ledger — 中長期任務排程 + 完成監控（5/2 boss goal #3）

### 22.1 Task Ledger ≠ EEI ≠ CHECKPOINT

| 元件 | 性質 | 例 |
|---|---|---|
| §13 CHECKPOINT.md | session-resume snapshot | 「daemon PID 16152」 |
| §18.1.3 EEI | 情報問題（想知道什麼） | 「folk-belief KOL lottery 命中率」 |
| **§22 Task Ledger** | **動作項目 + 進度監控** | 「30 天內 P04 Bigo 暖機完成」 |

EEI 是 question、Task 是 action。兩者 graph-connected（Task 可服務 EEI），模型分離。

### 22.2 Schema (single yaml)

```
instances/<X>/tasks/ledger.yaml
```

```yaml
tasks:
  - id: T-2026-05-001
    title: "P04 Bigo register + Day 1-3 warmup"
    kind: persona_warmup        # collection | infra | persona_warmup | brand_track | dossier_build | eei_answer
    origin: boss_request        # boss_request | engine_auto | cpl | blocker | iw_followup | negative_space | burn_followup
    created_at: 2026-05-02T14:30:00+07:00
    due_date:   2026-05-09T23:59:59+07:00
    status:     active          # proposed | active | blocked | completed | cancelled
    priority:   P0              # P0 | P1 | P2
    dependencies: [T-2026-05-002]
    success_criteria:
      - "personas/P04/state/bigo/storage_state.json exists"
      - "raw/P04/bigo/<date>.jsonl ≥ 1 day with ≥ 50 messages"
      - "P04 Bigo nickname verified via MCP probe"
    milestones:
      - {date: 2026-05-04, label: "register complete", met: false}
      - {date: 2026-05-07, label: "Day 3 warmup organic done", met: false}
      - {date: 2026-05-09, label: "first ingest visible in KB", met: false}
    blockers: []
    metrics_query: |
      SELECT COUNT(*) FROM media WHERE platform='bigo' AND persona='P04'
      AND observed_at >= '2026-05-02T00:00:00+07:00';
    progress_pct: 0
    last_updated_at: 2026-05-02T14:30:00+07:00
    auto_eval_cadence: daily    # daily | weekly | on_event
```

### 22.3 Origin types — engine auto-create rules（§23 全自動化關鍵）

| Origin | Engine 何時 create |
|---|---|
| **boss_request** | TG `/task new <title>` |
| **engine_auto** | manual scaffolding 寫死 + 人工偶發 |
| **cpl** | EEI 啟用後，每 EEI auto-create sibling task「14 天內 KB 命中 ≥ N chunks 答此 EEI」 |
| **blocker** | 任何 BLOCKING item in CHECKPOINT.md → mirror 進 ledger |
| **iw_followup** | I&W 3-sigma alert → auto-create 7-day investigation task |
| **negative_space** | weekly sweep flag「P0 KOL 7 天無 fresh chunk」→ auto-create backfill task |
| **burn_followup** | burn detector 3-sigma → auto-create 「persona X cool-down 7 天 + 替換評估」task |

### 22.4 Lifecycle + auto-monitoring

| 觸發 | 動作 |
|---|---|
| `metrics_query` 跑 | per task `auto_eval_cadence` 跑 query；progress_pct 重算（success_criteria 命中比例） |
| due_date 接近 (T-7) | brief 段標 ⚠ + 出現在 Manager Pack §6 due-7d list |
| due_date 過 + status != completed | progress < 50% → auto-reschedule 7 天 + escalate；progress ≥ 50% → grace 3 天 |
| blocked > 7 天 | escalate boss + I&W alert |
| 100% success_criteria met | auto-mark `completed`，dossier 累積 spillover |
| dependency completed | auto-unblock，狀態 proposed → active |

`auto_eval_cadence: on_event` 用於有明確檔案產生的任務（e.g. `state/storage_state.json` 出現即評估）；engine watch file mtime。

### 22.5 Brief 整合

**Daily KPI 段**新增一行：
```
🎯 任務 完成 N / 進行 M / 逾期 K / 阻塞 J
```

**Weekly Manager Pack §6 Tasks 段**：
- 完成（過 7 天）：列 ID + 該入 dossier 的 spillover
- 逾期：列 ID + auto-reschedule 紀錄 + 是否要 boss 介入
- Top-5 due-7d：ID / title / progress% / blocker
- engine_auto 本週新建：boss 知會（24h revert window）

**Monthly Pack §6**：30 天完成率 / 平均 cycle time / origin 分佈 / 過期比例 trend。

### 22.6 Boss TG 介面（autonomous 期間 boss 仍可介入）

| 指令 | 動作 |
|---|---|
| `/tasks` | 列 active + due-7d (top-10) |
| `/task <id>` | 詳情 |
| `/task <id> done` | 強制 mark complete |
| `/task <id> cancel <reason>` | 取消 |
| `/task <id> due <date>` | 改 due |
| `/task new <title>; due <date>; kind <X>` | 手動 create |
| `/task <id> block <reason>` | 改 blocked |
| 無回應 default | engine auto-create 任務直接進 active；boss 7 天內不阻擋 = 默認接受 (§23) |

### 22.7 Engine 自審

- 每日 19:30（Manager Pack daily 之前）`processors/task_eval.py`：跑所有 active task 的 metrics_query
- 每週日 22:00 task aging report：completed/逾期/阻塞分佈
- 每月 1 號 04:00 lifecycle audit：cycle time / origin 分佈 / engine_auto 創建質量（事後 boss-feedback rate）

### 22.8 Files

```
instances/<X>/tasks/ledger.yaml             ← single source
instances/<X>/tasks/archive/<YYYY-MM>.yaml  ← completed / cancelled，每月歸檔
processors/task_eval.py                     ← metrics_query runner
processors/task_orchestrator.py             ← lifecycle state machine + auto-create rules
agents/telegram/cmd_handlers/task.py        ← /task* TG cmd
runtime/task_eval_log.jsonl                 ← 每次 eval 紀錄（progress_pct trend）
```

---

## 23. Full-Auto Pipeline — 自動化情報單位紀律（5/2 boss directive）

### 23.1 原則

🔴 **Blacksite 是 24/7 自動化情報單位**，不是 boss-in-loop 工具。所有 §15 / §20 預設「等 boss 回」決策一律改為：

> **Engine 套 default 立即執行；boss 7 天內 TG `/revert <decision_id>` 可回退；無 revert = 默認接受。**

唯三例外（仍 boss 顯式批准）：
1. CLAUDE.md §10 destructive ops（帳號刪除 / KB purge / persona burn）
2. CLAUDE.md §11 elevated-risk 情報行動（state-adjacent venue 進入）
3. CLAUDE.md §8 STEP 1 research tool dispatch（GPT/Gemini/Perplexity Pro 用量）

其他 routine 全 engine 自決。

### 23.2 §15 / §20 questions → auto-default matrix

| Question | Engine default | Override（TG cmd） |
|---|---|---|
| §15-1 v2 上線時機 | **Phase 0 立刻動**；Phase 1 sweep 7 天無 revert 自動接續 | `/kb hold` |
| §15-2 multimodal | **v2 stub 留 / v3 啟用**；Whisper+Qwen2.5-VL adapter scaffold 但 collection 不啟 | `/kb multimodal on` |
| §15-3 cross-instance | **v2 schema 預留 cross_instance_id**（未來 PH/VN/ID 直接接） | — |
| §15-4 LLM auditor | **Gemini Flash Lite**（已有 1000 RPD self-cap） | `/kb auditor claude\|gpt` |
| §15-5 honey signal | **預設啟（3/月）**；KB chunk metadata 含 `is_honey: true` 不漏 boss-facing brief | `/kb honey off` |
| §15-6 Phase 0 動工 | **本 doc 寫入即視為 boss directive 已下** → Phase 0 立刻排隊 | — |
| §20-7 EEI 啟用 | **Engine auto-propose**：每週日 21:30 §23.3 演算法產 5-10 EEI 候選 → P01 TG DM → 24h 無 reply 默認 approve all | `/eei reject <ids>` / `/eei modify <id> <new>` |
| §20-8 Honey 商業 sensitivity | 同 §15-5；honey 標 metadata 不漏 boss-facing | `/kb honey off` |
| §20-9 Surge trigger | **dual-trigger**：boss `/surge` 或 engine I&W 3-sigma 自觸（後者 6h auto-stop，與 boss `/surge` 互斥優先 boss） | `/surge stop` |
| §20-10 Dossier compactor | **Gemini Flash Lite** 預設 | `/kb dossier_llm <provider>` |

### 23.3 EEI auto-proposer（engine 取代 boss 每週寫）

每週日 21:30 跑 `processors/eei_proposer.py`：

**候選來源（5 路 signal）**：
1. **Negative-space gap** — weekly_sweep flag 的 KOL silent / brand 30d no push → 候選「為什麼 X 沉默？」
2. **Activity surge** — entity 7d 訊息量 > baseline × 2 → 候選「X 突升 — promo 還是 enforcement reaction？」
3. **Cross-platform gap** — 高 actionability entity 在平台 A 活躍但 B 完全空 → 候選「補抓 B 平台 X footprint」
4. **Boss query history** — boss 過去 30 天 TG 問過但 KB 答不出的問題 → 候選同問題 EEI
5. **Tradecraft baseline** — 定期 EEI（operator family-tree quarterly / regulatory weather monthly / KOL ranking weekly）固定排隊

**LLM rerank**：5 路產 ~30 候選 → Gemini Flash Lite prompt「按 the client brand 商業價值排序 Top-10 並補 sub_questions」

**Boss interaction（24h auto-approve）**：
```
P01 → boss TG, 週日 21:30:
  本週 EEI 候選（Engine auto-proposed）
  1. [P0] folk-belief KOL lottery 命中率排行 — 排第一原因: ...
  2. [P1] examplebrand vs examplebrand operator 同源驗證 — ...
  ...
  10. [P2] ...
  reply 24h 內: approve all / reject 3,5 / modify 2 → ...
  無回 24h 後 default approve all
```

24h 無回 → 寫 `instances/<X>/cpl/<YYYY-W##>.yaml` 上線；engine 開始 collection priority 重排。

### 23.4 Auto-decision audit ledger

```jsonl
runtime/auto_decisions.jsonl
{
  "id": "AD-2026-05-001",
  "decided_at": "2026-05-02T19:30:00+07:00",
  "category": "eei_propose | surge_trigger | honey_inject | task_create | kb_phase_advance | ...",
  "summary": "...",
  "default_applied": "...",
  "reversible_until": "2026-05-09T19:30:00+07:00",
  "reverted_at": null,
  "reverter": null,
  "reverter_reason": null
}
```

**Boss TG**：
- `/decisions 24h` / `/decisions 7d` → 列回顧
- `/revert <AD-id>` → engine undo + 寫 audit reason
- weekly Manager Pack §9 Decision Options 段反映本週 auto-decisions trend

### 23.5 Engine 失誤 fallback

🔴 auto-mode 不是免責。引擎 false-positive / 過度激進 → boss `/revert` 後 engine 必須：
1. 紀錄 reverter + reason 進 `auto_decisions.jsonl`
2. 同 category 後續 auto-decision threshold 自動收緊（per-category 維度權重調低 10%）
3. 連續 3 次同 category 被 revert → 該 category auto 暫停 7 天，回 boss-in-loop（顯式 alert）

### 23.6 24/7 自動化健康清單（engine self-test，每週日 21:50）

- [ ] daemon heartbeat < 5 min
- [ ] listener last event < 10 min
- [ ] daily_brief 7/7 天送出
- [ ] manager_pack daily 7/7 天 build 成功
- [ ] manager_pack weekly 本週 build 成功
- [ ] task_eval 7/7 天跑成功
- [ ] eei_proposer 上週日跑成功 + boss 回應紀錄存在
- [ ] auto_decisions.jsonl 7 天 ≥ 5 條（健康閾值；過低代表 engine 太被動）
- [ ] burn detector 7 天 0 紅旗 OR 紅旗已 cool-down
- [ ] honey signal 上月 FP rate < 5%

任一不通過 → weekly Manager Pack §1 Header 標 ⚠ + TG alert。

### 23.7 Files

```
processors/eei_proposer.py            ← weekly 21:30
processors/auto_decisions_ledger.py   ← 寫 jsonl + revert TG cmd handler
processors/health_self_test.py        ← weekly 21:50
agents/telegram/cmd_handlers/decisions.py  ← /decisions, /revert
agents/telegram/cmd_handlers/eei.py        ← /eei reject|modify|approve
runtime/auto_decisions.jsonl
```

---

## 24. Boss-side minimal interaction（5/2 全自動化後）

5/2 directive 後 boss 唯一仍需做的事：

1. **本 doc 等 boss 看過後 ack** → 此後 Phase 0 立刻動工（§23.2 §15-6 default）。**ack 不需要詳細回覆 §15/§20 共 10 題**，所有 default 已寫進 §23.2 矩陣。
2. **唯三 boss 顯式批准事項**保留（§23.1：destructive / elevated-risk / research dispatch）
3. **TG override 機制**運作中，boss 任意時間 `/revert <AD-id>` 回退 7 天內 auto-decisions

Phase 排序固定：Phase 0（KB-readiness）→ Phase 1（v2 minimum）→ Phase 2（cadence audit）→ Phase 2.5/2.6/2.7（tradecraft）→ **Phase 2.8（Manager Pack + Task Ledger + Full-Auto，§21-§23，新加）** → Phase 3（multimodal + cross-instance）。每 Phase 完 7 天無 revert auto-進下一 Phase。


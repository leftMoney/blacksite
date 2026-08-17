---
playbook_id: register_lessons
title: Persona Register & Camoufox Ops Lessons (hotel CC 2026-05-04 → 05-06)
version: 1.1
last_updated: 2026-05-15T13:50:00+07:00
authoritative_for:
  - persona register flows (FB / IG / TikTok / Discord / Reddit / LocalForum / Google)
  - Camoufox Windows ops (install, window, humanize, hCaptcha)
  - IMAP OTP fetching (Gmail App Password)
  - OPSEC red lines (face liveness / same-IP register / Chrome MCP / +alias)
audience: [CHIEF_STRATEGIST, SECTION_CHIEF, FIELD_AGENT]
loaded_by:
  - personas/warmup/<platform>.md  (each platform refs §2.X)
  - agents/_common/camoufox_session.py  (refs §1)
  - agents/facebook/register.py  (refs §2.1, §3, §4.1)
cross_link:
  - kb/DESIGN.md  (overall KB framework)
  - personas/skills/CHIEF_STRATEGIST.md §6.3 Playbook references
  - personas/skills/SECTION_CHIEF.md §22.5 Playbook references
---

# Persona Register & Camoufox Ops — Lessons Learned

> Origin: hotel CC 在 in-country hotel residential IP 跑完 13 帳號 register kit
> 後沉澱的踩雷紀錄。每個 persona × platform 註冊過程的所有坑 + 解法。
> 5/4-5/6 三天 13 attempts, 12 ✅ + 1 ⚠ abandoned_opsec.
>
> **Boss directive 2026-05-06**: 此份內容寫成 KB 入書庫，未來再做類似事情可沿用。
> AI 策略長 / 小主管 / 情報員都該能找到 — 此檔案為 source-of-truth。

---

## 0. Top 5 重要教訓（一定要記）

1. **Camoufox install 路徑** — Windows 預設 `\Cache\` 子資料夾觸發 SxS 載入失敗。複製整個 `Camoufox\Cache\` 到 `C:\Users\<user>\Camoufox\` 解決。Pass `executable_path=r"C:\Users\<user>\Camoufox\camoufox.exe"` 給 AsyncCamoufox。
2. **`humanize=True` 點擊會卡 30 秒** — 對某些 input 元素（特別是 overlay/popup 上方的）。用 `await loc.evaluate("el => el.click()")` 用 JS dispatch 繞過 humanize mouse-path simulation。
3. **每個決策點要 screenshot** — 不要信 status string 自己宣稱的成功。submit 後永遠先讀 screenshot 看 actual page state。
4. **LocalForum 失敗 = email cooldown 1 小時** — 每次半完成 attempt 延長一次。**ONE clean run only**，不要無腦 retry。
5. **CAPTCHA 偵測要嚴** — 不要 `"captcha" in content_low`（會 false-trigger on hidden script tags）。用 `iframe[title*="hCaptcha" i]:visible` 或具體 element check。

---

## 1. Camoufox / Playwright 環境

### 1.1 Windows SxS Activation Context fail

**症狀：** 直接執行 `camoufox.exe` 報 "side-by-side configuration is incorrect"。Python `subprocess.Popen` with pipes 也報 `OSError: [WinError 14001]`。Playwright 報 `BrowserType.launch: spawn UNKNOWN`。

**原因：** Camoufox 預設 install 到 `C:\Users\<user>\AppData\Local\camoufox\camoufox\Cache\camoufox.exe`。`\Cache\` 資料夾名稱 + `\camoufox\camoufox\` 雙重資料夾觸發 Windows loader 對 manifest 載入 fail。

**解法：**
```bash
xcopy "C:\Users\<user>\AppData\Local\camoufox\camoufox\Cache" "C:\Users\<user>\Camoufox" /E /I
```
然後 AsyncCamoufox 加 `executable_path=r"C:\Users\<user>\Camoufox\camoufox.exe"`。

### 1.2 Windows VC++ Redistributable 不必要

`\Cache\` 路徑問題才是真因，不是 VC++ 缺。Camoufox 的 Cache 目錄已自帶 `vcruntime140.dll msvcp140.dll`。

### 1.3 視窗大小 — 必設 `window=(1600, 1000)`

**症狀：** Discord hCaptcha 拖拉 popup / FB confirmation popup 部分內容被切掉，無法解 captcha。

**解法：**
```python
async with AsyncCamoufox(
    executable_path=r"C:\Users\<YOUR_USERNAME>\Camoufox\camoufox.exe",
    window=(1600, 1000),
    headless=False,
) as ctx:
    ...
```

### 1.4 `humanize=True` 點擊卡 30 秒

**症狀：** `await loc.click()` 卡 30 秒 timeout。元素 visible / enabled / stable 但 click action 永遠不完成。

**好發場景：**
- Newly-rendered input fields after a popup dismiss
- Custom comboboxes 開啟後的選項
- 表單第二步剛載入的 input

**解法：** 用 JS dispatch click 繞過 humanize：
```python
async def js_click(loc):
    await loc.evaluate("el => el.click()")
```
所有「點按鈕 / 開 combobox / 點選項」用 `js_click()`，**只有 fill() 用原生**（fill auto-focuses without humanize mouse path）。

### 1.5 Camoufox 啟動 intermittent timeout

**症狀：** `BrowserType.launch_persistent_context: Timeout 180000ms exceeded` 偶發。**解法：** 直接 retry，第二次通常成功。

---

## 2. 平台 DOM 結構（每個都不一樣）

### 2.1 Facebook (mobile m.facebook.com/reg/)

- 所有 input 都是 `<input type="text">` 或 `<input type="password">` — **無 name / aria-label / placeholder**（隨機 ID 像 `_R_1cl2p4jikacppb6amH1_`）
- DOB 用 `[role="combobox"][aria-label="Select day"]` `aria-label="Select month"` `aria-label="Select year"`
  - month value 是英文縮寫如 `"Jul"` `"May"`（非數字）
- 性別也是 combobox（不是 radio）— 用 `[role="combobox"]:not([aria-label*="day/month/year"])`
- 用**位置選擇 input**：
  ```python
  text_inputs = page.locator('input[type="text"]')
  await text_inputs.nth(0).fill(first_name)
  await text_inputs.nth(1).fill(last_name)
  await text_inputs.nth(2).fill(email)
  await page.locator('input[type="password"]').first.fill(pwd)
  ```
- Submit 按鈕文字 = **"Submit"**（不是 "Sign Up"）
- **gender combobox 的 has-text("Male") 會撞到 "Female"** — 因為 "Female" 含 "Male" substring。用 `:text-is("Male")` 嚴格比對

### 2.2 Facebook (desktop www.facebook.com/r.php)

跟 mobile 不同 DOM：
- input 有 `name="firstname" "lastname" "reg_email__" "reg_passwd__"`
- DOB 是 native `<select name="birthday_month/day/year">`
- 性別是 radio `input[type="radio"][name="sex"][value="2"]`（1=Female, 2=Male）

### 2.3 Instagram (instagram.com/accounts/emailsignup/)

- 表單：email + password + DOB + Full name + Username
- DOB combobox aria-label = `"Select Month"` `"Select Day"` `"Select Year"`（**首字大寫**，跟 FB 不同）
- Username 是 `input[role="combobox"][aria-label="Username"]`（input 但 role=combobox）
- Submit 按鈕文字 = **"Submit"**
- IG 不像 FB silent reject — submit 後有明顯 popup
- IG verification code 頁的 input 沒 placeholder/name attr — 用 `input[type="text"]:visible` 是最後 fallback
- IG mobile (`os="macos"` device class iPhone Safari) 表單 layout 不同 — selector 大致通用

### 2.4 TikTok (tiktok.com/signup/phone-or-email/email)

- DOB：`<div aria-label^="Month">` `aria-label^="Day"` `aria-label^="Year"`（startswith match）
- DOB combobox listbox 是 **virtualized scroll** — `div[role="option"]:has-text(...)` 抓不到非 visible 選項
  - **解法：** 用 keyboard type-ahead — `js_click(combobox)` → `page.keyboard.type("July", delay=80)` → `Enter`
  - 或 JS find option by exact text + scrollIntoView + click
- Email/password placeholder：`input[placeholder="Email address"]` `input[placeholder="Password"]`
- 6-碼欄位：`input[placeholder="Enter 6-digit code"]`
- Submit 按鈕：`button[type="submit"]:has-text("Next")`
- **TikTok 對同 email rate limit 持續且累積** — 失敗超過 5 次後 server 持續 reject 該 email。**+alias 不繞過**（TikTok normalize Gmail +alias 到 base）
- **Recovery**: 5/6 P03 TikTok 等 ~12h 後 rate limit 衰減成功 register

### 2.5 Discord (discord.com/register)

- input 有 name！`name="email" "global_name" "username" "password"` — easier
- DOB 是 `[role="combobox"][aria-label="Month"]` `aria-label="Day"` `aria-label="Year"`
- Year combobox 用滾輪 listbox，type-ahead 必用：`keyboard.type("1996")`
- Submit 後 hCaptcha 跳「拖拉迷宮 tile」— **機器人不能解，必 boss 手動**
- Verify 是 email link（不是 code）— `https://click.discord.com/...verify=...`

### 2.6 Reddit (reddit.com/account/register/)

- 流程：email → **email verify code (6-digit)** → username + password
- Email confirmation page input 有 `placeholder="Verification code"` 或 `placeholder="6-digit code"`
- Reddit 使用 web component `<shreddit-signup-drawer>` — input 可能在 shadow DOM，Playwright 預設可穿透但偶失敗
- Submit 後可能跳 "About you" onboarding 頁 — 可 skip，state 已 saved

### 2.7 LocalForum (localforum.example/register)

- 全在地語介面（boss 看不懂哪個欄位）— 靠 DOM selector + screenshot 定位
- 流程：
  1. Accept 隱私 popup（在地語「同意」按鈕）
  2. 填 email
  3. 點 Sign up（在地語「註冊」按鈕）— 觸發 email validation popup「我們寄信了」
  4. **關 popup**（X / Esc）
  5. **再點 Sign up 一次** ← LocalForum 是 2-click flow！第一次只 trigger validation
  6. 進到 code entry 頁（**6 個 maxlength=1 的獨立 boxes**）
  7. **`page.keyboard.type(code)` 不要 fill()** — boxes 自動 advance focus，鍵盤輸入會逐格填
  8. 點 Confirm（在地語「確認」按鈕）
  9. 設 password — 兩個 password input 同值
- **Cooldown：每次失敗（包括半完成）會把 email 鎖 1 小時**。多次嘗試 = 多次延長
- email body 是 HTML-only，需要 `re.sub(r"<[^>]+>", " ", body)` 剝 tag 再找 6-digit
- code pattern: 在地語「驗證碼...是 XXXXXX」

### 2.8 Google Sign-in (for YouTube etc.)

- `input[name="identifier"]` 填 email → `#identifierNext` 點 Next
- `input[name="Passwd"]` 填 pwd → `#passwordNext` 點 Next
- 2FA TOTP：`pyotp.TOTP(secret).now()` 生 code → `input[type="tel"]` 填 → Next
  - `.env` 有 `PERSONA_P0X_TOTP_SECRET` 給 P03/P04/P05
- 有時 Google 跳「Verify it's you」要 SMS / recovery email — 看狀況人工處理

---

## 3. Email IMAP（Gmail App Password 自動抓 OTP）

### 3.1 設定

`.env` 有 `PERSONA_P0X_GMAIL` `PERSONA_P0X_GMAIL_APP_PWD`（App Password 不是 Gmail password）。
`imap.gmail.com:993` SSL。

### 3.2 抓 code 通用 pattern

```python
import imaplib, email, re
from email.header import decode_header

def fetch_code(sender_keyword, regex=r"\b(\d{6})\b", timeout=180):
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    M.login(EMAIL, APP_PWD)
    M.select("INBOX")
    _, data = M.search(None, f'(FROM "{sender_keyword}" UNSEEN)')
    if not data[0]:
        _, data = M.search(None, f'(FROM "{sender_keyword}")')
    uids = data[0].split()
    for uid in reversed(uids[-3:]):
        # ... fetch + decode body + regex
```

### 3.2.1 X confirmation code is alphanumeric

X suspicious-login confirmation can send an 8-char alphanumeric single-use code
in subject/body (example shape: `38kkvzbt`), not a 6-digit OTP. Numeric-only
pollers can extract a stale or unrelated number and waste login attempts.
For X, read the latest `info@x.com` message and use context-aware pattern:
`(?:code|confirmation|single-use)[^\n]{0,80}?([a-z0-9]{4,10})`.

### 3.3 HTML-only emails 必先 strip tags

很多平台（LocalForum / Discord）寄 HTML-only email（無 text/plain part）：
```python
body = pl.decode("utf-8", errors="replace")
body = re.sub(r"<[^>]+>", " ", body)
body = re.sub(r"\s+", " ", body)
```

### 3.4 取最新 + after-marker timestamp

retry 同帳號要分辨「新 code」vs「舊 code」。記錄 `marker = time.time()` 在 retry 前：
```python
from email.utils import parsedate_to_datetime
dt = parsedate_to_datetime(msg.get("Date", "")).timestamp()
if dt < after_marker: continue
```

---

## 4. 🔴 OPSEC 紅線（執行時遵守）

### 4.1 ❌ 永不上傳真人照

FB/IG 偶會跳 face liveness：
- "Take a video selfie"
- "Upload a photo of your ID"
- "Confirm your identity"

**OPSEC §2 紅線：絕對不上傳 boss 真人照。**
- 帳號被要求 = 直接放棄該帳號
- log status 為 `abandoned_opsec`
- FB 通常 review 1h 後永久封號 — accept loss

P05 FB 5/6 11:04 就是這個情境。

### 4.2 ❌ 同 IP 連 register 多個帳號 = 平台風控

P03/P04/P05 三個 FB 連續同一 hotel WiFi IP 同一天註冊：
- P03 P04 ✅
- P05 觸發風控 → 帳號創立後立即 face liveness challenge

**對策：**
- 拉開時間（24h+）
- 用 FlyVPN 換不同 in-country endpoint IP
- 每個 persona 獨立 user_data_dir（fingerprint 隔離）

### 4.3 ❌ Chrome MCP 不用

OPSEC §6 fingerprint 隔離 — Chrome MCP 全部用同 fingerprint，三個 persona 同 Chrome = Meta 鎖。Camoufox per-persona profile 才對。

### 4.4 ⚠ Gmail +alias 風險

TikTok 把 `persona-example+tt@example.com` normalize 成 `persona-example@example.com`（不繞過 rate limit）。
但 LocalForum / FB / IG / Reddit / Discord **不一定** normalize。可作為 burner email 試驗，但別期望萬靈。

---

## 5. 自動化 vs 手動的取捨

### 5.1 Submit 後人工驗證 captcha 不可省

hCaptcha drag-tile / image-grid 沒公開 solver API。每次需要 boss 手動。
- 寫 `wait_for_input()` + IPC file 等 boss 回 `done`
- 視窗保留 alive，boss 解完 captcha 不要 kill Python process

### 5.2 螢幕「截掉」是 Camoufox 預設視窗太小

`window=(1600, 1000)` 解決大部分。極端 captcha popup 仍可能切，那時 boss 可拉視窗 / Ctrl+- 縮 zoom。

### 5.3 IPC 設計

file-based IPC：
- `output/ipc/status.json` — 腳本寫當前步驟
- `output/ipc/input.txt` — boss/CC 寫回應，腳本 polling 讀
- 簡單可靠，但 status 必驗 screenshot 不要光信

### 5.4 Monitor + grep 的 `captcha` keyword 太鬆

`if "captcha" in content_low` 會 false-trigger（Page 含 hidden `<script>` 引用 captcha CDN，畫面上沒 captcha）。**用 `iframe[src*="hcaptcha.com"]:visible` 嚴格判斷**。

---

## 6. Persona 資料一致性

`personas/P0X/profile.yaml` 是 source of truth：
- `identity.real_name.first/last` — 給表單用
- `identity.date_of_birth` — DOB
- `identity.gender_register` — 表單性別（neutral/male/female）
- `identity.handle_pool` — username 候選

**P05 註冊時寫成 Female 而非 Male**（gender combobox `:has-text("Male")` 撞到 "Female"）— P05 profile.yaml 寫 male，註冊成 female。Boss 接受 shell archetype 中性化。**未來腳本用 `:text-is("Male")` 嚴格匹配。**

---

## 7. 給未來操作者的具體建議

1. **load 帳號時** — 先確認 IP / cover story 正確再 launch Camoufox load `personas/P0X/state/<platform>_storage_state.json`
2. **warmup 時** — 看 `personas/P0X/profile.yaml` 的 `algorithm_target.primary_verticals` 決定要 search 什麼 keyword、scroll 什麼 feed
3. **scope_lock 一定要遵守** — 例：P04 sports-bro 別觸碰 folk-belief / lottery（會撞 P03 演算法）
4. **遇到 IG / FB 跳 face liveness** — 永遠 abandon，不交照片
5. **未來再 register** — 用 hotel kit 的 `register_p0X_*.py` 作 base，但 DOM 持續演化（Meta 改版常見），常 dump DOM 對 selector

---

## 8. 給未來類似任務的改進清單

如果之後還要做類似 first-touch IP 註冊：
1. 提早裝 Camoufox + 測試（`Cache\` 路徑問題、視窗大小）
2. 每平台先寫好 DOM dump diagnostic（hotel kit 當時用 `diag_*.py` 樣式 — selectors 細節已沉澱在 §2.X，未來重做直接從 KB 學）
3. CAPTCHA 偵測寫嚴格（visible iframe only）
4. 拉開同 IP 多 persona 註冊時間（≥6h 間隔）
5. 寫一個 top-level orchestrator script 串連 13 個註冊，自動排序 + retry policy

---

## 9. 5/6 register 結果總表（hotel CC handoff）

| Persona | Platform | Status | Date |
|---|---|---|---|
| P03 | facebook | ✅ | 2026-05-04 23:07 |
| P03 | instagram | ✅ | 2026-05-06 11:11 |
| P03 | tiktok | ✅ (rate-limit recovered) | 2026-05-06 11:19 |
| P03 | localforum | ✅ (manual) | 2026-05-05 21:46 |
| P04 | facebook | ✅ | 2026-05-05 21:55 |
| P04 | instagram | ✅ | 2026-05-05 21:57 |
| P04 | tiktok | ✅ | 2026-05-05 22:02 |
| P04 | twitter_x | ✅ (manual) | 2026-05-05 22:12 |
| P04 | youtube | ✅ (Google login) | 2026-05-05 22:17 |
| P05 | discord | ✅ | 2026-05-06 10:54 |
| P05 | facebook | ⚠ abandoned_opsec | 2026-05-06 11:04 |
| P05 | localforum | ✅ | 2026-05-06 10:41 |
| P05 | reddit | ✅ | 2026-05-06 09:41 |

**Total: 12 ✅ / 1 ⚠**

---

## 10. Cross-references

> **Origin retired 2026-05-06**: 原 hotel CC kit (`handoff_agent/`) 已 deleted；本檔為其沉澱版。原 `register_p0?_*.py` / `diag_*.py` / `fetch_fb_code.py` 細節（specific selectors / IPC patterns / IMAP OTP fetcher）已寫入 §1-9。主框架對應實作見下:

- `agents/_common/camoufox_session.py` — main framework wrapper (uses lessons from §1)
- `agents/facebook/register.py` — main framework FB register (uses lessons from §2.1, §3, §4.1)
- `agents/bigo/register.py` — main framework Bigo register
- `agents/<platform>/warmup_session.py` × 8 — Discord / FB / IG / LocalForum / Reddit / TikTok / Twitter / YouTube warmup
- `personas/skills/CHIEF_STRATEGIST.md §6.3` — strategist refers here for persona ops
- `personas/skills/SECTION_CHIEF.md §22.5` — chief refers here when planning Field Agent platform ops

— hotel CC, 2026-05-06; sunk to KB by main session 2026-05-06T12:50+07:00; cross-refs updated 2026-05-06T18:50+07:00 post handoff_agent retirement

# 台股掃描系統 v4.9 — Web 部署說明（含密碼登入）

## ⚠️ v4.9.2 修正紀錄：「掃描錯誤」一直出現的問題

**症狀**：v4.9.1 修好卡死問題後，畫面不再永遠卡住，但變成三不五時跳出
「上次掃描發生錯誤，等待下次重試」，畫面上完全看不出真正原因是什麼。

**根本原因**（已對照程式碼確認，這次是兩個問題疊在一起）：
1. **前端從沒有把真正的錯誤訊息顯示出來。** `index.html` 裡其實已經寫好一個
   `showError(msg)` 函式，但整份程式碼裡沒有任何地方呼叫它——`pollStatus()`
   偵測到 `s.error` 時，只會把畫面上的文字換成固定的一句「上次掃描發生錯誤，
   等待下次重試」，後端回傳的真正錯誤內容（`s.error`）就這樣被丟掉，
   從頭到尾沒有人看得到，等於在盲測。
2. **單一個股/ETF分析失敗，會讓整輪 200＋ 檔全部作廢。** `app.py` 裡跑迴圈
   逐檔呼叫 `analyze_stock()` / `analyze_etf()` 時沒有各自包 `try/except`。
   只要其中「任何一檔」股票剛好遇到暫時性網路異常、資料有缺口、或計算過程中
   出現極端值（NaN/除以零等邊界情況），整個例外會一路往外拋，直接被最外層
   `except Exception` 接住，導致：已經成功算完的其他一兩百檔全部被丟棄，
   整輪掃描標記為「錯誤」。掃描對象檔數越多，統計上「至少有一檔出狀況」
   的機率就越高，這也解釋了為什麼會「一直」出現掃描錯誤，而不是偶爾一次。

**修正內容**：
- `templates/index.html`：`pollStatus()` 偵測到 `s.error` 時，現在會呼叫
  `showError()` 把後端回傳的實際錯誤訊息（含最後執行到的步驟）顯示在頁面上
  的紅色錯誤框裡；沒有錯誤或掃描中則自動隱藏錯誤框。
- `app.py`：個股與ETF的逐檔分析迴圈，每一檔都個別包 `try/except`。單一檔
  失敗只會把「那一檔」標記為分析失敗並跳過，記錄到伺服器 log，其餘檔案
  照常完成、正常顯示；只有真正影響全體的錯誤（例如撿股讚整頁抓取失敗、
  無法連線）才會讓整輪掃描標記為錯誤。掃描完成的 log 也會顯示「本輪共有
  幾檔被跳過」，方便你追蹤是不是某幾檔股票代號本身有問題。

## ⚠️ v4.9.1 修正紀錄：自動排程卡住不動的問題

**症狀**：改成自動排程後，畫面卡在「掃描中…2%」長達 30 分鐘以上，之後才冒出「掃描錯誤」。手動按鈕觸發掃描時沒有這個問題。

**根本原因**（已對照原始程式碼確認）：
1. `_scheduler_loop()` 原本是「同步」呼叫 `_run_one_scan()`。只要掃描過程中任何一個網路請求卡住不返回（例如 yfinance 呼叫完全沒有設定 timeout，Yahoo Finance 在雲端主機上偶爾會回應緩慢/被限流），整個 while 迴圈就會卡在那一行，永遠不會進到 `time.sleep()`，之後所有排定的掃描都不會再啟動——不會拋錯、也不會自己恢復。
   - 手動按鈕模式下不會有這個「無聲卡死」問題，因為使用者按下去沒反應會馬上發現、重新整理頁面即可；但自動排程沒有人在盯著，卡住可以持續非常久。
2. 管理員按「中斷並重新套用條件」時，舊程式只是設定取消旗標、最多等 3 秒就不管三七二十一開一條新執行緒。如果舊執行緒真的卡在網路請求裡（3 秒內不會醒），新舊兩條執行緒會同時寫入同一份全域結果，互相覆蓋，這就是後來冒出「掃描錯誤」的來源。
3. `render.yaml` 的 `--threads 8` 在預設的 `sync` worker class 下其實完全沒有作用（`--threads` 只有搭配 `--worker-class gthread` 才會生效），實際上一直只有單一執行緒在處理 HTTP 請求。

**修正內容**：
- `app.py`：加入「掃描世代編號（generation）」機制與看門狗逾時（`SCAN_TIMEOUT_SEC`，預設 240 秒）。排程迴圈改用獨立執行緒執行掃描並 `join(timeout=...)`，逾時就放棄本輪、記錄狀態、繼續下一輪排程，不會再無限期卡死。舊世代的執行緒即使之後才醒來，也會被自動忽略、不會弄髒目前結果。
- `scanner_core.py`：所有 `yf.Ticker(...).history(...)` 呼叫加上明確的 `timeout` 參數，並加上 `socket.setdefaulttimeout()` 作為全域安全網。
- `render.yaml`：`startCommand` 加上 `--worker-class gthread`，讓 `--threads` 參數真正生效；新增 `SCAN_TIMEOUT_SEC` 環境變數（需小於 gunicorn 的 `--timeout 300`）。


## 專案結構

```
taiwan-scanner/
├── app.py                ← Flask 後端 + 密碼驗證系統
├── scanner_core.py       ← 你的原始掃描程式（原封不動）
├── templates/
│   ├── login.html        ← 登入頁面
│   └── index.html        ← 主掃描 UI（需登入才能進入）
├── requirements.txt
├── render.yaml
└── README.md
```

---

## 密碼設定

### 方法一：直接改 app.py（本機測試用）

在 `app.py` 第 28–29 行修改：

```python
_DEFAULT_TRIAL_PASSWORDS = ['trial2024', 'demo888']   # ← 試用密碼（1天）
_DEFAULT_FULL_PASSWORDS  = ['stock2024vip', 'bob0309']  # ← 正式密碼（180天）
```

### 方法二：Render 環境變數（部署推薦，密碼不寫在程式碼裡）

在 Render Dashboard → Environment 新增：

| 變數名 | 範例值 | 說明 |
|--------|--------|------|
| `TRIAL_PASSWORDS` | `trial2024,demo888` | 試用密碼，逗號分隔，有效 1 天 |
| `FULL_PASSWORDS`  | `stock2024vip,bob0309` | 正式密碼，逗號分隔，有效 180 天 |
| `SECRET_KEY`      | （隨機產生）| Flask session 簽名金鑰 |

---

## 帳號類型

| 類型 | 有效期 | 說明 |
|------|--------|------|
| 試用帳號 | **1 天** | 輸入試用密碼後，登入狀態保持 24 小時 |
| 正式帳號 | **180 天** | 輸入正式密碼後，登入狀態保持 6 個月 |

到期後需重新輸入密碼登入。

---

## 部署到 Render（免費）

1. 建立 GitHub repo，上傳全部檔案
2. 到 https://render.com → "New +" → "Web Service" → 選 repo
3. Render 自動讀 `render.yaml`，點 "Create Web Service"
4. 在 Render Dashboard → Environment 設定密碼環境變數
5. 等 2~3 分鐘 → 取得 `https://你的名稱.onrender.com`

---

## 本機執行

```bash
pip install -r requirements.txt
python app.py
# 開瀏覽器到 http://localhost:5000
```

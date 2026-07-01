# 台股掃描系統 v4.9 — Web 部署說明（含密碼登入）

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

# 台股掃描系統 v4.9 — Web 部署說明（含密碼登入）

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

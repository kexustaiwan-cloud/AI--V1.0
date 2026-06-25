# 台股掃描系統 v4.9 — Web 部署說明

## 專案結構

```
taiwan-scanner/
├── app.py              ← Flask 後端 API
├── scanner_core.py     ← 你的原始掃描程式（原封不動）
├── templates/
│   └── index.html      ← 漂亮的 Web UI
├── requirements.txt    ← Python 套件
├── render.yaml         ← Render 部署設定
└── README.md
```

---

## 方法一：Render（免費，有公開網址）

### 步驟

1. **建立 GitHub repo**
   - 到 https://github.com/new 建立新 repo（例如 `taiwan-scanner`）
   - 把這個資料夾的全部檔案上傳進去

2. **部署到 Render**
   - 到 https://render.com 登入（可用 GitHub 帳號）
   - 點 "New +" → "Web Service"
   - 選擇你的 GitHub repo
   - Render 會自動讀取 `render.yaml`，直接點 "Create Web Service"
   - 等 2~3 分鐘部署完成

3. **取得網址**
   - Render 會給你一個 `https://taiwan-scanner.onrender.com` 格式的網址
   - 打開就能用！

### 注意事項
- Render 免費方案在閒置 15 分鐘後會休眠，第一次請求需等 30 秒喚醒
- 升級 $7/月 的方案可避免休眠

---

## 方法二：本機執行

```bash
# 安裝套件
pip install -r requirements.txt

# 啟動伺服器
python app.py

# 開瀏覽器到
# http://localhost:5000
```

---

## 方法三：Railway（備選，也免費）

1. 到 https://railway.app
2. "New Project" → "Deploy from GitHub repo"
3. 選你的 repo → 自動偵測 Python → Deploy

---

## 使用方式

1. 開啟網址後，左側調整參數：
   - K值門檻（預設30）
   - YOY年增率下限（預設15%）
   - 毛利率 / 營業利益率門檻
   - 財務篩選模式
2. 點「開始掃描」
3. 左下角顯示即時 log
4. 掃描完成後右側顯示結果：
   - A/B/C/D 類進場訊號（附財務品質、KD、均線、YOY、MoM）
   - ETF 買進訊號
   - 出場訊號
   - 觀察中股票

---

## FinMind Token（可選）

如需更精確財務資料，在 `scanner_core.py` 第 40 行填入：

```python
FINMIND_TOKEN = '你的token'
```

或在 Render 環境變數設定 `FINMIND_TOKEN`，並在 `app.py` 加入：

```python
import os
sc.FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', '')
```

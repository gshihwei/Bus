# 🚌 LINE Bot 公車到站查詢

透過 LINE Bot 查詢指定路線公車到達指定站點的到站時間及車牌號碼。  
資料來源：交通部 TDX（Transport Data eXchange）平台。

---

## 📱 使用方式

傳送訊息格式：
```
往[目的地] [路線] [站名]
```

### 範例
```
往新竹 1728 花開富貴
往台北 9 三重國小
往板橋 307 西門
往左營 高鐵快線 左營
```

### 回覆內容
- 🕐 到站預報（前 3 班車）
- 🚐 即時車輛位置（車牌、目前所在站、距離幾站）

---

## 🛠️ 部署步驟

### 第一步：申請 TDX API 金鑰

1. 前往 [TDX 平台](https://tdx.transportdata.tw/) 註冊帳號
2. 登入後進入「個人資料」→「應用程式管理」
3. 新增應用程式，取得 `Client ID` 和 `Client Secret`

> 免費方案每日 50,000 次請求，足夠個人使用

---

### 第二步：建立 LINE Bot

1. 前往 [LINE Developers](https://developers.line.biz/) 登入
2. 建立 Provider → 建立 Messaging API Channel
3. 取得：
   - **Channel Secret**（Basic settings 頁面）
   - **Channel Access Token**（Messaging API 頁面 → Issue）
4. 關閉「Auto-reply messages」和「Greeting messages」

---

### 第三步：部署到 Render

#### 方法一：使用 render.yaml（推薦）

1. 將專案推送到 GitHub：
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/linebot-bus.git
   git push -u origin main
   ```

2. 前往 [Render](https://render.com/) 登入
3. New → Web Service → 連接你的 GitHub repo
4. Render 會自動讀取 `render.yaml` 設定

5. 在 Environment Variables 填入：

   | Key | Value |
   |-----|-------|
   | `LINE_CHANNEL_ACCESS_TOKEN` | 你的 LINE Channel Access Token |
   | `LINE_CHANNEL_SECRET` | 你的 LINE Channel Secret |
   | `TDX_CLIENT_ID` | 你的 TDX Client ID |
   | `TDX_CLIENT_SECRET` | 你的 TDX Client Secret |

6. 點擊 **Create Web Service** 開始部署

---

### 第四步：設定 LINE Webhook

1. 部署完成後，Render 會給你一個網址，例如：
   ```
   https://linebot-bus-query.onrender.com
   ```

2. 前往 LINE Developers → 你的 Channel → Messaging API
3. Webhook URL 填入：
   ```
   https://linebot-bus-query.onrender.com/callback
   ```
4. 開啟「Use webhook」
5. 點擊「Verify」確認連線正常

---

## 📁 專案結構

```
linebot_bus/
├── app.py           # Flask 主程式、LINE webhook 處理
├── tdx_client.py    # TDX API 認證與資料查詢
├── bus_query.py     # 查詢解析與訊息格式化
├── requirements.txt # Python 套件
├── render.yaml      # Render 部署設定
├── Procfile         # 啟動指令
└── README.md
```

---

## 🔧 本地測試

```bash
# 安裝套件
pip install -r requirements.txt

# 設定環境變數
export LINE_CHANNEL_ACCESS_TOKEN="your_token"
export LINE_CHANNEL_SECRET="your_secret"
export TDX_CLIENT_ID="your_tdx_id"
export TDX_CLIENT_SECRET="your_tdx_secret"

# 啟動服務
python app.py

# 使用 ngrok 對外開放（另開終端機）
ngrok http 5000
```

本地測試時，將 ngrok 提供的 https URL + `/callback` 填入 LINE Webhook URL。

---

## 🗺️ 支援範圍

| 類型 | 說明 |
|------|------|
| 市區公車 | 台北、新北、桃園、台中、台南、高雄、新竹市/縣等 |
| 公路客運 | 統聯、國光、葛瑪蘭等（InterCity） |

---

## 📊 到站狀態說明

| 圖示 | 狀態 |
|------|------|
| 🔵 | 進站中（30秒內） |
| 🟢 | 即將到站（1分鐘內） |
| 🟡 | 5分鐘內到站 |
| 🟠 | 5分鐘以上 |
| ⚫ | 末班車已過 |
| ⏸️ | 尚未發車 |
| ⛔ | 交管不停靠 |

---

## ⚠️ 注意事項

- Render 免費方案在閒置後會進入休眠，首次請求可能需要 30-60 秒喚醒
- 建議升級到 Render 付費方案避免休眠，或使用 UptimeRobot 定期 ping 保持活躍
- TDX 資料更新頻率約每 30 秒至 1 分鐘

---

## 📡 API 使用的 TDX Endpoints

- `GET /v2/Bus/Route/{City}/{RouteName}` - 路線資訊
- `GET /v2/Bus/EstimatedTimeOfArrival/{City}/{RouteName}` - 到站時間預估
- `GET /v2/Bus/RealTimeByFrequency/{City}/{RouteName}` - 即時車輛位置
- `GET /v2/Bus/StopOfRoute/{City}/{RouteName}` - 路線站序

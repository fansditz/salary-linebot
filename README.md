# Salary LINE Bot

用來記錄上班工時與薪水的 LINE Bot 後端。支援三個主要指令：

- `工作資訊`：設定時薪、薪資起始日、結算日、發薪日。
- `社畜人來打卡啦！`：逐步輸入上班日期、上班時間、下班時間、休息時間與是否計薪，後端會自動計算工時與日薪。
- `偷偷給我看一下薪水吧......`：列出目前結算區間薪水，並顯示最近月份小計。

## 本機執行

1. 安裝 Python 3.12。
2. 安裝套件：

```bash
pip install -r requirements.txt
```

3. 建立環境變數，可參考 `.env.example`：

```bash
set LINE_CHANNEL_ACCESS_TOKEN=你的 token
set LINE_CHANNEL_SECRET=你的 secret
set DATABASE_PATH=salary_linebot.db
```

4. 啟動服務：

```bash
python app.py
```

服務啟動後，健康檢查網址是：

```text
http://localhost:5000/
```

Webhook 路徑是：

```text
http://localhost:5000/webhook
```

## Render 部署

1. 將專案推到 GitHub。
2. 在 Render 建立新的 Web Service，連到這個 repo。
3. Build Command 填：

```bash
pip install -r requirements.txt
```

4. Start Command 填：

```bash
gunicorn app:app
```

5. 設定環境變數：

- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `DATABASE_PATH=/var/data/salary_linebot.db`

6. 如果要讓 SQLite 資料在重新部署後保留，Render 需要掛 persistent disk：

- Mount Path: `/var/data`
- Size: `1 GB`

7. 部署完成後，到 LINE Developers 後台把 Webhook URL 設成：

```text
https://你的-render網域/webhook
```

## 輸入格式

- 日期：`YYYY-MM-DD`，例如 `2026-07-05`
- 時間：24 小時制 `HH:MM`，例如 `09:00`、`18:30`
- 休息時間：分鐘整數，例如 `60`
- 休息是否計薪：`是` 或 `否`

如果下班時間早於上班時間，系統會自動視為跨日班。

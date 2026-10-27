# 訂房價格監控

監控指定房源的價格，一降價就通知，並提供價格走勢圖網頁。
資料來源：Google Hotels（透過 [SerpApi](https://serpapi.com)），彙整 Booking、Agoda、Expedia、官網等報價。

## 本機使用

```bash
export SERPAPI_KEY=你的key
python hotel_monitor.py find "東京 京王廣場飯店"   # 找房源 token，貼進 config.toml
python hotel_monitor.py check                      # 檢查一次
python hotel_monitor.py serve                      # 開網頁 http://127.0.0.1:8000，並自動定時檢查
```

需要 Python 3.11 以上，不用安裝其他套件。

## 放到雲端（GitHub Actions + GitHub Pages，免費）

雲端每天定時檢查兩次，降價就發 Telegram 通知，網頁在 `https://<帳號>.github.io/<repo>/`。

1. **建立 GitHub repo**，把這個資料夾的檔案推上去。
   GitHub 免費方案只有 **public repo** 能用 Pages；網頁和價格紀錄會公開（API key 不會）。
2. **設定 Secrets**：repo → Settings → Secrets and variables → Actions → New repository secret
   - `SERPAPI_KEY`（必填）
   - `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（選填，要收通知才需要）
3. **開啟 Pages**：repo → Settings → Pages → Source 選 **GitHub Actions**。
4. **找房源 token**：repo → Actions → 「找房源」→ Run workflow，輸入飯店名稱。
   跑完點進去，結果頁面會列出 token，再到 `config.toml` 按鉛筆圖示編輯、貼上。
5. **第一次執行**：repo → Actions → 「房價監控」→ Run workflow。
   之後會照排程自動執行，網頁上的「手動執行 ↗」也會連到這裡。

### 調整檢查頻率

編輯 `.github/workflows/monitor.yml` 的 `cron`（時間是 UTC，台灣時間 = UTC + 8）：

| 設定 | 意思 |
|---|---|
| `23 0,12 * * *` | 每天 2 次（台灣 08:23、20:23）← 預設 |
| `23 */6 * * *` | 每 6 小時 |
| `23 * * * *` | 每小時 |

SerpApi 免費方案每月 100 次，用量 ≈ 房源數 × 每天次數 × 30。
例如 2 間房源、每天 2 次 = 每月 120 次，會超過免費額度，可以減少房源或升級方案。

### 注意

- 監控的日期過了之後，檢查會失敗（GitHub 會寄信通知），記得更新 `config.toml` 的日期。
- 價格紀錄存在 repo 裡的 `hotel_monitor.db`，每次檢查會自動 commit。
- 公開 repo 如果 60 天都沒有任何活動，GitHub 會停用排程；每次自動 commit 價格紀錄都算活動，正常不會發生。

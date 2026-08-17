# 復健醫學 繼續教育活動彙整

把散落在各學會網站的復健醫學積分課程抓下來，變成一頁可以用**時間／地區／積分／主題／來源**篩選的清單。
純靜態網站，資料存成一份 JSON，靠 GitHub Actions 每天自動更新，沒有伺服器也沒有資料庫。

> 資料只是彙整索引，**報名與積分認定一律以主辦單位公告為準**。

## 網站長怎樣

- 上方是統計（幾場活動、日期範圍、幾個來源、最後更新時間）
- 中間是六組篩選器（時間／地區／積分／主題／主辦／來源）+ 關鍵字搜尋 + 三種排序
- 下面是活動卡片，點標題直接連到該學會的原始公告頁
- 手機上篩選器預設收起來，第一屏就看得到活動

## 資料從哪來

| 來源 | 狀態 | 說明 |
|------|------|------|
| [台灣復健醫學會](https://www.pmr.org.tw/active_news/active.asp) | ✅ 已接 | 主來源。全台各醫院／學會申請復健積分的課程都會登記在這裡，涵蓋度最高 |
| [台灣兒童復健醫學會](https://www.tapedpmr.org.tw/activity/index.asp) | ✅ 已接 | 補兒童復健／早療這一塊，會進詳情頁補抓地點與主辦單位 |
| [中華民國復健醫學發展協會](https://www.rmdaroc.org/) | ⏸ 未接 | 網站是 Google Sites，內容靠 JavaScript 載入，靜態抓不到，要接得改用 headless 瀏覽器 |
| [台灣心肺復健醫學會](https://www.tacvpr-taiwan.com/) | ⏸ 未接 | 站台掛在 Cloudflare 人機驗證後面，一般程式請求會被擋（HTTP 403） |

兩個未接的來源不是做不到，是要多付一層代價（跑 headless 瀏覽器、而且會被反爬機制影響穩定度）。
先把兩個穩定的來源做好，需要時再加。

## 怎麼加新來源

這是整包設計最重要的地方：**一個來源 = 一支獨立檔案**，加來源不用動前端也不用動排程。

1. 在 `sources/` 底下新增一支 `xxx.py`，裡面要有：
   - `NAME`：學會名稱（會顯示在網站的來源篩選器上）
   - `fetch()`：回傳 `list[Event]`
2. 把它加進 `scripts/build.py` 最上面的 `SOURCES` 清單
3. 跑 `python3 scripts/build.py --dry-run` 確認抓得到

`Event` 的欄位、地區判定、積分解析、分類判定全部在 `sources/base.py`，新來源直接拿來用就好：

```python
from .base import Event, get, parse_date, parse_credits, detect_region, detect_categories
```

地區判定刻意**只看地點不看標題**——標題裡的「視訊」「遠距」常常是課程主題而不是上課形式，
看標題會把實體課誤標成線上課（已踩過這個坑，`base.detect_region` 的註解有寫）。

## 本機怎麼跑

```bash
pip install -r requirements.txt
python3 scripts/build.py          # 抓資料 → data/events.json
python3 -m http.server 8899       # 開 http://127.0.0.1:8899
```

只想看抓到什麼、不想寫檔：`python3 scripts/build.py --dry-run`

## 自動更新

`.github/workflows/update.yml` 每天台灣時間 06:00 跑一次，有變動才 commit（也可以在 Actions 頁面手動觸發）。
也可以到 repo 的 Actions 頁面按 **Run workflow** 手動更新。

## 部署到 GitHub Pages

Settings → Pages → Source 選 **Deploy from a branch**，branch 選 `main`、資料夾選 `/ (root)`，存檔即可。

## 單一來源掛掉會怎樣

不會整包壞掉。某個來源抓失敗時，其他來源照常更新，失敗訊息會寫進 `data/events.json` 的 `errors` 欄位，
網站頂部會跳一條提示 —— 這樣來源網站改版時**看得見**，而不是資料默默變少沒人發現。

## 檔案結構

```
sources/base.py     共用資料結構與判定邏輯（積分、地區、分類、日期）
sources/pmr.py      台灣復健醫學會
sources/tapedpmr.py 台灣兒童復健醫學會
scripts/build.py    跑所有來源 → 合併去重 → 寫出 data/events.json
data/events.json    網站唯一的資料來源（自動產生，不要手改）
index.html          頁面結構
assets/style.css    樣式
assets/app.js       篩選與排序（原生 JS，沒有框架）
```

## 授權

MIT，見 `LICENSE`。

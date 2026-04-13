# EnergyLabel

這個專案是「能源效率分級圖檔下載器」的線上版。

前端建議部署到 Cloudflare Pages，後端建議部署到 Render。

這樣拆開的原因很直接：

- Cloudflare Pages Functions / Workers 連到 `ranking.energylabel.org.tw` 時，會因為對方憑證鏈問題遇到 `HTTP 526`
- 本專案的 Render 後端會像你原本本地版一樣，使用 `verify=False` 去抓取官網資料，因此能正常工作
- 前端仍然只負責輸入型號、顯示進度、預覽圖片、把圖片下載到使用者瀏覽器，不會把檔案存到伺服器

## 專案結構

`public/`

- Cloudflare Pages 的靜態前端
- `index.html` 是主頁
- `config.js` 用來設定 Render API 網址

`backend/`

- Render 用的 FastAPI 後端
- `app/main.py` 提供 API
- `requirements.txt` 是 Python 套件

`render.yaml`

- Render Blueprint 設定檔

## API 說明

後端提供兩支 API：

`GET /healthz`

- 健康檢查
- 正常時回傳 `{"status":"ok"}`

`POST /api/download`

- 一次回傳整批結果
- Request body:

```json
{
  "models": ["28cmtd", "rxv50zvlt"]
}
```

- Response body:

```json
{
  "results": [
    {
      "model": "28cmtd",
      "result": {
        "status": "ok",
        "base64": "..."
      }
    }
  ]
}
```

`POST /api/download-stream`

- SSE 串流版
- 前端目前使用這支，因為可以逐筆顯示進度

## Render 部署

### 方式一：用 Blueprint

1. 把這個 repo 推到 GitHub
2. 到 Render 建立新服務
3. 選 `Blueprint`
4. 指向這個 repo
5. Render 會讀取 repo 內的 `render.yaml`
6. 建立完成後，等待第一次部署成功

這是最省事的做法。

### 方式二：手動建立 Web Service

如果你不用 Blueprint，可以手動建立：

1. New `Web Service`
2. 選這個 GitHub repo
3. 確認以下設定：

- Environment: `Python`
- Root Directory: `backend`
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

4. 建立完成後，記下 Render 網址，例如：

```text
https://energylabel-api.onrender.com
```

### Render 環境變數

`render.yaml` 已經幫你先寫好預設值，但你也可以在 Render 後台修改：

- `ALLOWED_ORIGINS`
  用來限制可呼叫 API 的前端來源
  預設是 `https://energylabel.pages.dev`

- `MAX_ATTEMPTS`
  單一型號最多重試次數
  預設是 `5`

- `BACKOFF_BASE_MS`
  重試退避基準毫秒數
  預設是 `1200`

如果之後你的 Cloudflare Pages 網址換掉，記得同步更新 `ALLOWED_ORIGINS`。

### Render 部署完成後檢查

先打健康檢查：

```bash
curl https://your-render-service.onrender.com/healthz
```

預期結果：

```json
{"status":"ok"}
```

再打下載 API：

```bash
curl -X POST https://your-render-service.onrender.com/api/download \
  -H 'content-type: application/json' \
  --data '{"models":["28cmtd"]}'
```

如果成功，回應裡會有：

- `status: "ok"`
- `base64: "..."`

## Cloudflare Pages 部署

前端還是放 Cloudflare Pages。

### Pages 建立方式

1. 在 Cloudflare Pages 建立新專案
2. 指向這個 repo
3. 使用以下設定：

- Framework preset: `None`
- Build command: 留空
- Build output directory: `public`

### 設定前端 API 網址

修改 `public/config.js`：

```js
window.ENERGYLABEL_API_BASE = "https://your-render-service.onrender.com";
```

把 `your-render-service` 換成你實際的 Render 網址。

之後重新部署 Cloudflare Pages。

### 前端部署完成後檢查

打開 Cloudflare Pages 網址後，頁面上方資訊列會顯示目前綁定的 API。

若你看到：

```text
API：尚未設定 Render API
```

表示 `public/config.js` 還沒改對，或最新版本還沒部署上去。

## 本機測試

如果你要先在本機跑後端：

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8010
```

然後你可以測：

```bash
curl http://127.0.0.1:8010/healthz
```

```bash
curl -X POST http://127.0.0.1:8010/api/download \
  -H 'content-type: application/json' \
  --data '{"models":["28cmtd"]}'
```

## 為什麼本地版可以，Cloudflare 不行

這是這次改成 Render 的核心原因。

原本本地版 Python 程式在抓上游官網時用了 `verify=False`，等於不驗證對方 TLS 憑證鏈。

Cloudflare Pages Functions / Workers 不能這樣做，所以遇到官網憑證鏈不完整或 Cloudflare 不接受的情況時，就會出現：

- `HTTP 526`
- `Connection timed out`
- 偶發的上游連線錯誤

Render 後端是一般 Python 服務，能重現本地版行為，因此可行。

## 重試機制

後端已內建重試與退避：

- 單一型號最多重試 `5` 次
- 退避為指數增加
- 失敗時會把重試次數附在訊息中
- 成功但曾重試，也會在前端顯示重試次數

## 常見問題

### 1. 前端顯示「尚未設定 Render API」

原因：

- `public/config.js` 還沒改
- 改了但 Cloudflare Pages 還沒重新部署

處理：

- 檢查 `public/config.js`
- 重新部署 Pages

### 2. Render `/healthz` 正常，但前端呼叫失敗

原因通常是 CORS。

處理：

- 到 Render 看 `ALLOWED_ORIGINS`
- 把你的 Cloudflare Pages 網址填進去

### 3. Render 回傳 `找不到此型號（官網搜尋無結果）`

代表程式真的沒從官網結果頁找到對應型號，不是 TLS 問題。

建議：

- 先去官網手動查同一個型號
- 看是否需要輸入完整型號
- 檢查是否有空格、破折號、大小寫差異

### 4. Render 回傳 `官網回應異常` 或 `官網連線失敗`

代表上游官網暫時不穩，或該時段拒絕請求。

這種情況跟你的前端無關。

## 之後要改哪裡

如果你要改前端介面，主要看：

- `public/index.html`

如果你要改後端抓圖邏輯，主要看：

- `backend/app/main.py`

如果你要調整 Render 部署參數，主要看：

- `render.yaml`

如果你要換 Render API 網址，主要看：

- `public/config.js`

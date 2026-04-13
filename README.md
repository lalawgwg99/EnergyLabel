# EnergyLabel

前端可部署到 Cloudflare Pages，後端部署到 Render。

## Render

1. 在 Render 建立 `Blueprint` 或 `Web Service`，指向這個 repo。
2. 若使用 `Blueprint`，直接套用 repo 內的 `render.yaml`。
3. 若手動建立：
   - Root Directory: `backend`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. 部署完成後，確認 `https://<your-render-service>.onrender.com/healthz` 可回傳 `{"status":"ok"}`。

## Cloudflare Pages

1. 保持靜態輸出目錄為 `public`。
2. 修改 `public/config.js`，填入 Render 網址：

```js
window.ENERGYLABEL_API_BASE = "https://your-render-service.onrender.com";
```

3. 重新部署 Cloudflare Pages。

## 說明

- Render 後端會直接連到能源標章官網，並關閉上游 TLS 驗證，這是為了避開 Cloudflare Workers/Pages 會遇到的 `526`。
- 前端仍然在瀏覽器下載圖片，不會在 Render 伺服器存檔。

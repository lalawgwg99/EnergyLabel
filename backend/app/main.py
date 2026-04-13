import asyncio
import json
import os
import re
import urllib3

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TARGET = "https://ranking.energylabel.org.tw/product/Approval"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
    "Referer": f"{TARGET}/list.aspx",
}
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "5"))
BACKOFF_BASE_MS = int(os.getenv("BACKOFF_BASE_MS", "1200"))


def get_allowed_origins() -> list[str]:
    raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
    if raw == "*":
        return ["*"]
    return [item.strip() for item in raw.split(",") if item.strip()]


app = FastAPI(title="Energy Label Render API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


def escape_regexp(text: str) -> str:
    return re.escape(text)


def find_links(soup_or_none) -> list:
    if soup_or_none is None:
        return []
    return soup_or_none.find_all("a", href=re.compile(r"upt\.aspx\?.*id=\d+"))


def pick_best_link(links: list, soup_or_none, model: str):
    if not links:
        return None

    model_norm = re.sub(r"\s+", "", (model or "")).upper()
    if not model_norm:
        return links[0]

    scored: list[tuple[int, object]] = []
    for anchor in links:
        row = anchor.find_parent("tr") or anchor.find_parent("table") or anchor.parent
        row_text = ""
        try:
            row_text = (row.get_text(" ", strip=True) if row else "") or ""
        except Exception:
            row_text = ""
        row_norm = re.sub(r"\s+", "", row_text).upper()

        score = 0
        if model_norm in row_norm:
            score += 10
        if re.search(rf"(?<![A-Z0-9]){escape_regexp(model_norm)}(?![A-Z0-9])", row_norm):
            score += 5
        if score:
            scored.append((score, anchor))

    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    if len(links) == 1:
        return links[0]

    return None


async def aspnet_post_search(client: httpx.AsyncClient, model: str):
    url = f"{TARGET}/list.aspx"
    page = await client.get(url, timeout=20)
    page.raise_for_status()

    soup = BeautifulSoup(page.text, "html.parser")
    form: dict[str, str] = {}
    for hidden in soup.find_all("input", type="hidden"):
        if hidden.get("name"):
            form[hidden["name"]] = hidden.get("value", "")

    form["ctl00$CPage$key2"] = model
    form["ctl00$CPage$key"] = ""
    form["ctl00$CPage$Type"] = ""
    form["ctl00$CPage$RANK"] = ""
    form["ctl00$CPage$comp"] = "0"
    form["ctl00$CPage$approvedateA"] = ""
    form["ctl00$CPage$approvedateB"] = ""
    form["ctl00$CPage$condiA"] = ""
    form["ctl00$CPage$condiB"] = ""
    form["ctl00$CPage$btnSearch"] = "查  詢"

    resp = await client.post(
        url,
        data=form,
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


async def get_search_fallback(client: httpx.AsyncClient, model: str):
    combos = [
        f"key2={model}",
        f"key2={model}&Type=&RANK=&con=",
        f"key2={model}&Type=0&RANK=0&con=0",
    ]
    for params in combos:
        response = await client.get(f"{TARGET}/list.aspx?{params}", timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        links = find_links(soup)
        if links:
            return soup, links
    return None, []


async def fetch_image_once(model: str) -> dict:
    async with httpx.AsyncClient(
        verify=False,
        follow_redirects=True,
        headers=HEADERS,
    ) as client:
        try:
            fallback_soup, fallback_links = await get_search_fallback(client, model)
            picked = pick_best_link(fallback_links, fallback_soup, model)

            if not picked:
                result_soup = await aspnet_post_search(client, model)
                links = find_links(result_soup)
                picked = pick_best_link(links, result_soup, model)

            if not picked:
                return {"status": "error", "message": "找不到此型號（官網搜尋無結果）"}

            href = picked.get("href", "")
            p0 = re.search(r"p0=(\d+)", href)
            id_ = re.search(r"id=(\d+)", href)
            if not p0 or not id_:
                return {"status": "error", "message": "無法解析產品連結"}

            img_url = f"{TARGET}/ImgViewer.ashx?applyID={id_.group(1)}&goodID={p0.group(1)}"
            img_resp = await client.get(img_url, timeout=30)
            img_resp.raise_for_status()

            img_soup = BeautifulSoup(img_resp.text, "html.parser")
            tag = img_soup.find("img")
            if not tag or "base64," not in tag.get("src", ""):
                return {"status": "error", "message": "無法取得圖檔（頁面未回傳圖片）"}

            base64_data = tag["src"].split("base64,")[1].strip()
            if len(base64_data) < 100:
                return {"status": "error", "message": "圖檔資料異常"}

            return {"status": "ok", "base64": base64_data}
        except httpx.HTTPStatusError as exc:
            return {
                "status": "error",
                "message": f"官網回應異常 (HTTP {exc.response.status_code})",
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}


async def sleep(ms: int):
    await asyncio.sleep(ms / 1000)


def should_retry(result: dict) -> bool:
    if result.get("status") != "error":
        return False
    msg = str(result.get("message", ""))
    if "找不到此型號" in msg:
        return False
    if "無法解析產品連結" in msg:
        return False
    return True


async def fetch_image_with_retry(model: str) -> dict:
    result = await fetch_image_once(model)
    attempts = 1

    while attempts < MAX_ATTEMPTS and should_retry(result):
        retry_index = attempts - 1
        await sleep(BACKOFF_BASE_MS * (2**retry_index))
        attempts += 1
        result = await fetch_image_once(model)

    if attempts > 1 and result.get("status") == "error":
        return {**result, "message": f"{result['message']}（已重試 {attempts - 1} 次）"}

    if attempts > 1 and result.get("status") == "ok":
        return {**result, "retries": attempts - 1}

    return result


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/download")
async def download(request: Request):
    body = await request.json()
    models = [str(item).strip() for item in body.get("models", []) if str(item).strip()]
    if not models:
        raise HTTPException(400, "未提供型號")

    results = []
    for model in models:
        result = await fetch_image_with_retry(model)
        results.append({"model": model, "result": result})
        await sleep(800)

    return JSONResponse({"results": results})


@app.post("/api/download-stream")
async def download_stream(request: Request):
    body = await request.json()
    models = [str(item).strip() for item in body.get("models", []) if str(item).strip()]
    if not models:
        raise HTTPException(400, "未提供型號")

    async def event_generator():
        for index, model in enumerate(models):
            result = await fetch_image_with_retry(model)
            payload = json.dumps(
                {"index": index, "model": model, "result": result},
                ensure_ascii=False,
            )
            yield f"data: {payload}\n\n"
            if index < len(models) - 1:
                await sleep(800)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

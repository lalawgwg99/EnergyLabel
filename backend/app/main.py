import asyncio
import base64
import copy
import io
import json
import math
import os
import re
import time
import urllib3
import zipfile
from collections import OrderedDict

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
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "5"))
BACKOFF_BASE_MS = int(os.getenv("BACKOFF_BASE_MS", "1200"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))
CACHE_MAX_ITEMS = int(os.getenv("CACHE_MAX_ITEMS", "500"))
BATCH_COOLDOWN_SECONDS = int(os.getenv("BATCH_COOLDOWN_SECONDS", "30"))
BATCH_RETRY_RATIO = float(os.getenv("BATCH_RETRY_RATIO", "0.35"))
BATCH_RETRY_MIN_ERRORS = int(os.getenv("BATCH_RETRY_MIN_ERRORS", "2"))


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

MODEL_CACHE: OrderedDict[str, dict] = OrderedDict()
CACHE_LOCK = asyncio.Lock()


def normalize_model(model: str) -> str:
    return str(model or "").strip()


def cache_key_for(model: str) -> str:
    return re.sub(r"\s+", "", normalize_model(model)).upper()


def sleep_seconds(seconds: float):
    return asyncio.sleep(seconds)


async def sleep_ms(ms: int):
    await asyncio.sleep(ms / 1000)


def escape_regexp(text: str) -> str:
    return re.escape(text)


def find_links(soup_or_none) -> list:
    if soup_or_none is None:
        return []
    return soup_or_none.find_all("a", href=re.compile(r"upt\.aspx\?.*id=\d+"))


def pick_best_link(links: list, soup_or_none, model: str):
    if not links:
        return None

    model_norm = re.sub(r"\s+", "", normalize_model(model)).upper()
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


def unique_name(existing: set[str], model: str, ext: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\s]+', "_", normalize_model(model) or "image").strip("_") or "image"
    candidate = f"{safe}{ext}"
    index = 2
    while candidate in existing:
        candidate = f"{safe}_{index}{ext}"
        index += 1
    existing.add(candidate)
    return candidate


def make_error(code: str, message: str, retryable: bool, **extra) -> dict:
    payload = {
        "status": "error",
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    payload.update(extra)
    return payload


def make_success(model: str, base64_data: str, **extra) -> dict:
    payload = {
        "status": "ok",
        "code": "ok",
        "message": "下載成功",
        "model": model,
        "base64": base64_data,
        "retryable": False,
    }
    payload.update(extra)
    return payload


def classify_http_status(status_code: int) -> dict:
    if status_code in {401, 403}:
        return make_error("access_denied", f"官網拒絕連線 (HTTP {status_code})", False)
    if status_code in RETRYABLE_STATUS_CODES:
        return make_error("upstream_unavailable", f"官網暫時故障 (HTTP {status_code})", True)
    return make_error("upstream_unavailable", f"官網回應異常 (HTTP {status_code})", False)


def classify_http_exception(exc: Exception) -> dict:
    if isinstance(exc, httpx.HTTPStatusError):
        return classify_http_status(exc.response.status_code)
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return make_error("upstream_unavailable", "官網暫時故障（連線逾時）", True)
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return make_error("upstream_unavailable", "官網暫時故障（網路連線失敗）", True)
    message = str(exc).strip() or "未知錯誤"
    return make_error("unknown", f"未知錯誤：{message}", False)


def should_cache(result: dict) -> bool:
    if result.get("status") == "ok":
        return True
    return result.get("code") == "not_found"


async def get_cached_result(model: str):
    key = cache_key_for(model)
    async with CACHE_LOCK:
        entry = MODEL_CACHE.get(key)
        if not entry:
            return None
        if entry["expires_at"] <= time.time():
            MODEL_CACHE.pop(key, None)
            return None
        MODEL_CACHE.move_to_end(key)
        return copy.deepcopy(entry["result"])


async def put_cached_result(model: str, result: dict):
    if not should_cache(result):
        return
    key = cache_key_for(model)
    payload = copy.deepcopy(result)
    payload["cached"] = True
    payload["source"] = "cache"
    async with CACHE_LOCK:
        MODEL_CACHE[key] = {
            "expires_at": time.time() + CACHE_TTL_SECONDS,
            "result": payload,
        }
        MODEL_CACHE.move_to_end(key)
        while len(MODEL_CACHE) > CACHE_MAX_ITEMS:
            MODEL_CACHE.popitem(last=False)


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

    response = await client.post(
        url,
        data=form,
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


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
                return make_error("not_found", "型號不存在或官網查無結果", False)

            href = picked.get("href", "")
            p0 = re.search(r"p0=(\d+)", href)
            id_ = re.search(r"id=(\d+)", href)
            if not p0 or not id_:
                return make_error("image_parse_error", "官網產品連結格式異常", False)

            img_url = f"{TARGET}/ImgViewer.ashx?applyID={id_.group(1)}&goodID={p0.group(1)}"
            img_response = await client.get(img_url, timeout=30)
            img_response.raise_for_status()

            img_soup = BeautifulSoup(img_response.text, "html.parser")
            tag = img_soup.find("img")
            if not tag or "base64," not in tag.get("src", ""):
                return make_error("image_parse_error", "圖片解析失敗：官網未回傳圖片資料", False)

            base64_data = tag["src"].split("base64,")[1].strip()
            if len(base64_data) < 100:
                return make_error("image_parse_error", "圖片解析失敗：回傳圖檔資料異常", False)

            return make_success(model, base64_data, source="live", cached=False)
        except Exception as exc:
            return classify_http_exception(exc)


def should_retry(result: dict) -> bool:
    return result.get("status") == "error" and bool(result.get("retryable"))


async def fetch_image_with_retry(model: str, bypass_cache: bool = False) -> dict:
    normalized_model = normalize_model(model)
    if not bypass_cache:
        cached = await get_cached_result(normalized_model)
        if cached:
            return {
                **cached,
                "model": normalized_model,
                "cache_hit": True,
                "retries": 0,
                "attempts": 0,
            }

    attempts = 0
    result = None
    while attempts < MAX_ATTEMPTS:
        attempts += 1
        result = await fetch_image_once(normalized_model)
        result["model"] = normalized_model
        result["attempts"] = attempts
        result["retries"] = max(0, attempts - 1)
        result["cache_hit"] = False
        if result.get("status") == "ok":
            break
        if not should_retry(result) or attempts >= MAX_ATTEMPTS:
            break
        await sleep_ms(BACKOFF_BASE_MS * (2 ** (attempts - 1)))

    if result is None:
        result = make_error("unknown", "未知錯誤：未取得任何結果", False, model=normalized_model)

    if should_cache(result):
        await put_cached_result(normalized_model, result)

    return result


def should_run_batch_cooldown(results: list[dict]) -> bool:
    transient_errors = [item for item in results if item and should_retry(item["result"])]
    if len(transient_errors) < BATCH_RETRY_MIN_ERRORS:
        return False
    threshold = max(BATCH_RETRY_MIN_ERRORS, math.ceil(len(results) * BATCH_RETRY_RATIO))
    return len(transient_errors) >= threshold


async def run_batch(models: list[str], event_callback=None) -> dict:
    normalized_models = [normalize_model(model) for model in models if normalize_model(model)]
    results: list[dict] = []

    async def emit(payload: dict):
        if event_callback is not None:
            await event_callback(payload)

    for index, model in enumerate(normalized_models):
        result = await fetch_image_with_retry(model)
        row = {"index": index, "model": model, "result": result}
        results.append(row)
        await emit({"type": "result", **row})
        if index < len(normalized_models) - 1:
            await sleep_ms(800)

    batch_retry_indices = [
        item["index"]
        for item in results
        if item["result"].get("status") == "error" and item["result"].get("retryable")
    ]

    should_cooldown = should_run_batch_cooldown(results)
    if should_cooldown:
        await emit(
            {
                "type": "notice",
                "stage": "batch_cooldown",
                "message": (
                    f"偵測到 {len(batch_retry_indices)} 筆暫時性失敗，"
                    f"系統將冷卻 {BATCH_COOLDOWN_SECONDS} 秒後再試一次。"
                ),
            }
        )
        await sleep_seconds(BATCH_COOLDOWN_SECONDS)
        await emit(
            {
                "type": "notice",
                "stage": "batch_retry",
                "message": "開始第二輪批次重試。",
            }
        )
        for position, index in enumerate(batch_retry_indices):
            model = normalized_models[index]
            retry_result = await fetch_image_with_retry(model, bypass_cache=True)
            retry_result["batch_retry"] = True
            results[index] = {"index": index, "model": model, "result": retry_result}
            await emit({"type": "result", **results[index]})
            if position < len(batch_retry_indices) - 1:
                await sleep_ms(800)

    ok_count = sum(1 for item in results if item["result"].get("status") == "ok")
    error_count = len(results) - ok_count
    cache_hits = sum(1 for item in results if item["result"].get("cache_hit"))
    return {
        "results": results,
        "meta": {
            "ok_count": ok_count,
            "error_count": error_count,
            "cache_hits": cache_hits,
            "batch_retry_count": len(batch_retry_indices) if should_cooldown else 0,
        },
    }


def build_zip_bundle(items: list[dict], results: list[dict], bundle_name: str) -> bytes:
    archive = io.BytesIO()
    used_names: set[str] = set()
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())

    report_results = []
    for row in results:
        cleaned = copy.deepcopy(row)
        if isinstance(cleaned, dict) and isinstance(cleaned.get("result"), dict):
            cleaned["result"].pop("base64", None)
        report_results.append(cleaned)

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for item in items:
            model = normalize_model(item.get("model"))
            base64_data = str(item.get("base64", "")).strip()
            if not model or not base64_data:
                continue
            file_name = unique_name(used_names, model, ".jpg")
            bundle.writestr(file_name, base64.b64decode(base64_data))

        if results:
            bundle.writestr(
                "results.json",
                json.dumps(
                    {"bundle_name": bundle_name, "generated_at": timestamp, "results": report_results},
                    ensure_ascii=False,
                    indent=2,
                ),
            )

    archive.seek(0)
    return archive.getvalue()


@app.get("/")
def root():
    return {
        "service": "energylabel-render-api",
        "status": "ok",
        "endpoints": ["/healthz", "/api/download", "/api/download-stream", "/api/download-zip"],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/download")
async def download(request: Request):
    body = await request.json()
    models = [normalize_model(item) for item in body.get("models", []) if normalize_model(item)]
    if not models:
        raise HTTPException(400, "未提供型號")

    batch = await run_batch(models)
    return JSONResponse(batch)


@app.post("/api/download-stream")
async def download_stream(request: Request):
    body = await request.json()
    models = [normalize_model(item) for item in body.get("models", []) if normalize_model(item)]
    if not models:
        raise HTTPException(400, "未提供型號")

    async def event_generator():
        encoder_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def handle_event(payload: dict):
            await encoder_queue.put(json.dumps(payload, ensure_ascii=False))

        async def worker():
            try:
                batch = await run_batch(models, event_callback=handle_event)
                await encoder_queue.put(json.dumps({"type": "summary", **batch["meta"]}, ensure_ascii=False))
            finally:
                await encoder_queue.put(None)

        task = asyncio.create_task(worker())
        try:
            while True:
                payload = await encoder_queue.get()
                if payload is None:
                    break
                yield f"data: {payload}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            await task

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/download-zip")
async def download_zip(request: Request):
    body = await request.json()
    items = body.get("items", [])
    results = body.get("results", [])
    bundle_name = normalize_model(body.get("bundle_name")) or "energy-labels"

    valid_items = [
        {"model": item.get("model"), "base64": item.get("base64")}
        for item in items
        if normalize_model(item.get("model")) and str(item.get("base64", "")).strip()
    ]
    if not valid_items:
        raise HTTPException(400, "沒有可打包的成功圖檔")

    payload = build_zip_bundle(valid_items, results, bundle_name)
    safe_bundle_name = re.sub(r'[^A-Za-z0-9._-]+', "-", bundle_name).strip("-") or "energy-labels"
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_bundle_name}.zip"',
    }
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers=headers,
    )

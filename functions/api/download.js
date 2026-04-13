const TARGET = 'https://ranking.energylabel.org.tw/product/Approval';

const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
  'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
  'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8',
  'Referer': `${TARGET}/list.aspx`,
};
const MAX_ATTEMPTS = 5;
const BACKOFF_BASE_MS = 1200;
const RETRYABLE_HTTP_CODES = new Set([408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524]);

function escapeRegExp(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function stripTags(html) {
  return html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

function extractRows(html) {
  const rows = [];
  const rowMatches = html.match(/<tr[\s\S]*?<\/tr>/gi) || [];
  for (const row of rowMatches) {
    const hrefMatch = row.match(/href=["']([^"']*upt\.aspx\?[^"']*id=\d+[^"']*)["']/i);
    if (!hrefMatch) continue;
    const text = stripTags(row);
    rows.push({ href: hrefMatch[1], text });
  }
  return rows;
}

function extractLinks(html) {
  const links = [];
  const re = /href=["']([^"']*upt\.aspx\?[^"']*id=\d+[^"']*)["']/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    links.push(m[1]);
  }
  return links;
}

function pickBestLink(rows, links, model) {
  if (!links || links.length === 0) return null;
  const modelNorm = (model || '').replace(/\s+/g, '').toUpperCase();
  if (!modelNorm) return links[0];

  const scored = [];
  for (const row of rows) {
    const rowText = (row.text || '').toUpperCase();
    let score = 0;
    if (rowText.includes(modelNorm)) score += 10;
    const tokenRe = new RegExp(`(^|[^A-Z0-9])${escapeRegExp(modelNorm)}(?![A-Z0-9])`);
    if (tokenRe.test(rowText)) score += 5;
    if (score) scored.push({ score, href: row.href });
  }
  if (scored.length) {
    scored.sort((a, b) => b.score - a.score);
    return scored[0].href;
  }

  if (links.length === 1) return links[0];
  return null;
}

function parseHiddenFields(html) {
  const fields = {};
  const inputs = html.match(/<input[^>]*type=["']hidden["'][^>]*>/gi) || [];
  for (const input of inputs) {
    const nameMatch = input.match(/name=["']([^"']+)["']/i);
    if (!nameMatch) continue;
    const valueMatch = input.match(/value=["']([^"']*)["']/i);
    fields[nameMatch[1]] = valueMatch ? valueMatch[1] : '';
  }
  return fields;
}

function normalizeFetchError(err) {
  const msg = String(err?.message || err || '');
  const retryableMatch = msg.match(/^RETRYABLE_HTTP_(\d{3})$/);
  if (retryableMatch) {
    return `官網連線逾時或忙碌中 (HTTP ${retryableMatch[1]})`;
  }
  if (msg.toLowerCase().includes('fetch failed')) {
    return '官網連線失敗（可能暫時無法連線）';
  }
  return msg || '官網連線失敗';
}

function httpStatusToError(status) {
  if (RETRYABLE_HTTP_CODES.has(status)) {
    return `官網連線逾時或忙碌中 (HTTP ${status})`;
  }
  if (status === 403 || status === 401) {
    return `官網拒絕存取 (HTTP ${status})`;
  }
  return `官網回應異常 (HTTP ${status})`;
}

async function aspnetPostSearch(model) {
  const url = `${TARGET}/list.aspx`;
  let page;
  try {
    page = await fetch(url, { headers: HEADERS, redirect: 'follow' });
  } catch (e) {
    return { html: null, error: normalizeFetchError(e) };
  }
  if (!page.ok) {
    return { html: null, error: httpStatusToError(page.status) };
  }
  const html = await page.text();
  const form = parseHiddenFields(html);

  form['ctl00$CPage$key2'] = model;
  form['ctl00$CPage$key'] = '';
  form['ctl00$CPage$Type'] = '';
  form['ctl00$CPage$RANK'] = '';
  form['ctl00$CPage$comp'] = '0';
  form['ctl00$CPage$approvedateA'] = '';
  form['ctl00$CPage$approvedateB'] = '';
  form['ctl00$CPage$condiA'] = '';
  form['ctl00$CPage$condiB'] = '';
  form['ctl00$CPage$btnSearch'] = '查  詢';

  const body = new URLSearchParams(form);
  let resp;
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: { ...HEADERS, 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
      redirect: 'follow',
    });
  } catch (e) {
    return { html: null, error: normalizeFetchError(e) };
  }
  if (!resp.ok) {
    return { html: null, error: httpStatusToError(resp.status) };
  }
  return { html: await resp.text(), error: null };
}

async function getSearchFallback(model) {
  const combos = [
    `key2=${encodeURIComponent(model)}`,
    `key2=${encodeURIComponent(model)}&Type=&RANK=&con=`,
    `key2=${encodeURIComponent(model)}&Type=0&RANK=0&con=0`,
  ];
  let lastError = null;
  for (const params of combos) {
    try {
      const r = await fetch(`${TARGET}/list.aspx?${params}`, { headers: HEADERS, redirect: 'follow' });
      if (!r.ok) {
        lastError = httpStatusToError(r.status);
        continue;
      }
      const html = await r.text();
      const rows = extractRows(html);
      const links = extractLinks(html);
      if (links.length) return { html, rows, links, error: null };
    } catch (e) {
      lastError = normalizeFetchError(e);
    }
  }
  return { html: null, rows: [], links: [], error: lastError };
}

async function fetchImageOnce(model) {
  try {
    const { rows: fallbackRows, links: fallbackLinks, error: fallbackError } = await getSearchFallback(model);
    let picked = pickBestLink(fallbackRows, fallbackLinks, model);

    if (!picked) {
      const postResult = await aspnetPostSearch(model);
      if (postResult?.html) {
        const rows = extractRows(postResult.html);
        const links = extractLinks(postResult.html);
        picked = pickBestLink(rows, links, model);
      } else if (postResult?.error) {
        return { status: 'error', message: postResult.error };
      } else if (fallbackError) {
        return { status: 'error', message: fallbackError };
      }
    }

    if (!picked) return { status: 'error', message: '找不到此型號（官網搜尋無結果）' };

    const p0 = picked.match(/p0=(\d+)/i);
    const id = picked.match(/id=(\d+)/i);
    if (!p0 || !id) return { status: 'error', message: '無法解析產品連結' };

    const imgUrl = `${TARGET}/ImgViewer.ashx?applyID=${id[1]}&goodID=${p0[1]}`;
    const imgResp = await fetch(imgUrl, { headers: HEADERS, redirect: 'follow' });
    if (!imgResp.ok) {
      if (RETRYABLE_HTTP_CODES.has(imgResp.status)) {
        return { status: 'error', message: `官網連線逾時或忙碌中 (HTTP ${imgResp.status})` };
      }
      return { status: 'error', message: `HTTP ${imgResp.status}` };
    }
    const imgHtml = await imgResp.text();

    const srcMatch = imgHtml.match(/src=["']data:image\/(?:jpeg|jpg);base64,([^"']+)["']/i);
    if (!srcMatch || !srcMatch[1] || srcMatch[1].length < 100) {
      return { status: 'error', message: '無法取得圖檔（頁面未回傳圖片）' };
    }

    return { status: 'ok', base64: srcMatch[1].trim() };
  } catch (e) {
    return { status: 'error', message: normalizeFetchError(e) };
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function shouldRetry(result) {
  if (!result || result.status !== 'error') return false;
  const msg = String(result.message || '');
  if (msg.includes('找不到此型號')) return false;
  if (msg.includes('無法解析產品連結')) return false;
  return true;
}

function retryBackoffMs(retryIndex) {
  return BACKOFF_BASE_MS * (2 ** retryIndex);
}

async function fetchImageWithRetry(model) {
  let result = await fetchImageOnce(model);
  let attempts = 1;

  while (attempts < MAX_ATTEMPTS && shouldRetry(result)) {
    const retryIndex = attempts - 1;
    await sleep(retryBackoffMs(retryIndex));
    attempts += 1;
    result = await fetchImageOnce(model);
  }

  if (attempts > 1 && result.status === 'error') {
    return { ...result, message: `${result.message}（已重試 ${attempts - 1} 次）` };
  }

  if (attempts > 1 && result.status === 'ok') {
    return { ...result, retries: attempts - 1 };
  }

  return result;
}

export async function onRequest(context) {
  if (context.request.method !== 'POST') {
    return new Response('Method Not Allowed', { status: 405 });
  }

  let body;
  try {
    body = await context.request.json();
  } catch {
    return new Response('Invalid JSON', { status: 400 });
  }

  const models = Array.isArray(body.models) ? body.models.map(m => String(m || '').trim()).filter(Boolean) : [];
  if (!models.length) return new Response('未提供型號', { status: 400 });

  const results = [];
  for (const model of models) {
    const result = await fetchImageWithRetry(model);
    results.push({ model, result });
    await sleep(800);
  }

  return new Response(JSON.stringify({ results }, null, 2), {
    headers: { 'Content-Type': 'application/json; charset=utf-8' }
  });
}

#!/usr/bin/env python3
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Union
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "trend_hunter.db"

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

SCAN_LOCK = threading.Lock()
IS_SCANNING = False
LAST_AUTO_SCAN_AT = 0.0
TRENDS_CACHE: dict = {
    "ts": 0.0,
    "sig": "",
    "data": {"generated_at": "", "items": []},
}
RELATED_CACHE: dict = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            link TEXT UNIQUE NOT NULL,
            source TEXT,
            published_at TEXT,
            keyword TEXT NOT NULL,
            trend_score INTEGER NOT NULL DEFAULT 0,
            trend_signal INTEGER NOT NULL DEFAULT 0,
            is_new INTEGER NOT NULL DEFAULT 1,
            saved INTEGER NOT NULL DEFAULT 0,
            discovered_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            new_articles INTEGER NOT NULL DEFAULT 0,
            total_articles INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 1,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    defaults = {
        "auto_scan": "0",
        "interval_minutes": "10",
        "last_scan_time": "",
    }
    for key, value in defaults.items():
        cur.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, value),
        )

    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def parse_pub_date(text: Optional[str]) -> datetime:
    if not text:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def fetch_xml(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=12) as resp:
        return resp.read()


def fetch_google_news(keyword: str, max_items: int = 30) -> list[dict]:
    query = quote_plus(f"{keyword} when:1d")
    url = f"https://news.google.com/rss/search?q={query}&hl=tr&gl=TR&ceid=TR:tr"
    raw = fetch_xml(url)
    root = ET.fromstring(raw)

    items: list[dict] = []
    for item in root.findall("./channel/item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = parse_pub_date(item.findtext("pubDate")).isoformat()
        source = (item.findtext("source") or "Google News").strip()

        if not title or not link:
            continue

        items.append(
            {
                "title": title,
                "link": link,
                "source": source,
                "published_at": pub_date,
                "keyword": keyword,
            }
        )
    return items


def fetch_google_trends(max_items: int = 40) -> list[str]:
    url = "https://trends.google.com/trending/rss?geo=TR"
    raw = fetch_xml(url)
    root = ET.fromstring(raw)
    trends: list[str] = []
    for item in root.findall("./channel/item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        if title:
            trends.append(normalize_text(title))
    return trends


def parse_trends_json(raw: bytes) -> dict:
    text = raw.decode("utf-8", errors="ignore")
    # Google Trends API prefix
    text = re.sub(r"^\)\]\}',?\s*", "", text)
    return json.loads(text)


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def fetch_last_hour_interest_for_batch(keywords: list[str], geo: str = "TR", timeframe: str = "now 1-H") -> dict[str, list[int]]:
    if not keywords:
        return {}

    comparison = [
        {"keyword": kw, "geo": geo, "time": timeframe}
        for kw in keywords
    ]
    req_payload = {"comparisonItem": comparison, "category": 0, "property": ""}
    params = urlencode(
        {
            "hl": "tr-TR",
            "tz": "-180",
            "req": json.dumps(req_payload, ensure_ascii=False, separators=(",", ":")),
        }
    )
    explore_url = f"https://trends.google.com/trends/api/explore?{params}"
    explore_raw = fetch_xml(explore_url)
    explore = parse_trends_json(explore_raw)

    widgets = explore.get("widgets", [])
    timeseries_widget = None
    for w in widgets:
        if w.get("id") == "TIMESERIES":
            timeseries_widget = w
            break
    if not timeseries_widget:
        return {}

    multi_req = timeseries_widget.get("request", {})
    multi_token = timeseries_widget.get("token", "")
    if not multi_token:
        return {}

    multi_params = urlencode(
        {
            "hl": "tr-TR",
            "tz": "-180",
            "req": json.dumps(multi_req, ensure_ascii=False, separators=(",", ":")),
            "token": multi_token,
        }
    )
    multi_url = f"https://trends.google.com/trends/api/widgetdata/multiline?{multi_params}"
    multi_raw = fetch_xml(multi_url)
    multi = parse_trends_json(multi_raw)

    timeline = multi.get("default", {}).get("timelineData", [])
    values_by_keyword: dict[str, list[int]] = {kw: [] for kw in keywords}
    for row in timeline:
        vals = row.get("value", [])
        for idx, kw in enumerate(keywords):
            v = 0
            if idx < len(vals):
                try:
                    v = int(vals[idx])
                except Exception:
                    v = 0
            values_by_keyword[kw].append(v)
    return values_by_keyword


def trends_explore_widgets_for_keywords(
    keywords: list[str], geo: str = "TR", timeframe: str = "now 1-H"
) -> list[dict]:
    if not keywords:
        return []
    comparison = [
        {"keyword": kw, "geo": geo, "time": timeframe}
        for kw in keywords
    ]
    req_payload = {"comparisonItem": comparison, "category": 0, "property": ""}
    params = urlencode(
        {
            "hl": "tr-TR",
            "tz": "-180",
            "req": json.dumps(req_payload, ensure_ascii=False, separators=(",", ":")),
        }
    )
    explore_url = f"https://trends.google.com/trends/api/explore?{params}"
    explore_raw = fetch_xml(explore_url)
    explore = parse_trends_json(explore_raw)
    return explore.get("widgets", [])


def fetch_related_queries_for_keyword(
    keyword: str, geo: str = "TR", timeframe: str = "now 1-H"
) -> dict:
    widgets = trends_explore_widgets_for_keywords([keyword], geo=geo, timeframe=timeframe)
    related_widget = None
    for w in widgets:
        if w.get("id") == "RELATED_QUERIES":
            related_widget = w
            break

    if not related_widget:
        return {"keyword": keyword, "top": [], "rising": [], "generated_at": now_iso()}

    req_payload = related_widget.get("request", {})
    token = related_widget.get("token", "")
    if not token:
        return {"keyword": keyword, "top": [], "rising": [], "generated_at": now_iso()}

    params = urlencode(
        {
            "hl": "tr-TR",
            "tz": "-180",
            "req": json.dumps(req_payload, ensure_ascii=False, separators=(",", ":")),
            "token": token,
        }
    )
    url = f"https://trends.google.com/trends/api/widgetdata/relatedsearches?{params}"
    raw = fetch_xml(url)
    data = parse_trends_json(raw)

    ranked = data.get("default", {}).get("rankedList", [])
    top_raw = ranked[0].get("rankedKeyword", []) if len(ranked) > 0 else []
    rising_raw = ranked[1].get("rankedKeyword", []) if len(ranked) > 1 else []

    def normalize_items(items: list[dict]) -> list[dict]:
        out = []
        for it in items:
            q = (it.get("query") or "").strip()
            if not q:
                continue
            out.append(
                {
                    "query": q,
                    "value": it.get("value", 0),
                    "formatted_value": (it.get("formattedValue") or "").strip(),
                    "link": it.get("link", ""),
                }
            )
        return out

    return {
        "keyword": keyword,
        "generated_at": now_iso(),
        "top": normalize_items(top_raw),
        "rising": normalize_items(rising_raw),
    }


def cached_related_queries_for_keyword(
    keyword: str, geo: str = "TR", timeframe: str = "now 1-H", ttl_seconds: int = 300, force_refresh: bool = False
) -> dict:
    k = f"{normalize_text(keyword)}|{geo}|{timeframe}"
    now_ts = time.time()
    item = RELATED_CACHE.get(k)
    if not force_refresh and item and now_ts - item.get("ts", 0.0) < ttl_seconds:
        return item.get("data", {})

    data = fetch_related_queries_for_keyword(keyword=keyword, geo=geo, timeframe=timeframe)
    RELATED_CACHE[k] = {"ts": now_ts, "data": data}
    return data


def build_discover_queries(
    keywords: list[str], timeframe: str, mode: str = "rising", force_refresh: bool = False
) -> dict:
    if not keywords:
        return {
            "generated_at": now_iso(),
            "timeframe": timeframe,
            "mode": mode,
            "source_keywords": [],
            "items": [],
        }

    rows = []
    for kw in keywords:
        try:
            rel = cached_related_queries_for_keyword(
                keyword=kw,
                geo="TR",
                timeframe=timeframe,
                force_refresh=force_refresh,
            )
            rows.append(rel)
        except Exception:
            rows.append({"keyword": kw, "top": [], "rising": []})

    pick_mode = "top" if mode == "top" else "rising"
    merged: dict[str, dict] = {}
    for rel in rows:
        source_kw = rel.get("keyword", "")
        items = rel.get(pick_mode, []) or []
        for item in items:
            q = (item.get("query") or "").strip()
            if not q:
                continue
            key = normalize_text(q)
            score = item.get("value", 0) or 0
            entry = merged.get(key)
            if not entry:
                entry = {
                    "query": q,
                    "value": score,
                    "formatted_value": item.get("formatted_value", ""),
                    "from_keywords": [],
                }
                merged[key] = entry
            entry["value"] = max(entry.get("value", 0), score)
            if item.get("formatted_value"):
                entry["formatted_value"] = item.get("formatted_value")
            if source_kw and source_kw not in entry["from_keywords"]:
                entry["from_keywords"].append(source_kw)

    items = list(merged.values())
    items.sort(
        key=lambda x: (
            1 if str(x.get("formatted_value", "")).lower() in ("breakout", "hizli artis", "hızlı artış") else 0,
            int(x.get("value", 0) or 0),
            len(x.get("from_keywords", [])),
        ),
        reverse=True,
    )

    return {
        "generated_at": now_iso(),
        "timeframe": timeframe,
        "mode": pick_mode,
        "source_keywords": keywords,
        "items": items,
    }


def build_last_hour_trends(keywords: list[str]) -> dict:
    if not keywords:
        return {"generated_at": now_iso(), "items": []}

    collected: dict[str, list[int]] = {}
    for batch in chunked(keywords, 8):
        try:
            batch_vals = fetch_last_hour_interest_for_batch(batch, geo="TR", timeframe="now 1-H")
            collected.update(batch_vals)
        except Exception:
            for kw in batch:
                collected.setdefault(kw, [])

    items = []
    for kw in keywords:
        series = collected.get(kw, [])
        if not series:
            item = {
                "keyword": kw,
                "latest_index": 0,
                "avg_60m": 0.0,
                "avg_20m": 0.0,
                "delta_20m": 0.0,
                "points": [],
            }
            items.append(item)
            continue

        latest = int(series[-1])
        avg_60 = sum(series) / len(series)
        tail_20 = series[-20:] if len(series) >= 20 else series
        head_20 = series[:20] if len(series) >= 20 else series
        avg_20 = sum(tail_20) / len(tail_20)
        prev_20 = sum(head_20) / len(head_20)
        delta_20 = avg_20 - prev_20

        item = {
            "keyword": kw,
            "latest_index": latest,
            "avg_60m": round(avg_60, 2),
            "avg_20m": round(avg_20, 2),
            "delta_20m": round(delta_20, 2),
            "points": series,
        }
        items.append(item)

    items.sort(key=lambda x: (x["latest_index"], x["avg_20m"]), reverse=True)
    return {"generated_at": now_iso(), "items": items}


def cached_last_hour_trends(keywords: list[str], ttl_seconds: int = 240, force_refresh: bool = False) -> dict:
    sig = "|".join(sorted(normalize_text(k) for k in keywords))
    now_ts = time.time()
    if not force_refresh and TRENDS_CACHE["sig"] == sig and now_ts - TRENDS_CACHE["ts"] < ttl_seconds:
        return TRENDS_CACHE["data"]

    data = build_last_hour_trends(keywords)
    TRENDS_CACHE["ts"] = now_ts
    TRENDS_CACHE["sig"] = sig
    TRENDS_CACHE["data"] = data
    return data


def calc_trend_score(title: str, keyword: str, published_at: str, trends: list[str], keyword_density: int) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    pub_dt = parse_pub_date(published_at)
    age_hours = max(0.0, (now - pub_dt).total_seconds() / 3600.0)

    title_norm = normalize_text(title)
    keyword_norm = normalize_text(keyword)

    score = 0
    signal = 0

    # Recency signal: first 6h is strongest.
    recency = max(0, int(45 - age_hours * 3.5))
    score += recency

    # Keyword relevance in title.
    if keyword_norm and keyword_norm in title_norm:
        score += 20

    # Trend match from Google Trends feed.
    trend_hit = 0
    for t in trends:
        if not t:
            continue
        if t in title_norm or any(part for part in t.split(" ") if len(part) > 4 and part in title_norm):
            trend_hit = 1
            break
    if trend_hit:
        score += 25
        signal = 1

    # Burst signal per keyword inside same scan.
    density_boost = min(20, keyword_density * 2)
    score += density_boost

    score = max(0, min(100, score))
    return score, signal


def scan_now() -> dict:
    global IS_SCANNING, LAST_AUTO_SCAN_AT

    if not SCAN_LOCK.acquire(blocking=False):
        return {"success": False, "error": "Tarama zaten devam ediyor."}

    IS_SCANNING = True
    scan_start = now_iso()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO scans(started_at) VALUES (?)", (scan_start,))
    scan_id = cur.lastrowid
    conn.commit()

    try:
        keywords_rows = conn.execute(
            "SELECT keyword FROM keywords ORDER BY created_at DESC"
        ).fetchall()
        keywords = [row["keyword"] for row in keywords_rows]

        if not keywords:
            cur.execute(
                "UPDATE scans SET finished_at=?, success=0, error=? WHERE id=?",
                (now_iso(), "Anahtar kelime yok", scan_id),
            )
            conn.commit()
            return {"success": False, "error": "Lutfen once anahtar kelime ekleyin."}

        trends = []
        try:
            trends = fetch_google_trends()
        except Exception:
            # Trends endpoint fails sometimes; keep scan alive.
            trends = []

        gathered: list[dict] = []
        keyword_density: dict[str, int] = {}

        for kw in keywords:
            try:
                items = fetch_google_news(kw, max_items=25)
            except Exception:
                items = []
            gathered.extend(items)
            keyword_density[kw] = len(items)

        new_articles = 0
        total_processed = 0

        for item in gathered:
            total_processed += 1
            score, signal = calc_trend_score(
                item["title"],
                item["keyword"],
                item["published_at"],
                trends,
                keyword_density.get(item["keyword"], 0),
            )

            discovered_at = now_iso()
            try:
                cur.execute(
                    """
                    INSERT INTO news(title, link, source, published_at, keyword, trend_score, trend_signal, is_new, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        item["title"],
                        item["link"],
                        item["source"],
                        item["published_at"],
                        item["keyword"],
                        score,
                        signal,
                        discovered_at,
                    ),
                )
                new_articles += 1
            except sqlite3.IntegrityError:
                # Existing link: refresh score to current value and keep record updated.
                cur.execute(
                    """
                    UPDATE news
                    SET trend_score = ?,
                        trend_signal = ?,
                        keyword = ?,
                        source = COALESCE(source, ?),
                        published_at = COALESCE(?, published_at)
                    WHERE link = ?
                    """,
                    (score, signal, item["keyword"], item["source"], item["published_at"], item["link"]),
                )

        cur.execute(
            "UPDATE scans SET finished_at=?, new_articles=?, total_articles=?, success=1 WHERE id=?",
            (now_iso(), new_articles, total_processed, scan_id),
        )
        conn.commit()

        set_setting("last_scan_time", now_iso())
        LAST_AUTO_SCAN_AT = time.time()

        total_news = conn.execute("SELECT COUNT(*) AS c FROM news").fetchone()["c"]

        return {
            "success": True,
            "newArticles": new_articles,
            "totalProcessed": total_processed,
            "totalArticles": total_news,
        }
    except Exception as exc:
        cur.execute(
            "UPDATE scans SET finished_at=?, success=0, error=? WHERE id=?",
            (now_iso(), str(exc), scan_id),
        )
        conn.commit()
        return {"success": False, "error": f"Tarama hatasi: {exc}"}
    finally:
        conn.close()
        IS_SCANNING = False
        SCAN_LOCK.release()


def read_status() -> dict:
    conn = get_conn()
    total_news = conn.execute("SELECT COUNT(*) AS c FROM news").fetchone()["c"]
    new_count = conn.execute("SELECT COUNT(*) AS c FROM news WHERE is_new = 1").fetchone()["c"]
    saved_count = conn.execute("SELECT COUNT(*) AS c FROM news WHERE saved = 1").fetchone()["c"]
    keyword_count = conn.execute("SELECT COUNT(*) AS c FROM keywords").fetchone()["c"]
    scan_count = conn.execute("SELECT COUNT(*) AS c FROM scans WHERE success = 1").fetchone()["c"]

    status = {
        "total_news": total_news,
        "new_count": new_count,
        "saved_count": saved_count,
        "keyword_count": keyword_count,
        "scan_count": scan_count,
        "last_scan_time": get_setting("last_scan_time", ""),
        "auto_scan": get_setting("auto_scan", "0") == "1",
        "interval_minutes": int(get_setting("interval_minutes", "10") or "10"),
        "is_scanning": IS_SCANNING,
    }
    conn.close()
    return status


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    body = handler.rfile.read(length)
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def json_response(handler: BaseHTTPRequestHandler, payload: Union[dict, list], status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_file(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.read()


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/"):
            self.handle_api_get(path, parsed)
            return

        if path == "/" or path == "/index.html":
            file_path = STATIC_DIR / "index.html"
            self.serve_file(file_path, "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            relative = path.replace("/static/", "", 1)
            file_path = STATIC_DIR / relative
            if file_path.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif file_path.suffix == ".js":
                ctype = "application/javascript; charset=utf-8"
            else:
                ctype = "application/octet-stream"
            self.serve_file(file_path, ctype)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        self.handle_api_post(path)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        self.handle_api_delete(path)

    def serve_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        payload = read_file(file_path)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_api_get(self, path: str, parsed) -> None:
        if path == "/api/status":
            json_response(self, read_status())
            return

        if path == "/api/keywords":
            conn = get_conn()
            rows = conn.execute(
                """
                SELECT k.id, k.keyword,
                       (SELECT COUNT(*) FROM news n WHERE n.keyword = k.keyword) AS count
                FROM keywords k
                ORDER BY k.created_at DESC
                """
            ).fetchall()
            conn.close()
            json_response(self, {"keywords": [dict(r) for r in rows]})
            return

        if path == "/api/trends/last-hour":
            query = parse_qs(parsed.query)
            force_refresh = (query.get("force", ["0"])[0] or "0") == "1"
            conn = get_conn()
            rows = conn.execute(
                "SELECT keyword FROM keywords ORDER BY created_at DESC"
            ).fetchall()
            conn.close()
            keywords = [r["keyword"] for r in rows]
            data = cached_last_hour_trends(keywords, force_refresh=force_refresh)
            json_response(self, data)
            return

        if path == "/api/trends/related":
            query = parse_qs(parsed.query)
            keyword = (query.get("keyword", [""])[0] or "").strip()
            force_refresh = (query.get("force", ["0"])[0] or "0") == "1"

            if not keyword:
                conn = get_conn()
                row = conn.execute(
                    "SELECT keyword FROM keywords ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                conn.close()
                keyword = row["keyword"] if row else ""

            if not keyword:
                json_response(self, {"keyword": "", "generated_at": now_iso(), "top": [], "rising": []})
                return

            try:
                data = cached_related_queries_for_keyword(
                    keyword=keyword,
                    geo="TR",
                    timeframe="now 1-H",
                    force_refresh=force_refresh,
                )
                json_response(self, data)
            except Exception as exc:
                json_response(
                    self,
                    {
                        "keyword": keyword,
                        "generated_at": now_iso(),
                        "top": [],
                        "rising": [],
                        "error": str(exc),
                    },
                    200,
                )
            return

        if path == "/api/discover":
            query = parse_qs(parsed.query)
            timeframe_key = (query.get("timeframe", ["1h"])[0] or "1h").strip().lower()
            mode = (query.get("mode", ["rising"])[0] or "rising").strip().lower()
            force_refresh = (query.get("force", ["0"])[0] or "0") == "1"
            selected_keyword = (query.get("keyword", [""])[0] or "").strip()

            timeframe = "now 4-H" if timeframe_key == "4h" else "now 1-H"
            mode = "top" if mode == "top" else "rising"

            if selected_keyword:
                keywords = [selected_keyword]
            else:
                conn = get_conn()
                rows = conn.execute(
                    "SELECT keyword FROM keywords ORDER BY created_at DESC LIMIT 30"
                ).fetchall()
                conn.close()
                keywords = [r["keyword"] for r in rows]

            data = build_discover_queries(
                keywords=keywords,
                timeframe=timeframe,
                mode=mode,
                force_refresh=force_refresh,
            )
            json_response(self, data)
            return

        if path == "/api/news":
            query = parse_qs(parsed.query)
            flt = (query.get("filter", ["all"])[0] or "all").lower()
            keyword = (query.get("keyword", [""])[0] or "").strip()
            limit = int((query.get("limit", ["120"])[0] or "120"))
            limit = max(1, min(limit, 500))

            where = []
            args: list = []

            if flt == "new":
                where.append("is_new = 1")
            elif flt == "saved":
                where.append("saved = 1")

            if keyword:
                where.append("keyword = ?")
                args.append(keyword)

            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            sql = (
                "SELECT id, title, link, source, keyword, trend_score, trend_signal, "
                "published_at, discovered_at, is_new, saved "
                f"FROM news {where_sql} "
                "ORDER BY datetime(published_at) DESC, trend_score DESC, datetime(discovered_at) DESC LIMIT ?"
            )
            args.append(limit)

            conn = get_conn()
            rows = conn.execute(sql, tuple(args)).fetchall()

            total_sql = f"SELECT COUNT(*) AS c FROM news {where_sql}"
            total = conn.execute(total_sql, tuple(args[:-1])).fetchone()["c"]

            conn.close()
            json_response(self, {"total": total, "news": [dict(r) for r in rows]})
            return

        if path == "/api/scans":
            conn = get_conn()
            rows = conn.execute(
                "SELECT id, started_at, finished_at, new_articles, total_articles, success, error "
                "FROM scans ORDER BY id DESC LIMIT 30"
            ).fetchall()
            conn.close()
            json_response(self, {"scans": [dict(r) for r in rows]})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def handle_api_post(self, path: str) -> None:
        if path == "/api/scan":
            result = scan_now()
            code = 200 if result.get("success") else 409
            json_response(self, result, code)
            return

        if path == "/api/keywords":
            data = parse_json_body(self)
            keyword = (data.get("keyword") or "").strip()
            if not keyword:
                json_response(self, {"error": "keyword gerekli"}, 400)
                return

            conn = get_conn()
            try:
                conn.execute(
                    "INSERT INTO keywords(keyword, created_at) VALUES (?, ?)",
                    (keyword, now_iso()),
                )
                conn.commit()
                json_response(self, {"success": True})
            except sqlite3.IntegrityError:
                json_response(self, {"error": "Bu kelime zaten var"}, 409)
            finally:
                conn.close()
            return

        if path.startswith("/api/save/"):
            article_id = path.replace("/api/save/", "", 1).strip()
            if not article_id.isdigit():
                json_response(self, {"error": "gecersiz id"}, 400)
                return

            conn = get_conn()
            row = conn.execute("SELECT saved FROM news WHERE id = ?", (article_id,)).fetchone()
            if not row:
                conn.close()
                json_response(self, {"error": "kayit bulunamadi"}, 404)
                return

            new_saved = 0 if row["saved"] == 1 else 1
            conn.execute("UPDATE news SET saved = ? WHERE id = ?", (new_saved, article_id))
            conn.commit()
            conn.close()
            json_response(self, {"success": True, "saved": bool(new_saved)})
            return

        if path == "/api/mark-seen":
            conn = get_conn()
            conn.execute("UPDATE news SET is_new = 0 WHERE is_new = 1")
            conn.commit()
            conn.close()
            json_response(self, {"success": True})
            return

        if path == "/api/settings":
            data = parse_json_body(self)
            auto_scan = data.get("auto_scan")
            interval = data.get("interval_minutes")

            if auto_scan is not None:
                set_setting("auto_scan", "1" if bool(auto_scan) else "0")

            if interval is not None:
                try:
                    interval_val = int(interval)
                except Exception:
                    json_response(self, {"error": "interval_minutes sayi olmali"}, 400)
                    return
                interval_val = max(2, min(interval_val, 180))
                set_setting("interval_minutes", str(interval_val))

            json_response(self, {"success": True})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def handle_api_delete(self, path: str) -> None:
        if path.startswith("/api/keywords/"):
            keyword = path.replace("/api/keywords/", "", 1).strip()
            if not keyword:
                json_response(self, {"error": "keyword gerekli"}, 400)
                return

            conn = get_conn()
            conn.execute("DELETE FROM keywords WHERE keyword = ?", (keyword,))
            conn.commit()
            conn.close()
            json_response(self, {"success": True})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def log_message(self, fmt: str, *args) -> None:
        # Keep output concise.
        return


def auto_scan_worker() -> None:
    global LAST_AUTO_SCAN_AT

    while True:
        try:
            auto_on = get_setting("auto_scan", "0") == "1"
            interval = int(get_setting("interval_minutes", "10") or "10")
            interval = max(2, min(interval, 180))

            if auto_on:
                now = time.time()
                elapsed = now - LAST_AUTO_SCAN_AT
                if LAST_AUTO_SCAN_AT == 0.0 or elapsed >= interval * 60:
                    scan_now()
            time.sleep(8)
        except Exception:
            time.sleep(8)


def main() -> None:
    init_db()

    thread = threading.Thread(target=auto_scan_worker, daemon=True)
    thread.start()

    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Trend Hunter Pro calisiyor: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

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
from urllib.parse import parse_qs, quote_plus, urlparse
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
                # Existing link: refresh score and keyword.
                cur.execute(
                    """
                    UPDATE news
                    SET trend_score = MAX(trend_score, ?),
                        trend_signal = MAX(trend_signal, ?),
                        keyword = ?,
                        source = COALESCE(source, ?)
                    WHERE link = ?
                    """,
                    (score, signal, item["keyword"], item["source"], item["link"]),
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
                "ORDER BY trend_score DESC, datetime(discovered_at) DESC LIMIT ?"
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

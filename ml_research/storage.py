"""SQLite 数据层 — 统一落库,分析层只读库不重复请求外部。
表:
  blue_whale_products  蓝鲸选品(含 EAN/UPC/品牌型号强标识)
  ml_listings          美客多竞品 listing(唯一键 item_id)
  listing_snapshots    每日快照(日销估算 = 相邻快照 sold_qty 差值)
  visits_snapshots     访客快照(仅自己店铺商品,来自 /visits/items)
  product_matches      选品 ↔ 竞品匹配结果
  own_items            自己店铺商品
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Optional


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS blue_whale_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT,
    source_platform TEXT,
    source_url TEXT,
    price_cny REAL,
    sales_est INTEGER,
    ean TEXT,
    brand TEXT,
    model TEXT,
    raw_json TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ml_listings (
    item_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category_id TEXT,
    price REAL,
    currency_id TEXT,
    available_qty INTEGER,
    sold_qty INTEGER,
    seller_id TEXT,
    seller_nickname TEXT,
    seller_reputation TEXT,
    condition TEXT,
    permalink TEXT,
    thumbnail TEXT,
    date_created TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listing_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    sold_qty INTEGER,
    price REAL,
    available_qty INTEGER,
    snapshot_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_item ON listing_snapshots(item_id, snapshot_at);

CREATE TABLE IF NOT EXISTS visits_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    visit_date TEXT NOT NULL,
    visits INTEGER NOT NULL,
    UNIQUE(item_id, visit_date)
);

CREATE TABLE IF NOT EXISTS product_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blue_whale_product_id INTEGER NOT NULL,
    ml_listing_id TEXT NOT NULL,
    match_score REAL,
    match_method TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(blue_whale_product_id, ml_listing_id)
);

CREATE TABLE IF NOT EXISTS own_items (
    item_id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT,
    price REAL,
    available_qty INTEGER,
    sold_qty INTEGER,
    category_id TEXT,
    visits_90d INTEGER,
    last_sync_at TEXT NOT NULL
);
"""


class ResearchDb:
    """ml_research 数据库访问。db_path 支持 ":memory:"(自测用)。"""

    def __init__(self, db_path=None):
        if db_path is None:
            from . import config
            db_path = config.DB_PATH
        self.db_path = db_path
        # ":memory:" 模式下每次 connect() 都是全新空库,必须持有一条持久连接
        self._memory_conn = sqlite3.connect(":memory:") if db_path == ":memory:" else None

    def _connect(self):
        if self._memory_conn is not None:
            self._memory_conn.row_factory = sqlite3.Row
            return self._memory_conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ---- DDL ----
    def init_db(self):
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def table_names(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return [r["name"] for r in rows]

    def table_count(self, table: str) -> int:
        with self._connect() as conn:
            row = conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()
        return row["n"]

    # ---- 蓝鲸选品 ----
    def upsert_blue_whale_product(self, p: dict) -> int:
        """按 source_url 幂等写入;返回行 id。"""
        raw_json = p.get("raw_json")
        if isinstance(raw_json, (dict, list)):
            raw_json = json.dumps(raw_json, ensure_ascii=False)
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO blue_whale_products
                    (name, category, source_platform, source_url, price_cny,
                     sales_est, ean, brand, model, raw_json, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (p.get("name"), p.get("category"), p.get("source_platform"),
                 p.get("source_url"), p.get("price_cny"), p.get("sales_est"),
                 p.get("ean"), p.get("brand"), p.get("model"), raw_json, now),
            )
            row = conn.execute(
                "SELECT id FROM blue_whale_products WHERE source_url=?",
                (p.get("source_url"),),
            ).fetchone()
            return row["id"] if row else 0

    # ---- 美客多竞品 listing ----
    def upsert_ml_listing(self, item: dict, seen_at: Optional[str] = None) -> None:
        """按 item_id 幂等写入(首次写 first_seen_at,之后只刷 last_seen_at 和最新值)。"""
        now = seen_at or _now_iso()
        seller = item.get("seller") or {}
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ml_listings
                    (item_id, title, category_id, price, currency_id, available_qty,
                     sold_qty, seller_id, seller_nickname, seller_reputation,
                     condition, permalink, thumbnail, date_created,
                     first_seen_at, last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET
                    title=excluded.title,
                    category_id=excluded.category_id,
                    price=excluded.price,
                    currency_id=excluded.currency_id,
                    available_qty=excluded.available_qty,
                    sold_qty=excluded.sold_qty,
                    seller_id=excluded.seller_id,
                    seller_nickname=excluded.seller_nickname,
                    seller_reputation=excluded.seller_reputation,
                    condition=excluded.condition,
                    permalink=excluded.permalink,
                    thumbnail=excluded.thumbnail,
                    date_created=excluded.date_created,
                    last_seen_at=excluded.last_seen_at
                """,
                (item.get("id"), item.get("title"), item.get("category_id"),
                 item.get("price"), item.get("currency_id"),
                 item.get("available_quantity"), item.get("sold_quantity"),
                 str(seller.get("id")) if seller.get("id") is not None else None,
                 seller.get("nickname"),
                 (item.get("seller_reputation") or ""),
                 item.get("condition"), item.get("permalink"),
                 item.get("thumbnail"), item.get("date_created"),
                 now, now),
            )

    def add_listing_snapshot(self, item_id: str, sold_qty, price, available_qty,
                             snapshot_at: Optional[str] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO listing_snapshots (item_id, sold_qty, price, available_qty, snapshot_at)
                VALUES (?,?,?,?,?)
                """,
                (item_id, sold_qty, price, available_qty, snapshot_at or _now_iso()),
            )

    # ---- 访客快照(自己店铺) ----
    def add_visits_snapshot(self, item_id: str, visit_date: str, visits: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO visits_snapshots (item_id, visit_date, visits)
                VALUES (?,?,?)
                ON CONFLICT(item_id, visit_date) DO UPDATE SET visits=excluded.visits
                """,
                (item_id, visit_date, visits),
            )

    # ---- 自己店铺商品 ----
    def upsert_own_item(self, item: dict, visits_90d: Optional[int] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO own_items
                    (item_id, title, status, price, available_qty, sold_qty,
                     category_id, visits_90d, last_sync_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET
                    title=excluded.title, status=excluded.status,
                    price=excluded.price, available_qty=excluded.available_qty,
                    sold_qty=excluded.sold_qty, category_id=excluded.category_id,
                    visits_90d=excluded.visits_90d, last_sync_at=excluded.last_sync_at
                """,
                (item.get("id"), item.get("title"), item.get("status"),
                 item.get("price"), item.get("available_quantity"),
                 item.get("sold_quantity"), item.get("category_id"),
                 visits_90d, _now_iso()),
            )

    # ---- 匹配结果 ----
    def upsert_match(self, blue_whale_product_id: int, ml_listing_id: str,
                     match_score: Optional[float] = None,
                     match_method: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO product_matches
                    (blue_whale_product_id, ml_listing_id, match_score, match_method, created_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(blue_whale_product_id, ml_listing_id) DO UPDATE SET
                    match_score=excluded.match_score, match_method=excluded.match_method
                """,
                (blue_whale_product_id, ml_listing_id, match_score, match_method, _now_iso()),
            )

    # ---- 查询 ----
    def recent_own_items(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM own_items ORDER BY last_sync_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_listings(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ml_listings ORDER BY last_seen_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

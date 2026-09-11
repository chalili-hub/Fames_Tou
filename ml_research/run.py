"""P1 管线 CLI。

用法:
    python -m ml_research.run --selftest             # 本地自测(无需网络/密钥)
    python -m ml_research.run --sync-own [--limit N] # 拉自己店铺商品+访客入库
    python -m ml_research.run --search "关键词"       # 站点搜索竞品入库
    python -m ml_research.run --import-csv 文件.csv   # 蓝鲸选品 CSV 兜底导入
"""
import argparse
import sys


def _db():
    from .storage import ResearchDb
    db = ResearchDb()
    db.init_db()
    return db


def cmd_selftest():
    from .storage import ResearchDb

    db = ResearchDb(":memory:")
    db.init_db()
    # 写读冒烟
    pid = db.upsert_blue_whale_product({
        "name": "测试商品", "category": "Test", "source_platform": "amazon",
        "source_url": "https://example.com/1", "price_cny": 12.5,
        "sales_est": 100, "ean": "7501234567890", "brand": "Acme", "model": "X1",
    })
    db.upsert_ml_listing({
        "id": "MLM1", "title": "Test Listing", "category_id": "MLM1051",
        "price": 199.9, "currency_id": "MXN", "available_quantity": 3,
        "sold_quantity": 42, "seller": {"id": 123, "nickname": "seller1"},
        "condition": "new", "permalink": "https://articulo.mercadolibre.com.mx/MLM-1",
        "date_created": "2026-01-01T00:00:00",
    })
    db.add_listing_snapshot("MLM1", sold_qty=42, price=199.9, available_qty=3)
    db.add_listing_snapshot("MLM1", sold_qty=45, price=199.9, available_qty=2)
    db.add_visits_snapshot("MLM1", "2026-08-18", 12)
    db.upsert_own_item({"id": "MLM1", "title": "Test Listing", "status": "active",
                        "price": 199.9, "available_quantity": 3,
                        "sold_quantity": 45, "category_id": "MLM1051"}, visits_90d=300)
    db.upsert_match(pid, "MLM1", match_score=0.95, match_method="ean")

    print("[selftest] 通过:表结构 + 读写正常")
    print(f"[selftest] 表: {db.table_names()}")
    print(f"[selftest] ml_listings={db.table_count('ml_listings')} "
          f"snapshots={db.table_count('listing_snapshots')} "
          f"own_items={db.table_count('own_items')} matches={db.table_count('product_matches')}")


def cmd_sync_own(args):
    from .ml_api import MlApi, TokenError

    db = _db()
    try:
        api = MlApi()
    except TokenError as e:
        print(f"[sync-own] {e}")
        sys.exit(1)

    me = api.get_me()
    user_id = me["id"]
    print(f"[sync-own] user_id={user_id} nickname={me.get('nickname')}")

    item_ids = api.get_my_item_ids(user_id)
    print(f"[sync-own] 在售商品 {len(item_ids)} 条")
    if args.limit:
        item_ids = item_ids[: args.limit]
    if not item_ids:
        print("[sync-own] 无商品,结束")
        return

    items = api.get_items(item_ids)
    visits = api.get_visits(item_ids)
    for it in items:
        db.upsert_own_item(it, visits_90d=visits.get(it.get("id")))
    print(f"[sync-own] 已入库 {len(items)} 条到 own_items")
    print(f"[sync-own] 总销量={sum(it.get('sold_quantity') or 0 for it in items)} "
          f"总访客90d={sum((visits.get(it.get('id')) or 0) for it in items)}")


def cmd_search(args):
    from .ml_api import MlApi
    from .storage import ResearchDb

    db = _db()
    api = MlApi()
    data = api.search_items(args.search, limit=args.limit or 50)
    results = data.get("results", []) or []
    total = (data.get("paging") or {}).get("total", 0)
    print(f"[search] '{args.search}' 共 {total} 条,本次入库 {len(results)} 条")

    for r in results:
        db.upsert_ml_listing(r)
        db.add_listing_snapshot(
            r.get("id"), sold_qty=r.get("sold_quantity"),
            price=r.get("price"), available_qty=r.get("available_quantity"),
        )

    print(f"\n{'item_id':<16}{'价格':>10}{'销量':>8} 标题")
    for r in results[:10]:
        print(f"{str(r.get('id')):<16}{str(r.get('price')):>10}"
              f"{str(r.get('sold_quantity')):>8}  {r.get('title','')[:50]}")


def cmd_import_csv(args):
    from .bluewhale_client import import_export_csv

    db = _db()
    products = import_export_csv(args.csv)
    print(f"[import-csv] 读入 {len(products)} 条")
    n = 0
    for p in products:
        if p.get("source_url"):
            db.upsert_blue_whale_product(p)
            n += 1
    print(f"[import-csv] 已入库 {n} 条到 blue_whale_products "
          f"(总数 {db.table_count('blue_whale_products')})")


def main():
    parser = argparse.ArgumentParser(description="ml_research P1 管线")
    parser.add_argument("--selftest", action="store_true", help="本地自测(无需密钥/网络)")
    parser.add_argument("--sync-own", action="store_true", help="同步自己店铺商品+访客")
    parser.add_argument("--search", metavar="关键词", help="站点搜索竞品并入库")
    parser.add_argument("--import-csv", dest="csv", metavar="文件", help="导入蓝鲸选品 CSV")
    parser.add_argument("--limit", type=int, default=0, help="数量上限")
    args = parser.parse_args()

    if args.selftest:
        cmd_selftest()
    elif args.sync_own:
        cmd_sync_own(args)
    elif args.search:
        cmd_search(args)
    elif args.csv:
        cmd_import_csv(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

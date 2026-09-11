"""蓝鲸选品客户端 — 骨架版,等 API 文档填充真实 endpoint。

已确定的输入:
  - 有账号 + API(文档/接口示例待你提供)
  - 导出字段含 EAN/UPC/品牌型号 强标识

本模块对外暴露统一接口,上层(匹配/分析)只认这个规范化 dict:
  {
    "name":           商品名,
    "category":       类目,
    "source_platform":"amazon"/"temu"/...,
    "source_url":     源平台链接(蓝鲸给的),
    "price_cny":      参考价(人民币),
    "sales_est":      销量估算,
    "ean":            EAN/UPC(强标识),
    "brand":          品牌,
    "model":          型号,
  }

P1 提供 CSV 导出兜底;import_export_csv 的列名映射可按你的导出模板调整。
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from . import config

# CSV 列名映射(按你的导出模板改这里即可)
CSV_COLUMNS = {
    "name": ("商品名称", "标题", "name", "title", "产品名称"),
    "category": ("类目", "分类", "category"),
    "source_platform": ("来源平台", "平台", "source_platform", "platform"),
    "source_url": ("链接", "商品链接", "url", "source_url", "商品url"),
    "price_cny": ("参考价", "价格", "price", "price_cny"),
    "sales_est": ("销量", "销量估算", "sales", "sales_est"),
    "ean": ("EAN", "UPC", "ean", "upc", "条码"),
    "brand": ("品牌", "brand"),
    "model": ("型号", "model", "款号"),
}


class BlueWhaleClient:
    """蓝鲸选品 API 客户端。

    TODO(等 API 文档):
      1. 在 __init__ 里按文档完成鉴权(api_key 放 header / query / 签名)
      2. 实现 _request() 复用 config.BLUEWHALE_API_BASE
      3. 把 fetch_products() 里的 NotImplementedError 换成真实调用,
         每条结果过 normalize_product() 转成统一 dict
    """

    def __init__(self, api_base: Optional[str] = None, api_key: Optional[str] = None):
        self.api_base = api_base or config.BLUEWHALE_API_BASE
        self.api_key = api_key or config.BLUEWHALE_API_KEY

    def fetch_products(self, category: Optional[str] = None,
                       days: int = 7, limit: int = 100) -> list[dict]:
        """拉取选品列表,返回规范化 dict 列表。

        category: 类目筛选;days: 时间窗(天);limit: 数量上限。
        """
        if not self.api_base:
            raise RuntimeError(
                "未配置 BLUEWHALE_API_BASE。若暂用 CSV 兜底,请用 "
                "bluewhale_client.import_export_csv(path)"
            )
        raise NotImplementedError(
            "等待蓝鲸选品 API 文档:填充真实 endpoint / 参数 / 鉴权后实现,"
            "每条结果调用 self.normalize_product(raw) 转统一格式"
        )

    def normalize_product(self, raw: dict) -> dict:
        """把蓝鲸原始字段映射成统一 dict(按 API 文档确认字段名后调整)。"""
        return {
            "name": raw.get("name") or raw.get("title") or "",
            "category": raw.get("category") or "",
            "source_platform": raw.get("platform") or raw.get("source") or "",
            "source_url": raw.get("url") or raw.get("link") or "",
            "price_cny": _to_float(raw.get("price") or raw.get("price_cny")),
            "sales_est": _to_int(raw.get("sales") or raw.get("sales_est")),
            "ean": str(raw.get("ean") or raw.get("upc") or raw.get("barcode") or "").strip(),
            "brand": raw.get("brand") or "",
            "model": raw.get("model") or raw.get("型号") or "",
        }


def import_export_csv(path: str | Path, column_map: Optional[dict] = None) -> list[dict]:
    """CSV 导出兜底:读蓝鲸导出的 CSV,按列名映射转统一 dict。

    column_map 覆盖 CSV_COLUMNS 的默认映射;每条还保留原始行(raw)供查。
    """
    columns = {**CSV_COLUMNS, **(column_map or {})}
    products = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            normalized = {}
            for key, aliases in columns.items():
                value = _first_present(row, aliases)
                if key == "price_cny":
                    normalized[key] = _to_float(value)
                elif key == "sales_est":
                    normalized[key] = _to_int(value)
                else:
                    normalized[key] = (value or "").strip()
            normalized["raw"] = dict(row)
            if normalized.get("name"):
                products.append(normalized)
    return products


def _first_present(row: dict, aliases: tuple) -> Optional[str]:
    for alias in aliases:
        value = row.get(alias)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("¥", "").replace("$", "").strip())
    except ValueError:
        return None


def _to_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except ValueError:
        return None

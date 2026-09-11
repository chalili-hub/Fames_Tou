"""美客多官方 API 客户端 — OAuth2 token 管理 + 常用端点封装(站点 MLM/墨西哥)。

合规主通道:自己店铺商品、搜索竞品、访客、卖家信誉全部走这里,**不需要代理/换 IP**。

OAuth2 要点:
  - access_token 有效期 6 小时,refresh_token 有效期 6 个月
  - 自动续期;6 个月未刷新需重新走 authorize.py 授权
  - token 落盘到 data/ml_tokens.json(gitignored)
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from typing import Optional

import requests

from . import config


class MlError(RuntimeError):
    """API 调用失败(网络/4xx/5xx)。"""


class TokenError(RuntimeError):
    """token 缺失/过期不可续期,需重新授权。"""


def _chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


class TokenManager:
    """OAuth2 token 生命周期:加载 / 保存 / 换码 / 续期。"""

    def __init__(self, token_file=None):
        self.token_file = token_file or config.TOKEN_FILE
        self._data: dict = {}
        self._lock = threading.Lock()
        self._data = self._load()

    # ---- 文件 ----
    def _load(self) -> dict:
        try:
            with open(self.token_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.token_file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    # ---- OAuth2 流程 ----
    def get_authorize_url(self) -> str:
        if not config.CLIENT_ID:
            raise TokenError("缺少 MLM_CLIENT_ID,先在 ml_research/.env 配置")
        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": config.CLIENT_ID,
            "redirect_uri": config.REDIRECT_URI,
        })
        return f"{config.AUTH_BASE}/authorization?{params}"

    def exchange_code(self, code: str) -> dict:
        """授权回调拿到 code 后换 token。"""
        if not config.CLIENT_SECRET:
            raise TokenError("缺少 MLM_CLIENT_SECRET")
        resp = requests.post(
            f"{config.API_BASE}/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": config.CLIENT_ID,
                "client_secret": config.CLIENT_SECRET,
                "code": code,
                "redirect_uri": config.REDIRECT_URI,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._store_token(data)
        return data

    def refresh(self) -> dict:
        """用 refresh_token 续期(access_token 过期或 401 时调用)。"""
        if not self._data.get("refresh_token"):
            raise TokenError("没有 refresh_token,请重新运行 python -m ml_research.authorize")
        resp = requests.post(
            f"{config.API_BASE}/oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_id": config.CLIENT_ID,
                "client_secret": config.CLIENT_SECRET,
                "refresh_token": self._data["refresh_token"],
            },
            timeout=30,
        )
        if resp.status_code == 400:
            raise TokenError(
                "refresh_token 已失效(可能超过 6 个月),请重新运行 "
                "python -m ml_research.authorize 授权"
            )
        resp.raise_for_status()
        data = resp.json()
        self._store_token(data)
        return data

    def _store_token(self, data: dict):
        expires_in = data.get("expires_in", 21600)
        with self._lock:
            self._data = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", self._data.get("refresh_token")),
                "expires_at": time.time() + expires_in,
                "user_id": data.get("user_id"),
                "scope": data.get("scope", ""),
                "updated_at": time.time(),
            }
            self._save()

    # ---- 读取 ----
    def access_token(self) -> str:
        with self._lock:
            expires_at = self._data.get("expires_at", 0)
        if time.time() >= expires_at - 60:  # 提前 60s 续期
            self.refresh()
        token = self._data.get("access_token")
        if not token:
            raise TokenError("没有 access_token,先运行 python -m ml_research.authorize")
        return token

    @property
    def user_id(self) -> Optional[str]:
        return self._data.get("user_id")


class MlApi:
    """美客多 API 封装:统一限速 / 鉴权 / 401 自动续期 / 429 退避重试。"""

    def __init__(self, token_manager: Optional[TokenManager] = None):
        self.tokens = token_manager or TokenManager()
        self._last_request_at = 0.0

    # ---- 底层请求 ----
    def _request(self, method: str, path: str, params: dict = None,
                 json_body: dict = None, retries: int = 1) -> dict:
        self._throttle()
        url = f"{config.API_BASE}{path}"
        headers = {"Authorization": f"Bearer {self.tokens.access_token()}"}

        for attempt in range(retries + 1):
            resp = requests.request(
                method, url, params=params, json=json_body, headers=headers, timeout=30,
            )
            if resp.status_code == 401 and attempt < retries:
                # token 可能刚过期:续期一次重试
                self.tokens.refresh()
                headers = {"Authorization": f"Bearer {self.tokens.access_token()}"}
                continue
            if resp.status_code == 429 and attempt < retries:
                wait = _retry_after_seconds(resp) or (2 ** attempt)
                print(f"[ml_api] 429 限流,等待 {wait}s 后重试", flush=True)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise MlError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        raise MlError(f"{method} {path} 重试耗尽")

    def _throttle(self):
        """全局最小请求间隔,避免触发限频。"""
        elapsed = time.time() - self._last_request_at
        if elapsed < config.REQUEST_MIN_INTERVAL_S:
            time.sleep(config.REQUEST_MIN_INTERVAL_S - elapsed)
        self._last_request_at = time.time()

    # ---- 端点封装 ----
    def get_me(self) -> dict:
        return self._request("GET", "/users/me")

    def search_items(self, q: str, limit: int = 50, offset: int = 0) -> dict:
        """站点内搜索(带 token,不限 IP)。返回原始响应:results / paging。"""
        return self._request(
            "GET", f"/sites/{config.SITE_ID}/search",
            params={"q": q, "limit": max(1, min(limit, 50)), "offset": max(0, offset)},
        )

    def get_item(self, item_id: str) -> dict:
        return self._request("GET", f"/items/{item_id}")

    def get_items(self, item_ids: list[str]) -> list[dict]:
        """批量取商品详情(每批最多 20 个 id)。"""
        out = []
        for chunk in _chunks(item_ids, 20):
            data = self._request("GET", "/items", params={"ids": ",".join(chunk)})
            for entry in data or []:
                if entry.get("code") == 200 and "body" in entry:
                    out.append(entry["body"])
        return out

    def get_item_health(self, item_id: str) -> dict:
        return self._request("GET", f"/items/{item_id}/health")

    def get_visits(self, item_ids: list[str]) -> dict:
        """90 天访客汇总,key=item_id,value=总访客数。仅自己店铺商品可用。"""
        out = {}
        for chunk in _chunks(item_ids, 10):
            data = self._request("GET", "/visits/items", params={"ids": ",".join(chunk)})
            for v in (data or {}).get("visits", []):
                item_id = v.get("item_id")
                total = sum(d.get("visits", 0) for d in v.get("visits", []))
                out[item_id] = total
        return out

    def get_seller_reputation(self, seller_id) -> dict:
        return self._request("GET", f"/reputations/{seller_id}/summary")

    def get_my_item_ids(self, user_id: str = None, status: str = "active",
                        limit: int = 50) -> list[str]:
        """自己店铺商品 id 列表(分页拉全,受 API 1000 条偏移上限约束)。"""
        user_id = user_id or self.tokens.user_id
        if not user_id:
            raise MlError("未知 user_id,先调 get_me() 或完成授权")
        ids: list[str] = []
        offset = 0
        while True:
            data = self._request(
                "GET", f"/users/{user_id}/items/search",
                params={"status": status, "limit": limit, "offset": offset},
            )
            results = (data or {}).get("results", []) or []
            ids.extend(results)
            total = ((data or {}).get("paging") or {}).get("total", 0)
            offset += limit
            if not results or offset >= total or offset > 1000:
                break
        return ids


def _retry_after_seconds(resp: requests.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None

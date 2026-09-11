# 美客多选品调研 / 跟卖分析 — 技术方案设计

> 目标:从蓝鲸选品(有账号+API)拿选品链接 → 在美客多(Mercado Libre)采集该商品的竞争情况(换 IP 部分仅限网页采集)→ 通过自己店铺已授权的官方 API 拉取自家数据 → 输出跟卖/定价建议。
>
> 版本:v1(方案设计) · 日期:以仓库提交为准

---

## 0. 核心认知纠偏(先讲清楚)

| 数据源 | 合规通道 | 是否需要换 IP |
|---|---|---|
| 蓝鲸选品 | 官方 API(你有账号+API) | 不需要,按平台限频即可 |
| 美客多**自己店铺**数据 | 美客多官方 API(OAuth2 已授权) | **不需要**,token 访问不限 IP |
| 美客多**竞品公开数据** | 官方 API 的搜索/商品端点优先;API 覆盖不到的部分(长评论、页面字段)才用网页采集 | 网页采集才需要代理轮换 |

**结论**:换 IP 是"竞品网页采集"这一段的局部手段,不是全流程的前提。能用官方 API 的绝不用网页采集——又快又合规又不用养代理。

---

## 1. 整体架构

```
┌──────────────┐   ┌───────────────────┐   ┌────────────────────┐
│ 蓝鲸选品 API  │   │ 美客多官方 API     │   │ 美客多网页/搜索采集   │
│ (账号+API key)│   │ (OAuth2 店铺授权)  │   │ (代理池轮换, 可选)   │
└──────┬───────┘   └─────────┬─────────┘   └─────────┬──────────┘
       │                     │                      │
       ▼                     ▼                      ▼
  选品采集器           店铺数据采集器            竞品采集器
  bluewhale_client     ml_api.py               ml_scraper.py
       └───────────────────┬─┴──────────────────────┘
                           ▼
                  ┌──────────────────┐
                  │  统一数据层        │
                  │  SQLite (storage) │
                  │  products/listings│
                  │  snapshots/matches│
                  └────────┬─────────┘
                           ▼
                  ┌──────────────────┐
                  │  匹配 & 分析引擎   │
                  │  matcher.py       │
                  │  analytics.py     │
                  └────────┬─────────┘
                           ▼
                  ┌──────────────────┐
                  │  决策输出          │
                  │  跟卖建议/定价/周报 │
                  │  report.py        │
                  └──────────────────┘
```

分层原则:

1. **采集层**只负责"拿数据",不关心业务;
2. **数据层**统一落库,所有分析只读库,不重复请求外部;
3. **分析层**纯计算,可离线反复跑;
4. 每个外部依赖(蓝鲸 API、美客多 API、代理池)都是独立模块,失败降级不影响其他模块。

---

## 2. 模块设计与职责

### 2.1 bluewhale_client.py — 蓝鲸选品对接

- 输入:API key / 账号凭证(具体接入方式等你提供 API 文档)
- 职责:拉选品列表(按类目、时间窗、关键词),提取每个选品的:
  - `name` 商品名(转成美客多搜索关键词的来源)
  - `source_url` 源平台链接(蓝鲸给的链接,可能是 Amazon/Temu 等)
  - `category` 类目、`price_cny` 参考价、`sales_est` 销量估算
  - 若有 `ean` / `upc` / `brand+model`,直接作为美客多匹配的强标识
- 注意:蓝鲸选品是第三方 SaaS,按其 API 限频;导出能力(如果有 CSV)可作兜底

### 2.2 ml_api.py — 美客多官方 API 封装(核心,合规)

**OAuth2 授权流**(首次一次性):

```
1. GET https://auth.mercadolibre.com.<site>/authorization
     ?response_type=code&client_id=<CLIENT_ID>&redirect_uri=<你的回调>
2. 用户在美客多页面授权 → 回调带 code
3. POST https://api.mercadolibre.com/oauth/token
     {grant_type: authorization_code, client_id, client_secret, code, redirect_uri}
   → 返回 access_token(6h) + refresh_token(6 个月)
4. 之后用 refresh_token 自动续期;6 个月未刷新需重新授权
```

**必须实现 `token_manager`**:自动刷新 + 落盘 + 过期告警,这是整个 API 模块的地基。

**常用端点**(全部用你的 token):

| 用途 | 端点 |
|---|---|
| 当前用户 | `GET /users/me` |
| 我的商品列表 | `GET /users/{user_id}/items/search?status=active` |
| 商品详情 | `GET /items/{ITEM_ID}`(含 sold_quantity / available_quantity / price / category_id / permalink) |
| 商品质量 | `GET /items/{ITEM_ID}/health`(listing 质量分) |
| 访客趋势(90天) | `GET /visits/items?ids=...`(估流量/转化) |
| 订单 | `GET /orders/search`(自己店铺) |
| 卖家信誉 | `GET /reputations/{seller_id}/summary`(竞品卖家也能查) |
| 类目树 | `GET /sites/{SITE_ID}/categories` |
| 搜索(竞品) | `GET /sites/{SITE_ID}/search?q=...&limit=50`(也带 token,不限 IP) |
| 上架(后续铺货) | `POST /items` |

**站点 ID**:阿根廷 MLA / 巴西 MLB / 墨西哥 MLM / 智利 MLC / 哥伦比亚 MCO / 秘鲁 MPE / 乌拉圭 MLU 等——**先确认你做哪个站点**,所有搜索和类目都挂在站点 ID 下。

### 2.3 ml_scraper.py — 竞品网页采集(可选、灰区、需代理)

- 只采集官方 API **覆盖不到**的公开页面数据:商品长评论、页面独有的字段、卖家店铺页细节
- 用**会话级粘性代理**(住宅代理池,如隧道代理服务)+ 随机 User-Agent + 限速(每请求间隔 2~5s)+ 指数退避重试
- 输入商品 permalink / 搜索 URL,输出结构化 JSON,与 API 数据统一入库
- 遵守 `robots.txt`、不采集个人信息(巴西 LGPD)、不登录账号态采集、频率控制在合理区间

### 2.4 proxy_pool.py — IP 代理池(独立可选模块)

- 封装隧道代理/住宅代理服务商 API:取 IP、健康检查、失败剔除、轮换策略
- 提供 `get_proxy()` / `report_failure(proxy)` 两个接口,`ml_scraper` 只管用
- 未配置代理时自动降级为直连(仅用于官方 API 路径)

### 2.5 storage.py — 数据层(SQLite)

表设计(全部含 `created_at` / `updated_at`):

```sql
-- 蓝鲸选品
blue_whale_products(
  id PK, name, category, source_platform, source_url,
  price_cny, sales_est, ean, raw_json, fetched_at)

-- 美客多竞品 listing(唯一键 item_id)
ml_listings(
  item_id PK, title, category_id, price, available_qty, sold_qty,
  seller_id, seller_reputation, condition, permalink,
  date_created, first_seen_at, last_seen_at)

-- 每日快照(销量/价格增量估算的数据基础)
listing_snapshots(
  id PK, item_id FK, sold_qty, price, available_qty, snapshot_at)

-- 访客快照(来自 /visits/items)
visits_snapshots(
  id PK, item_id FK, visit_date, visits)

-- 选品 ↔ 竞品匹配结果
product_matches(
  id PK, blue_whale_product_id FK, ml_listing_id FK,
  match_score, match_method, created_at)

-- 自己店铺商品
own_items(
  item_id PK, status, price, sold_qty, available_qty,
  category_id, visits_90d, last_sync_at)

-- 调研决策输出
research_decisions(
  id PK, blue_whale_product_id FK, competition_level,
  suggested_price, decision, reasoning, created_at)
```

### 2.6 matcher.py — 选品 → 美客多竞品匹配

按强度排序的匹配策略:

1. **强标识**:EAN / UPC / ISBN / 品牌+型号 精确匹配(如果有)
2. **标题相似度**:蓝鲸品名清洗(去品牌词/规格词)→ 分词 → 美客多搜索 → 对结果做 Jaccard/词向量相似度打分
3. **图片感知哈希**(pHash):蓝鲸图 vs 美客多 listing 图,相似度阈值过滤(可选,需要图下载)

输出 `product_matches`,每条带 `match_score` 和 `match_method`。

### 2.7 analytics.py — 跟卖分析引擎

对每个选品计算:

| 指标 | 计算方式 | 用途 |
|---|---|---|
| 竞品数量 | 匹配到的在售 listing 数 | 竞争饱和度 |
| 价格带 | min / p25 / 中位 / p75 / max | 定价区间 |
| 头部销量 | 按 sold_qty 排序,top3 集中度 | 头部垄断程度 |
| 日销估算 | `listing_snapshots` 前后两天 sold_qty 差 | 真实动销(需连续快照) |
| 流量估算 | `visits_snapshots` 90 天访客 | 转化率推算 |
| 卖家信誉 | 头部竞品卖家 reputation 等级 | 跟卖风险(大卖盯梢) |
| 差评率 | 评论采集(若有) | 品控风险 |
| 自家对照 | 自己店铺是否已在卖、价格差 | 补货/调价决策 |

**跟卖建议规则示例**(可配置):

```
竞争度 = f(竞品数, 头部集中度, 头部信誉, 差评率)
建议 = 竞争度低 且 头部日销>阈值  → 建议跟卖,定价 = 中位数 × 0.95
       竞争度高 且 头部差评率高  → 建议避开
       自家已有 且 价格高于中位  → 建议调价
```

### 2.8 scheduler.py + report.py — 调度与输出

- `scheduler.py`:APScheduler / cron,每日定时跑"快照采集"(sold_qty/price/visits),每周跑一次"全量调研"
- `report.py`:输出 CSV / Markdown 周报 / 简单 Web 面板(可选)

---

## 3. 一次跟卖调研的完整流程

```
① 蓝鲸选品 API 拉候选选品(按你的类目/预算窗口)
② 对每个选品:
   匹配器 → 美客多搜索竞品(官方 API 优先)
   竞品 top N 落库 + 首次快照
   若有历史快照 → 估算日销
③ 店铺 API 拉自家商品,比对"是否已在卖"
④ 分析引擎算竞争度 + 建议定价
⑤ 输出决策表(CSV/周报)
⑥ 每日定时:对已入库竞品追加快照,日销估算自动累积
```

---

## 4. 目录规划(待实现)

```
ml-research/
├── design.md            # 本方案
├── config.example.toml  # 站点ID/API key/代理配置模板
├── bluewhale_client.py  # 蓝鲸选品 API
├── ml_api.py            # 美客多 OAuth2 + API + token_manager
├── ml_scraper.py        # 网页采集(可选,代理)
├── proxy_pool.py        # 代理池(可选)
├── storage.py           # SQLite 数据层
├── matcher.py           # 匹配引擎
├── analytics.py         # 跟卖分析
├── scheduler.py         # 定时任务
├── report.py            # 输出
└── run_research.py      # 一键跑全流程
```

---

## 5. 分阶段实施计划

| 阶段 | 内容 | 预估 |
|---|---|---|
| P1 | 蓝鲸 API 对接(需要你提供 API 文档)+ 美客多 OAuth 授权 + 店铺数据拉取落库 | 1~2 天 |
| P2 | 官方 API 搜索竞品 + 快照机制 + 日销/访客估算 | 2~3 天 |
| P3 | 匹配引擎(标题/EAN/图片)+ 竞争度指标 + 跟卖建议 | 2~3 天 |
| P4 | 定时任务 + 周报输出;按需接入代理池做网页补充采集 | 1~2 天 |

P1 先跑通"蓝鲸 → 自己店铺"最小闭环,验证授权和数据结构;P2 再引入竞品;P3 才做分析;P4 自动化。

---

## 6. 风险与合规提醒

1. **美客多官方 API 是合规主通道**:自己店铺、搜索、访客、订单全有官方端点,优先全部走 API。
2. **网页采集是灰区**:遵守 robots.txt、限速、住宅代理、不采个人信息(巴西 LGPD)、不登录态采集;若被风控,立即降速或停用该通道。
3. **蓝鲸选品按平台规则使用**,勿超频。
4. **token 安全**:access_token / refresh_token 只存本地 `.env`(gitignore),绝不入库/提交。
5. **数据仅用于自身调研**,不对外贩卖采集数据。

---

## 7. 需要你补充的输入

- [ ] 美客多**站点国家**(决定 site_id:MLA/MLB/MLM/…)
- [ ] 蓝鲸选品 **API 文档 / 密钥 / 接口示例**(或"有账号但只能网页导出"的实际情况)
- [ ] 蓝鲸选品导出的字段里**有没有 EAN/UPC 等强标识**
- [ ] 你主要看的**类目**
- [ ] 是否需要后续"一键上架到美客多"(决定要不要做 `POST /items` 部分)

# 数码电商 Agent Skill 调度 Demo 中文说明

## 1. 项目简介

本项目是 Group5《Python 数据采集》期末项目的真实数据版 Demo，主题为“数码电商精细化分析”。系统采用 **FastAPI + Streamlit + SQLite + APScheduler + Agent Skills** 的工程结构，实现从自然语言指令到爬虫调度、数据查询、平台对比和可视化展示的闭环。

当前版本按“不使用样例，只使用真实数据”的要求设计：系统启动后数据库为空，只有真实抓取或授权真实数据文件解析成功后才会写入 SQLite。

## 2. 核心口径

- 京东和苏宁统一展示：价格、商家、评论数量、评价标签。
- 不采集、不保存、不展示评论正文。
- 京东走 ScrapingBee 云端渲染服务，不直接访问 `search.jd.com`，因为该域名对 requests/playwright headless 强制重定向登录。
- 苏宁从公开搜索页获取商品入口和评价数量，从公开价格接口获取价格，从公开评价接口只提取标签字段。
- ZOL 保留为可选排行参考源，本次不生成样例数据。

## 3. 系统架构

```mermaid
flowchart LR
    U["用户 / Streamlit 控制台"] --> A["FastAPI Agent 接口"]
    A --> O["Agent Orchestrator"]
    O --> L["Gitee AI / OpenAI 兼容接口"]
    O --> R["Skill Registry 白名单"]
    R --> S1["run_crawl"]
    R --> S2["schedule_crawl"]
    R --> S3["query_products"]
    R --> S4["compare_sources"]
    S1 --> C["京东 (ScrapingBee 渲染) / 苏宁公开接口"]
    S2 --> J["APScheduler 定时任务"]
    C --> D["SQLite product_items"]
    S3 --> D
    S4 --> D
    D --> V["表格 / 图表 / 状态卡"]
```

## 4. 目录结构

```text
group5/
├── agent/
│   ├── orchestrator.py      # Agent 指令解析：tool calling / JSON fallback
│   └── skills.py            # 白名单 Skill 注册与执行
├── api/
│   └── main.py              # FastAPI 后端接口
├── jobs/
│   └── scheduler.py         # APScheduler 定时任务
├── spiders/
│   ├── jd_spider.py         # 京东:走 ScrapingBee 渲染代理
│   ├── suning_spider.py     # 苏宁真实数据爬虫
│   └── zol_spider.py        # ZOL 可选参考源占位
├── storage/
│   └── db.py                # SQLite 初始化、查询和聚合
├── tests/
│   └── test_live_mode.py    # 真实数据模式核心流程测试
├── streamlit_app.py         # Streamlit 控制台
├── requirements.txt
└── README.md
```

## 5. SQLite 核心字段

| 字段 | 含义 |
| --- | --- |
| `source` | 数据来源：`jd`、`suning`、`zol` |
| `product_id` | 商品ID(京东 sku / 苏宁 productId)，用于去重 |
| `keyword` | 商品关键词 |
| `title` | 商品标题 |
| `price` | 商品价格 |
| `merchant` | 商家名称 |
| `comment_count` | 评论数量 |
| `rating_tags` | 评价标签，多个标签用顿号连接 |
| `rank` | 可选排行字段 |
| `crawled_at` | 抓取时间 |

## 6. Agent Skills

| Skill | 功能 |
| --- | --- |
| `run_crawl` | 手动触发一个或多个数据源爬虫，并写入 SQLite |
| `schedule_crawl` | 创建周期性爬虫任务 |
| `query_products` | 按关键词和来源查询商品数据 |
| `compare_sources` | 聚合比较京东、苏宁易购、中关村在线数据 |
| `job_status` | 查看当前定时任务和数据库状态 |

系统优先尝试 Gitee AI / OpenAI 兼容 tool calling。模型或接口不稳定时，自动降级为本地 JSON action 解析。两种方式都不会生成样例数据。

## 7. 京东合规真实数据源

京东 `search.jd.com` 对 requests/playwright headless 强制重定向到登录页，本地直接抓不可行。本项目接入第三方云端渲染服务 [ScrapingBee](https://app.scrapingbee.com/account/register)，由其在云端使用真实浏览器拿到渲染后的 HTML，本地负责 BeautifulSoup 解析。

需要的环境变量：

```powershell
$env:SCRAPINGBEE_API_KEY="..."
# 可选
$env:JD_RENDER_JS="true"      # 默认 true
$env:JD_PREMIUM_PROXY="true"  # 默认 true
```

当前代码使用的接口：

- 搜索列表：`https://search.jd.com/Search?keyword=...&enc=utf-8`，解析 `li.gl-item`。
- 评论摘要：`https://club.jd.com/comment/productCommentSummaries.action?referenceIds=...`，取好评率/追评/差评。

没有配置 API key 时，京东 skill 会返回清晰错误，不会伪造数据。

## 8. 运行方法

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

启动 FastAPI：

```powershell
python -m uvicorn api.main:app --reload --port 8000
```

启动 Streamlit：

```powershell
python -m streamlit run streamlit_app.py
```

默认访问地址：

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8501
```

## 9. 演示流程建议

1. 打开 Streamlit 控制台，展示 FastAPI、Scheduler、SQLite、Gitee AI 状态。
2. 说明数据库初始为空，不使用 seed 样例数据。
3. 执行自然语言指令：

```text
每30分钟抓取 手机 京东和苏宁价格、商家、评论数量和评价标签
```

4. 点击”设置定时”，展示京东、苏宁两个定时任务。
5. 点击”手动抓取”，展示苏宁真实数据入库；京东如果未配置 SCRAPINGBEE_API_KEY，会返回明确错误。
6. 展示表格中的平台、商品标题、价格、商家、评论数量、评价标签和更新时间。
7. 展示最低价对比图和数据源概览。

## 10. 测试方法

```powershell
python -m pytest -q
```

当前测试覆盖：

- SQLite 是否在无 seed 情况下保持空库。
- Agent fallback 是否能把自然语言指令路由到 `schedule_crawl`。
- 京东缺少 SCRAPINGBEE_API_KEY 时是否明确失败。
- SQLite 查询结果是否使用 `rating_tags`，不再暴露 `comment_text`。
- 重复抓取同一商品时,数据库不会出现两条相同 (source, product_id) 的行。

## 11. 合规说明

项目不绕过验证码、不破解签名、不抓取隐私数据、不强行访问受限接口。淘宝和苏宁易购都属于动态渲染、登录或反爬相关场景，答辩时应明确说明 robots.txt、网站规则、反爬处理和数据使用边界。

本项目的真实数据策略：

- 苏宁：公开搜索页 + 公开价格接口 + 公开评价接口中的标签字段。
- 京东：通过 ScrapingBee 云端渲染服务获取页面 HTML，由服务商负责合规处理 robots/反爬。
- 不使用 seed 样例，不把模拟数据伪装成真实数据。

# 数码电商 Agent Skill 调度 Demo

这是 Group5 的真实数据版项目骨架：FastAPI 负责 Agent、Skill 调用、APScheduler 定时任务和 SQLite 查询；Streamlit 负责简化控制台展示。

## 功能

- 自然语言触发 Agent，例如"每30分钟抓取 手机 京东和苏宁价格、商家、评论数量和评价标签"。
- 白名单 skills：手动抓取、设置定时、查询商品、平台对比、任务状态。
- 主要数据源：京东、苏宁易购，ZOL 保留为可选排行参考源。
- 京东/苏宁字段：价格、商家、评论数量、评价标签。
- 不采集、不保存、不展示评论正文。
- 不再生成样例数据；数据库初始化为空，只有真实抓取或授权真实数据解析成功才写入。
- SQLite 设有 `UNIQUE(source, product_id)` 索引，重复抓取自动 UPSERT，不会撑爆表。
- 苏宁爬虫：User-Agent 轮换、随机请求间隔、指数退避重试、价格接口多模板降级。
- 所有爬虫/调度的关键事件都会写到 `logs/spider.log`（rotating，单文件 2MB × 3）。

## 运行

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium  # 仅在用 ScrapingBee 失败时本地排错时用
python -m uvicorn api.main:app --reload --port 8000
```

另开一个终端：

```powershell
python -m streamlit run streamlit_app.py
```

## Gitee AI

默认没有 API key 时，Agent 使用本地 JSON action 降级解析，仍可调用 skills。若要接入 Gitee 模力方舟：

```powershell
$env:GITEE_AI_API_KEY="你的key"
$env:GITEE_AI_BASE_URL="https://ai.gitee.com/v1"
$env:GITEE_AI_MODEL="qwen2.5-72b-instruct"
```

## 京东真实数据配置 (ScrapingBee)

京东 `search.jd.com` 对非浏览器请求强制重定向到登录页，requests/playwright headless 都会被风控直接挡掉。本项目采用云端渲染代理 [ScrapingBee](https://app.scrapingbee.com/account/register) 拉取渲染后的 HTML，再本地解析。

```powershell
$env:SCRAPINGBEE_API_KEY="你的key"  # 免费注册即送 1000 次调用额度
# 可选
$env:JD_RENDER_JS="true"        # 默认 true,关掉会快很多但只能拿到首屏
$env:JD_PREMIUM_PROXY="true"    # 默认 true,关掉就走普通代理,过京东风控概率低
```

不配置 API key 时，京东 skill 会返回清晰的报错，不会伪造数据。

字段映射：
- 商品列表：`search.jd.com/Search?keyword=...` 渲染后的 `li.gl-item`，取 `.p-name em` / `.p-price i` / `.p-shop a` / `.p-commit strong a`。
- 评价摘要：`club.jd.com/comment/productCommentSummaries.action`，取好评率 / 追评数 / 差评数拼成 `rating_tags`。

## 合规说明

项目不绕过验证码、不破解签名、不抓取隐私数据、不强行访问受限接口。苏宁抓取基于当前可访问的公开页面、价格接口和评价标签接口。京东走第三方渲染服务商，由其负责合规处理 robots/反爬。

## API

- `POST /agent/command`
- `POST /skills/crawl`
- `POST /skills/schedule`
- `GET /skills/jobs`
- `GET /data/products`
- `GET /data/compare`
- `GET /health`

# 数码电商 Agent Skill 调度 Demo

这是 Group5 的真实数据版项目骨架：FastAPI 负责 Agent、Skill 调用、APScheduler 定时任务和 SQLite 查询；Streamlit 负责简化控制台展示。

## 功能

- 自然语言触发 Agent，例如"每30分钟抓取 手机 苏宁和哔哩哔哩价格、商家、评论数量和具体评论"。
- 白名单 skills：手动抓取、设置定时、查询商品、平台对比、任务状态。
- 主要数据源：苏宁易购、唯品会、哔哩哔哩；ZOL 作为手机和笔记本电脑型号参考源。
- 主商品库 `data/live_products.db` 只保存商品基础字段：来源、型号关键词、标题、价格、商家、评论数、排名和抓取时间。
- 评论明细库 `data/product_comments.db` 按"型号 - 商家 - 具体评论"保存评论正文。
- 不再生成样例数据；数据库初始化为空，只有真实抓取或授权真实数据解析成功才写入。
- SQLite 设有 `UNIQUE(source, title, merchant)` 索引，重复抓取自动 UPSERT，不会撑爆表。
- 苏宁爬虫：User-Agent 轮换、随机请求间隔、指数退避重试、价格接口多模板降级。
- 唯品会爬虫：使用独立 Playwright 用户目录保存登录状态，登录后监听搜索页公开商品接口写入 SQLite。
- 哔哩哔哩爬虫：根据 ZOL 手机/笔记本型号定向搜索视频，抓取视频标题、BV 号和视频下方公开评论。
- 所有爬虫/调度的关键事件都会写到 `logs/spider.log`（rotating，单文件 2MB × 3）。

哔哩哔哩爬虫默认交互式选择“爬型号 / 爬类型”，爬类型时会读取 ZOL 型号库，默认抓取 150 条视频数据：

```powershell
python spiders/bilibili_spider.py
python spiders/bilibili_spider.py "iPhone 15" --limit 30
python spiders/bilibili_spider.py --category phone --limit 150
```

## 运行

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium  # 仅在使用唯品会 Playwright 登录态时需要
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

## 哔哩哔哩数据源

B 站不需要登录。爬虫会先访问首页和搜索页获取基础 cookie，再访问公开搜索和评论接口。

字段映射：
- `keyword`：ZOL 型号或手动输入的型号。
- `title`：视频标题。
- `merchant`：BV 号。
- `comment_count`：B 站评论总数。
- 视频下方公开评论不会写入商品表，会直接逐条写入 `data/product_comments.db`。

可选环境变量：

```powershell
$env:BILIBILI_COMMENTS_PER_VIDEO="20"
$env:BILIBILI_MAX_SEARCH_PAGES="10"
```

## 合规说明

项目不绕过验证码、不破解签名、不抓取隐私数据、不强行访问受限接口。苏宁抓取基于当前可访问的公开页面、价格接口和评价接口；B 站抓取公开搜索结果和公开评论；ZOL 仅作为公开型号参考源。

## API

- `POST /agent/command`
- `POST /skills/crawl`
- `POST /skills/schedule`
- `GET /skills/jobs`
- `GET /data/products`
- `GET /data/compare`
- `GET /health`

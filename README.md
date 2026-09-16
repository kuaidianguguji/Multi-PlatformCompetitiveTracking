# CompetitiveTracking

Python + DrissionPage 的模块化商品监控项目。当前实现 **飞书监控表读取 → Mercado/蓝鲸商品采集 → 本地 JSON 与日志 → 飞书结果多维表更新、二维表历史追加、运营消息推送**。

已接入 **Shopee / Shopdora 巴西站**：任务读取 → 收藏扫描 → 选产品补充指标 / 缺失 ID 加入收藏 → JSON 与日志 → 多维表更新 + 二维表历史追加 → 对应运营人员消息推送。

已接入 **TikTok / FastMoss 巴西站**：任务读取 → 按完整商品 ID 查询 → 手机号密码登录或复用会话 → 页面与查询响应核对 → JSON 与日志。目前 TikTok 只采集，不写飞书表、不发送消息。

## 快速开始（Windows / PowerShell）

需要 Python 3.11+ 和已安装的 Chrome 或 Edge。首次使用需要能看见浏览器并手动登录蓝鲸。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item config.example.toml config.toml  # 已有 config.toml 时不要覆盖
```

编辑 `config.toml` 的 `[feishu]`：

| 配置 | 来源 |
| --- | --- |
| `app_id` | 飞书开放平台企业自建应用的 App ID |
| `app_secret` | 同一自建应用的 App Secret |
| `app_token` | 多维表链接 `/base/` 后的 token |
| `table_id` | 多维表链接 `table=tbl...` 参数 |

应用需开通“查看多维表格”相关读取权限、发布应用版本，并具有目标多维表的访问权限（在多维表的添加文档应用/应用权限中授权）。只有截图无法获得表标识。若链接为 `/wiki/`，需要获取其承载多维表的 app_token，不能直接把 Wiki 节点 token 填入。

也可以使用环境变量 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_APP_TOKEN`、`FEISHU_TABLE_ID`，它们优先于配置文件。无需填写蓝鲸密码，蓝鲸通过手动登录及会话复用认证。

```powershell
.\.venv\Scripts\python.exe -m competitive_tracking check-config
.\.venv\Scripts\python.exe -m competitive_tracking once
.\.venv\Scripts\python.exe -m competitive_tracking serve
```

`once` 立即采集一次；`serve` 常驻并按 `schedule.daily_time` / `schedule.timezone` 每日执行。自定义配置位置使用 `python -m competitive_tracking --config C:\path\config.toml once`。所有相对输出路径以配置文件所在目录为基准。

仅运行 Shopee：`python -m competitive_tracking once --platform shopee`；仅运行 Mercado 则使用 `--platform mercado`。不指定平台时按 `[app].platforms` 顺序执行。启用 Shopee 时填写后文的 `[shopee]` 配置。

`check-config` 仅检查本地配置，不验证远程权限。返回码：0 正常；1 运行失败或部分采集失败；2 配置缺少必要飞书字段；130 用户中断。

## 当前流程

1. 使用飞书 API 自动读取所有记录页，处理访问令牌过期、限流和暂时性网络错误。
2. 对每个平台筛选：平台为 `mercado`、商品 ID 非空、监控开关值恰为“开启”、数据推送人非空。监控字段不把布尔 true 或数字 1 当作开启。
3. 以 `平台:商品ID` 去重，例如 `mercado:MLB6984707226`。同一商品的所有飞书记录、接收人、推送开关保留在 `tracking_records`。推送开关不影响采集，发送消息时逐记录判断。
4. 无合格记录时直接进入下个平台，不启动浏览器。未实现的平台记为 `unsupported` 并继续。
5. 自动创建 `stron_token/mercado/profile`。专用 Chromium profile 保存 Cookie、localStorage 和浏览器会话信息；另外按来源域保存/恢复 sessionStorage。会话过期仍需登录。
6. 打开收藏页，文档加载完成后固定等 10 秒。若跳转 `/login`，点击“注册/登录”，最多等 300 秒，检测到 `/home` 后额外等待 `login_settle_seconds`（默认 5 秒），再进入收藏页。
7. 从收藏第一页开始，遍历虚拟滚动的所有行和列，合并固定图片列，自动翻页；找到全部目标后可以提前结束。若仍缺商品，则必须完成收藏扫描，才能进入搜索补全。
8. 对收藏中缺少的 ID 进入搜索页、输入商品 ID、查询。严格对比“商品ID”，不会把被跟卖商品 ID 或第一条不匹配的搜索结果当成命中。
9. 搜索命中后采集商品，点击该行“加入收藏”。等待 2 秒及异步弹窗出现，选择“每日查询分组”，点击“确认”，通过按钮变为“取消收藏”验证成功。已收藏的不重复添加。请提前创建该分组。
10. 输出 JSON，并在控制台和轮转日志中打印每个命中商品。商品错误、收藏失败、数据缺失均留下状态和原因。
11. 如果启用 `[feishu_output]`，将有效商品同步到配置的结果表。先保存本地采集结果，再写飞书；写入失败不会丢失采集数据，可以使用已有 JSON 补写。
12. 如果启用 `[feishu_sheets]`，按 A 列最后有内容的行向下追加本轮商品历史。多维表和二维表独立执行，一方写入失败不阻止另一方。
13. 如果启用 `[feishu_messages]`，重新读取任务表的当前推送条件，向“数据推送人”发送商品 Markdown 卡片。三个输出模块独立执行，发送失败保留本地数据及待重试消息。

登录 XPath 已修复原需求中粘贴损坏的引号及括号。可变 XPath / CSS 集中在 `config.toml` 的 `[mercado.selectors]`；表格解析按表头与 `colid` 对应关系，不依赖固定列号。

## 数据字段与完整性

当前解析名称、链接、图片、类目、各币种价格、7/30/60/90 天及总销量、销售额与币种、销量环比、评论数、评分、尺寸、BSR、库存与履约、品牌/卖家、上架日期、平台数据更新时间、转化率，以及网页存在时的访问量。

每个商品同时保留 `raw_fields`：全部已采集列的文本、文本片段、链接、图片、title 属性及未读取 Canvas 数量。后续页面出现新列时，原始字段仍能保存，增加标准化字段只需修改解析器。未展示的指标为 `null`；百分比值以百分数保存，例如 `8.11` 表示 `8.11%`。采集时间和平台数据更新时间分开保存。

**蓝鲸导出 HTML 的限制：**提供的文件分页显示 63 条、第一页仅保存了 29 个唯一商品；销量、销售额、部分库存是 Canvas，静态 HTML 中不存在数值。因此离线解析不能还原全部 63 条或 Canvas 数值。实时模式在导航前注册 Canvas `fillText/strokeText` 监听，读取实际绘制文字，并滚动采样。若平台改为图片绘字、WebGL 或其他绘制方法，会输出 `partial` 和缺失提示，需要适配，不能把缺失当作 0。

虚拟滚动或分页不前进、达到上限、分页总量与扫描量不一致时，程序报告错误，不把未扫描区域内的商品判断为“收藏不存在”。搜索结果不更新会超时报错。采集异常不妨碍其他平台继续执行。

```json
{
  "schema_version": 1,
  "status": "ok",
  "products": {
    "mercado:MLB123": {
      "status": "ok",
      "platform": "mercado",
      "product_id": "MLB123",
      "tracking_records": [
        {"record_id": "rec_example", "recipients": [{"id": "ou_example"}], "push_enabled": false}
      ],
      "product": {
        "origin": "favorite",
        "prices": {"BRL": 24.06},
        "sales": {"7d": 0, "30d": 17986},
        "conversion_rate_percent": 8.11,
        "raw_fields": {},
        "warnings": []
      }
    }
  }
}
```

以上为精简示例。运行结果保存在 `data/run_时间戳.json` 和 `data/latest.json`。状态包括 `ok`、`partial`、`not_found`、`error`；平台另有 `skipped`、`unsupported`。失败结果也会落盘。`partial` 例如商品已采到但加入收藏失败，或部分 Canvas 数值没有读到。

## 常驻与会话

- 默认每天中国时间 09:00 执行，启动时不立即采集；需要启动即执行可设置 `run_on_start = true`。
- `stron_token/schedule_state.json` 保存当日定时执行记录，防止常驻进程重启后重复执行。失败当日也算一次尝试，修复后可用 `once` 手动补跑。`once` 不占用定时执行记录。
- `run_on_start = false` 且启动时已过当天计划时间，将等待次日；运行期间电脑睡眠后恢复，会在下次检查时补执行一次，不补跑历史多天。
- 程序使用进程锁避免同时读写同一 profile。默认每轮正常关闭浏览器以保存会话；端口被占用会明确报错，不接管日常浏览器。
- `close_after_run = false` 用于单次调试，下一次运行前必须关闭遗留的项目浏览器。定时运行应保留默认 `true`。
- Shopee 根据当前要求始终在查询结束后关闭浏览器，覆盖 `close_after_run = false`；新标签页中执行采集时也会保存会话并退出整个项目浏览器。
- 常驻依赖进程和电脑持续运行；Windows 可在“任务计划程序”创建用户登录时启动 `serve` 的任务。首次及过期登录需要交互桌面，请选择“仅当用户登录时运行”，不要无头运行。程序路径为虚拟环境 Python，参数为 `-m competitive_tracking --config "完整配置路径" serve`，起始目录设为项目目录。

`stron_token` 包含敏感登录数据和飞书同步状态；`config.toml` 包含密钥；`data`/`logs` 可能包含业务数据。它们已被 `.gitignore` 排除。不要手动强制加入 Git。外部写操作包括蓝鲸“加入收藏”、启用后的飞书表同步和消息发送，各模块可独立关闭。

## TikTok / FastMoss 采集

任务表平台为 `tiktok`、商品 ID 非空、监控开关严格为“开启”、数据推送人非空才会查询。商品 ID 保持字符串，19 位数字不会经过浮点转换。

在本地 `[tiktok]` 配置 `username`、`password`，也可设置 `FASTMOSS_USERNAME` / `FASTMOSS_PASSWORD` 环境变量。手机号区号默认 `+86`，提交前核对页面区号。未配置凭据时打开密码表单，允许手动登录；验证码仍需在可见浏览器内完成。登录最长等待、登录稳定等待、页面等待和结果等待均独立配置。真实凭据不要放入示例配置或 Git。

```powershell
python -m competitive_tracking once --platform tiktok
# 离线解析用户导出的商品结果页，不访问账户
python -m competitive_tracking parse-html "C:\path\商品结果.html" --platform tiktok --output data/tiktok_offline.json
```

1. 最大化专用浏览器，使用 `stron_token/tiktok/profile` 保存 Cookie/localStorage，另保存同源 sessionStorage。
2. 逐个构造 `https://www.fastmoss.com/zh/e-commerce/search?page=1&words=商品ID&region=BR`；有游客弹窗则点击“登录” → “手机号登录/注册” → “密码登录”，填写凭据并提交。
3. 登录成功后先重新打开**当前商品**，再继续下一个 ID；不会跳过触发登录的首个商品。中途登录失败停止其余查询并记录失败，保留已完成商品。
4. 只监听页面正常发起的 `/api/goods/V2/search` 响应。核对本次商品 ID、BR、页码、接口成功状态，再等待 DOM ID 集合与响应一致、分页及字段稳定。搜索框值、行 ID、商品详情链接 ID 同时核验；登录弹窗后的背景商品不作为结果。
5. 读取整个表格 DOM，包含横向滚动视野外的列；找不到目标时扫描后续页，重复页、超过上限或接口失败记为错误，不当作未找到。
6. 逐商品打印 JSON，结束后保存 `data/run_*.json`、`data/latest.json` 并关闭专用浏览器。`once --platform tiktok` 不调用 Mercado/Shopee 的远程输出。

JSON 保留以下数据供后续选字段：

| 分类 | 数据 |
| --- | --- |
| 商品 | 完整 ID、完整标题、巴西国家标识、图片、FastMoss 详情页、真实 TikTok 商品链接（响应提供时）、类目及三级类目 |
| 价格与状态 | BRL 显示售价、佣金比例、星级、上架时间、下架/包邮原始标记；SKU 库存入口是否存在，不伪造实际库存数 |
| 销量和金额 | 昨日、近 7/14/28 天、总销量与销售额；响应确认为 BRL 时保存 BRL 金额，页面万/亿约数另存 |
| 达人/内容 | 达人出单率、关联达人、总关联达人、关联视频、关联直播数量；达人出单率不解释为商品转换率 |
| 店铺 | 名称、ID、图片、FastMoss 店铺链接、店铺总销量 |
| 趋势 | `sales_trend` 保存日期与逐日销量；`raw_product.trend` 保留来源原值，不将来源可能为占位的日销售额 0 擅自解释为真实销售额 |
| 审核信息 | `raw_fields` 每列文本、完整标题、链接、图片；`display_values` 页面约数；`raw_product` 本次商品查询对象；`search_url`、`captured_at`、`warnings` |

接口与 DOM 的商品 ID、国家必须相同。接口完整数值优先，未验证的其他字段仅保留原始值；`global` 中换算的其他币种不混入 BRL。只保存商品对象，不保存响应外层的登录标识/IP、请求头、Cookie、密码。静态 HTML 缺少完整 Canvas 曲线及接口数据时保留警告；登录 HTML 中的背景商品会被拒绝解析。

添加 TikTok 采集不会启用其消息推送。`feishu_messages.platforms` 目前仍只支持 Mercado 和 Shopee，后续明确字段与目标表后再接入。

## Shopee / Shopdora 采集

在任务表中把平台设为 `shopee`，商品 ID 填完整数字字符串；监控开关必须为“开启”，数据推送人不能为空。采集目标仍以“平台 + 商品 ID”区分。`[shopee]` 配置账号、密码、等待时间和分页上限；`SHOPDORA_USERNAME` / `SHOPDORA_PASSWORD` 环境变量优先于本地配置，示例配置不包含账号。旧配置启用 Shopee 前需补充 `[shopee]` 及 `[shopee.selectors]`。

运行流程：

1. 用 `stron_token/shopee/profile` 启动并最大化专用浏览器，先访问首页。已登录则复用；未登录则填账号密码并点击登录，等待用户区域显示登录成功。若出现验证码，可在登录等待时间内手动完成。
2. 展开 Header 的“产品”菜单进入“我的收藏”。适配菜单新开标签页，切换后关闭原标签页。
3. 重置残留筛选，点击筛选区的“巴西”，核对单选框确实选中，再点击“查询”。顶部全局站点不作为查询站点依据。
4. 按页扫描主行及其补充信息行，只保留目标 ID 的数据。全部找到即停止收藏翻页；目标仍有缺失则核验收藏页已遍历完整，再逐个进入“选产品”。开启 `shopee.enrich_favorites` 时，已收藏商品也按 ID 查询选产品，补齐日销量、销售额和排名；关闭时恢复全部命中即结束的原流程。
5. 选产品页重置筛选、选中巴西、清除可能默认恢复的价格区间，在专用产品 ID 输入框输入 ID 并查询。结果必须精确匹配 ID；采到后点击该商品行的“加入收藏”，等待变为“取消收藏”。收藏失败时保留已采集数据并标记 `partial`。
6. 页面结果需与本次查询响应的 ID、总数、页码一致。分页不前进、接口失败、解析不完整均报错，不能把旧结果或未完成加载判为商品不存在。每条商品打印 JSON 日志，结束时保存运行 JSON 并关闭浏览器。

Shopee 商品 JSON 中的主要字段：

| 字段 | 含义 |
| --- | --- |
| `platform` / `site` / `product_id` | 平台、巴西站 `br`、完整产品 ID |
| `name` / `images` / `url` | 平台完整标题、图片、商品链接；由店铺 ID 和产品 ID 构造的链接标注 `url_source` |
| `prices.BRL` | 页面显示的巴西雷亚尔价格 |
| `sales.daily` / `sales.monthly` | 页面显示的日销量、月销量；收藏页日销量空白时为 `null` |
| `revenue` / `sales_growth_percent` / `revenue_growth_percent` | 页面存在时的日/月销售额、月销量增长率、销售额增长率 |
| `review_count` / `review_rate_percent` / `rating` / `monthly_new_reviews` | 评分数、留评率、星级、月新增评分数；留评率不是转换率 |
| `like_count` / `monthly_new_likes` | 点赞数、月新增点赞数 |
| `category_rank` | 页面存在时的类目排名、日/周变化；原始方向信息一并保留 |
| `category_path` / `categories` / `seller` / `brand` / `shop_type` / `variant_count` | 类目路径、店铺及卖家、本土/跨境、品牌、店铺类型、变体数 |
| `listed_at` / `listing_age` / `favorited_at` | 上架日期、上架时长、页面存在时的收藏时间 |
| `captured_at` / `query_period` / `origin` / `favorite_status` | 本次采集时间、页面查询月份、收藏/搜索来源、收藏结果 |
| `raw_fields` | 按实际表头保存全部列文本、分行值、链接、图片和补充信息；新增未标准化列也保留 |
| `raw_product` | 浏览器本次查询返回的完整商品对象，含页面未直接展示的字段、多语言类目等 |
| `sales_trend` | 当前商品的趋势日期与历史数据点，保留接口原值与 `null` |

`raw_product` 和 `sales_trend` 中价格、评分、比例等可能采用接口缩放单位；它们与页面显示值明确分开，不直接当金额/百分比使用。收藏页与选产品页提供的列不同，因此两种来源的标准字段可能不一样；缺失值不补 0，不猜测转换率。本阶段不进入商品详情、付费导出、监控或其他分析操作。

查询请求由 DrissionPage 监听浏览器正常操作产生的响应，用于确认本次查询完成及保存商品、趋势原始数据。只监听商品查询和趋势接口，不保存登录请求、请求头、Token 或 Cookie 到商品 JSON。[DrissionPage 4 监听说明](https://www.drissionpage.cn/dp40docs/ChromiumPage/listener/)

静态导出 HTML 也可离线解析，但不包含实时接口原始对象或 Canvas 曲线数据：

```powershell
python -m competitive_tracking parse-html "C:\path\mycollect.html" --platform shopee --output data/shopee_offline.json
```

Shopee 使用与其他平台相同的 `run_*.json` / `latest.json` 格式。补充成功时使用本次选产品的完整快照，`enriched_from_search=true`，原收藏快照保存在 `favorite_snapshot`，避免把不同来源的价格、销量、销售额拼成一条记录。补充失败保留收藏数据并标为 `partial`，缺失字段为空，不取上次 JSON 的旧数值填充。

## Shopee 飞书输出（28 个字段）

`[shopee_feishu_output]` 控制 Shopee 多维表更新，`[shopee_feishu_sheets]` 控制 Shopee 二维表历史。两者复用 `[feishu]` 应用凭据，配置与 Mercado 的 `[feishu_output]` / `[feishu_sheets]` 相互独立。开启后 `once` / `serve` 自动写入；只运行 Shopee 时不会触发 Mercado 写入或消息发送。Shopee 自身的推送由 `[feishu_messages]` 总开关及 `platforms` 列表控制。

多维表字段及二维表 A:AB 固定顺序为：

商品ID、商品标题、自定义-商品名、更新时间、竞品、负责人、价格-BRL、日销量、月销量、日销售额、月销售额、评分数、留评率、星级、月新增评分数、点赞数、月新增点赞数、类目排名、近1天排名变化、近7天排名变化、卖家名称、店铺链接、品牌、变体数、类目路径、商品链接、商品图片链接、采集时间。

- 商品 ID 为文本，多维表在 Shopee 独立表内按 ID 更新已有记录；查到多个相同 ID 时报告错误。首次命中则新增，保留现有空记录和人工内容。
- 商品标题来自平台；自定义-商品名、竞品、负责人来自任务表。同一商品多个任务的自定义名去重后以顿号连接，负责人合并；竞品标记冲突时警告并保留原值。
- “更新时间”为本次写入时间，“采集时间”为商品实际 `captured_at`。多维表用日期字段，二维表按 `schedule.timezone` 写时间文本。
- 价格和销售额使用页面 BRL 标准值；留评率使用百分比格式，例如页面 4.10% 在多维表存 0.041、在二维表写 `4.10%`；它不代表转换率。
- 月指标对应页面本次查询期间，期间和原始数据保留在 JSON；不将月销量解释为总销量。排名变化保留来源正负数和 0。
- 多维表未知值不清空旧值；二维表本次未知指标留空，0 正常写入。商品图片保存首张 URL。每轮写入后读取核对；本地状态按目标文档隔离。
- 二维表第 1 行表头，第 2 行起追加历史；查找 A 列最后非空行后方，整行 A:AB 检查为空才写入。同一个 `run_id` 重试不重复追加，新采集追加新历史。

首次创建目标表结构使用下面的显式命令，日常采集不会擅自调整结构。它可重跑续建，只有空白默认主列可以重命名；已有不匹配表头或列顺序会停止，不覆盖。

```powershell
python -m competitive_tracking init-shopee-tables --dry-run
python -m competitive_tracking init-shopee-tables
python -m competitive_tracking once --platform shopee
# 补写现有 JSON，不打开浏览器；加 --dry-run 仅预览
python -m competitive_tracking write-feishu data/run_你的时间戳.json --platform shopee
python -m competitive_tracking write-sheets data/run_你的时间戳.json --platform shopee
```

运行 JSON 中 `shopee_feishu_write` / `shopee_sheets_write` 记录两种输出的状态，详细报告仍保存为 `data/feishu_write_*.json` / `data/sheets_write_*.json`。初始化报告 `data/shopee_schema_*.json` 保存变更前结构和记录。[飞书创建字段 API](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-field/create)

## 写入飞书多维表

`[feishu]` 是监控读取源，`[feishu_output]` 是结果写入目标，分别配置 app_token/table_id，复用同一自建应用凭据。应用必须对目标表有读取字段、读取记录、创建和更新记录的权限；权限变更后需发布应用版本并授予文档访问权限。

目标表采用**每个商品一行，持续更新最新值**，不是每天追加历史行。当前目标配置绑定 `platform = "mercado"`，只接收该平台，按商品 ID 精确查重，等效匹配平台+商品 ID。不可将其他平台混用到这个没有平台列的结果表。按全表读取匹配，不受分享链接中的 view 筛选影响；ID 为空的占位行不会按标题猜测匹配，重复 ID 会报告冲突。

启用方法：在 `config.toml` 的 `[feishu_output]` 填写目标 `app_token`、`table_id`，设置 `enabled = true`。`once` 和 `serve` 随后自动同步；配置示例默认关闭。字段映射位于 `[feishu_output.fields]`，每项均有注释；将非主键映射设为空字符串可禁用该列，不自动创建或更改远程表结构。

| 数据 | 默认目标列 | 转换方式 |
| --- | --- | --- |
| 标识、标题 | 商品ID、商品标题 | 标题取平台实际标题 |
| 运营名称 | 自定义-商品名 | 从当前任务表按 Mercado + 商品 ID 读取；同一商品多个名称去重后以顿号连接，独立于商品标题 |
| 运营元数据 | 竞品、负责人 | 原监控表“是否竞品”与“负责人”，不以推送开关过滤写入 |
| 价格 | 价格-BRL、价格-RMB | BRL / CNY 数值 |
| 销量 | 总销量、销量-7天/30天/60天/90天 | 数值，0 是有效值 |
| 销售额 | 销售额-近30天 | 网页显示的 BRL 销售额 |
| 比例 | 销量变化-7天环比、销量变化-30天环比、转换率 | 目标为文本时写 `8.10%`；百分比格式的数字字段写 `0.081`；普通数字字段写 `8.1` |
| 评价 | 评论数、评分 | 来源数值 |
| 卖家资料 | 品牌、卖家名称、店铺类型 | 将三项数组拆列，不按空格拆分卖家名 |
| 排名 | bsr | 仅有效数字，`--` 不写入 |
| 链接 | 商品链接、商品图片链接 | 超链接对象；图片只写首张地址，不上传附件 |
| 分类 | 类目路径 | 以 ` > ` 连接完整路径 |
| 同步时间 | 更新日期 | 本次写入时间，日期字段；使用毫秒时间戳，显示格式由飞书该列设置决定 |

目标中没有对应列的库存、采集时间、蓝鲸数据日期、尺寸等仍保留在本地 JSON。“更新日期”表示本次写入飞书的时间，即使补写较早的 JSON，也填写本次同步时间。该列不是历史主键：已有商品仍更新原行，不按日期新增行；历史追溯使用每轮 JSON。

支持已有 JSON 补写（不启动蓝鲸浏览器）：

```powershell
# 只读查询并生成本地预览，不修改飞书
.\.venv\Scripts\python.exe -m competitive_tracking write-feishu data/run_你的时间戳.json --dry-run
# 正式写入；会重新读取当前监控表，补齐负责人/竞品并再次核验监控条件
.\.venv\Scripts\python.exe -m competitive_tracking write-feishu data/run_你的时间戳.json
```

同步规则：

- 新商品创建记录，已有商品按商品 ID 找到原 record_id 后更新已知变化字段，并刷新“更新日期”；指标未变化时只更新日期，相同 JSON 重跑不会重复新增。禁用 `updated_at` 映射后，无变化商品不发起更新请求。
- `null`、`--` 或缺失值不清空目标原值；不修改未映射的人工字段，不删除记录。
- 商品采集失败则跳过；`partial` 商品可以写已知字段，缺失值保留。元数据冲突（如单人字段对应多个负责人）会报告，并跳过冲突字段。
- 补写时会按平台+商品 ID 重新读取当前合格监控记录；已经关闭监控或不再满足条件的商品不补写，负责人/竞品采用当前监控表值。
- 根据 `stron_token/feishu_output_*.json` 中最近成功同步的采集时间跳过旧快照。此保护基于当前机器保留的同步状态，不等同于跨机器的远程历史版本控制。
- 创建请求在发送前保存 UUID `client_token`，不确定响应的重试复用同一个 token；更新请求使用固定 record_id。网络/限流按统一配置重试，重复重试不会故意创建新请求身份。
- 每次写入保存 `data/feishu_write_时间戳.json`，包含预览、成功批次、错误及写后读取核对结果。跨多个批次不提供整轮事务回滚；中途失败可补写剩余差异。
- 自动运行的 `run_*.json` 额外包含 `feishu_write` 状态；写失败时整轮标为 `partial`，采集数据保留。

## 写入飞书二维表历史

在 `[feishu_sheets]` 设置 `enabled = true`、链接 `/sheets/` 后的 `spreadsheet_token` 和工作表 `sheet_id`。复用 `[feishu]` 应用凭据；应用需具备电子表格读写权限，并在**该电子表格中添加应用、授予编辑权限**。多维表的授权不会自动授予另一份电子表格访问权限。

默认 `data_start_row = 3`，保留现有两行合并表头。写入前按以下顺序检查 A:Y 共 25 列：

商品ID、商品标题、更新日期、竞品、负责人、价格-BRL、价格-RMB、总销量、7天销量、30天销量、60天销量、90天销量、近30天销售额、7天销量变化环比、30天销量变化环比、转换率、评论数、评分、品牌、卖家名称、店铺类型、BSR、商品链接、商品图片链接、类目路径。

`once` / `serve` 采集后自动追加，也可以补写已有 JSON：

```powershell
.\.venv\Scripts\python.exe -m competitive_tracking write-sheets data/run_你的时间戳.json --dry-run
.\.venv\Scripts\python.exe -m competitive_tracking write-sheets data/run_你的时间戳.json
```

- 每次新采集都追加一行商品历史，即使商品 ID 和数值未变。补写同一个 `run_id` 的同一商品只记录一次，防止网络重试重复追加。
- 扫描 A 列最后有内容的位置，不填补历史中间的空洞。追加前检查整个 A:Y 目标区域；其他列存在人工内容时停止，不覆盖。行数不足时只在末尾增加空白行。
- 更新日期按 `schedule.timezone` 写时间文本；待恢复批次保留原先准备的日期以便核对。负责人写姓名，价格/销量写数值，比例写百分号文本，缺失指标为空；商品图片写链接。
- 仅写已采集到数据的 `ok` / `partial` 商品。历史 JSON 中已有负责人/竞品优先保留，旧文件缺失这些信息时才从当前监控表补齐；不因后来关闭监控而删除过去的采集记录。
- `stron_token/sheets_history_*.json` 保存完成状态与待恢复批次；请保留该文件。写入后读取核对全部字段，支持飞书将链接文本转换为超链接对象。响应丢失时先核对原范围；若整批记录因插入/删除行移动，按全部字段确认唯一匹配位置后恢复，不重复写入。
- 每次生成 `data/sheets_write_时间戳.json` 报告；自动采集结果中包含 `sheets_write` 状态。遇到未确认批次，先执行正式补写恢复，再预览下一批。
- 本地进程锁防止本项目同时运行；飞书的区域检查和写入不是事务，请避免其他程序同时向同一工作表追加。多台机器运行时不能共用这套本地去重保证。

## 发送飞书商品消息

启用 `[feishu_messages].enabled = true`，复用 `[feishu]` 的任务表和应用凭据。发送者是该自建应用的机器人，接收者来自任务表的“数据推送人”，不使用“负责人”代替。

`platforms = ["mercado", "shopee"]` 允许两个平台发送；移除 `shopee` 可单独关闭 Shopee 推送。卡片指标按平台渲染，共用当前任务路由、分批、失败恢复和防重复发送逻辑。自动流程在所有平台的表同步阶段结束后执行消息推送；表同步失败仍保留采集结果并独立尝试推送。

飞书应用需开启机器人能力，开通 **以应用的身份发消息（`im:message:send_as_bot`）**，将接收人纳入应用可用范围，并发布生效。给电子表格添加应用只解决文档权限，不等于拥有消息权限。[飞书发送消息 API](https://open.feishu.cn/document/server-docs/im-v1/message/create)

消息规则：

- 每次发送前读取当前任务表。平台、商品 ID、监控开关、数据推送人仍需满足采集筛选条件，且该记录的推送开关必须为“开启”。旧 JSON 补发同样重新检查。
- 多人字段逐人发送；同一商品出现在多个任务记录中时，同一接收者本批次只收一次。关闭推送的记录不会把它的接收人加入其他开启记录。
- 按接收人汇总，默认每张卡片最多 4 个商品，`products_per_message` 可设置为 1～4。使用加粗标签、分隔线和商品链接，展示实际采集时间、BRL/RMB 价格、销量、销售额、环比、转换率、评价、品牌及卖家。
- Shopee 卡片显示 `shopee · 商品ID - 自定义名称`、完整标题、实际采集时间及查询期间；指标包含 BRL 价格、日/月销量及销售额、评分数、留评率、星级、月新增评分、点赞及月新增点赞、类目排名及近 1/7 天变化、品牌、卖家、变体数、类目路径。金额取页面标准值；留评率不当转换率，月销量不当总销量。
- `[feishu.fields].name` 默认映射任务表的“自定义-商品名”。卡片首行显示 `mercado · 商品ID - 自定义名称`，名称为空时省略后缀；平台原始标题继续单独显示。同一接收人的多个开启任务提供不同名称时用 `/` 连接，其他接收人或关闭任务的名称不混入。
- 每个商品末尾按 `查看商品 ｜ 数据链接 ｜ 历史链接` 排列。Mercado 兼容 `[feishu_messages].data_url` / `history_url`；Shopee 使用 `[feishu_messages.platform_links.shopee]` 下的 `data_url` / `history_url`，不会继承 Mercado 链接。同一张卡片内也逐商品选择平台对应链接。填完整 URL，留空则隐藏；可用 `platform_links.mercado` 显式覆盖 Mercado 链接。
- 新卡片使用当前名称和链接；已发送卡片不自动修改，结果未知的重试仍复用原内容。
- 只发送有商品数据的 `ok` / `partial` 结果；后者附不完整提示。缺失数值为 `—`，有效的 0 正常显示。历史补发不会冒充刚抓取的数据。
- 成功状态按“采集 run_id + 接收人 open_id + 平台商品 ID”保存；同一个 JSON 再执行不重复发送，新采集则可以发送新消息。控制台和本地报告保存接收人、发送数量和 message_id；成功表示飞书接口已接收，不表示人员已读。
- 发送前保存固定 UUID 和卡片内容，超时重试复用它们。对结果未知且超过 45 分钟的批次停止自动重发，需人工核对消息及本地状态；明确返回 99991672 的权限拒绝允许补齐权限后继续。待重试批次的推送条件变化时不发送。
- 待重试批次需要通过其原始 JSON 恢复；新采集不会自动把之前的旧消息一起补发。保留 `stron_token/feishu_messages_*.json` 去重状态，不要多台机器同时推送同一批次。

```powershell
# 读取任务表并生成本地卡片预览，不发送消息
.\.venv\Scripts\python.exe -m competitive_tracking send-feishu data/run_你的时间戳.json --dry-run
# 正式发送或重试原批次
.\.venv\Scripts\python.exe -m competitive_tracking send-feishu data/run_你的时间戳.json
# 仅推送 Shopee；不重新采集，也不重复写两张表
.\.venv\Scripts\python.exe -m competitive_tracking send-feishu data/run_你的时间戳.json --platform shopee --dry-run
.\.venv\Scripts\python.exe -m competitive_tracking send-feishu data/run_你的时间戳.json --platform shopee
```

`--platform` 可重复，只缩小配置中允许的平台范围，不会启用配置中已经关闭的平台。不指定时按配置的平台列表处理输入 JSON。补发旧 JSON 显示其原始采集时间，不冒充新数据。

每次生成 `data/messages_时间戳.json`，预览包含接收人和实际卡片 JSON；真实发送报告另含 message_id、错误及已发送跳过数量。`once` / `serve` 自动结果中新增 `messages` 状态。

## 模块结构

```text
config.example.toml                   # 每个配置项都有中文说明
src/competitive_tracking/
  cli.py                              # once / serve / check-config / parse-html / write-feishu / write-sheets / send-feishu
  config.py                           # TOML、环境变量、配置校验
  models.py                           # TrackingTarget / Source / Collector / Sink 接口
  sources/feishu.py                    # 飞书读取、分页、筛选与目标去重
  integrations/feishu.py               # 飞书鉴权、HTTP 重试、通用 API 分页
  sinks/feishu.py                      # 结果表字段映射、查重、更新、幂等状态和写后核对
  sinks/feishu_sheets.py               # 二维表 25/28 列历史追加、批次恢复和写后核对
  sinks/shopee_fields.py               # Shopee 28 个字段的顺序、类型、标准值映射
  sinks/destinations.py                # 各平台目标配置隔离
  sinks/provision_shopee.py             # 显式创建 Shopee 字段及二维表表头
  sinks/feishu_messages.py             # 当前任务路由、Markdown 卡片、机器人发送与批次去重
  browser.py                          # 专用浏览器、profile、sessionStorage
  platforms/registry.py               # 平台注册表
  platforms/mercado/collector.py       # 登录、收藏、虚拟滚动、搜索、加入分组
  platforms/mercado/parser.py          # 纯 HTML 解析和标准化，可独立测试
  platforms/mercado/canvas.js          # 捕获实际绘制的 Canvas 文字
  platforms/mercado/snapshot.js        # 当前表格 DOM 快照
  platforms/shopee/collector.py        # Shopdora 登录、巴西收藏分页、ID 搜索、加入收藏
  platforms/shopee/parser.py           # 主行/补充行合并、页面字段与接口原值分开保存
  runner.py                           # 调度、平台隔离、运行结果
  storage.py                          # JSON 原子写入、进程锁
tests/                                # 自建脱敏测试数据与单元测试
scripts/browser_smoke.py               # 可选真实浏览器本地集成测试
.github/workflows/ci.yml               # Windows/Linux + Python 3.11/3.12 检查
```

新增平台：实现 `collect(targets)` 返回 `{target.key: result}`，注册到 `COLLECTORS`，并加入配置 `app.platforms`。平台使用独立会话子目录；通用 `BrowserSession` 接收平台自己的来源 URL 和初始化脚本，Mercado 的 Canvas 逻辑由其采集器注入。飞书来源、调度和 JSON 输出不需要改动。

多维表写入由 `FeishuSink` 实现，二维表历史由 `FeishuSheetsSink` 实现，运营消息由 `FeishuMessageSink` 实现；各模块使用标准化商品数据，消息模块独立核对当前任务表的推送条件。

## 验证与离线诊断

```powershell
python -m unittest discover -s tests -v
python scripts/browser_smoke.py
python -m competitive_tracking parse-html "C:\path\蓝鲸选品.html" --product-id MLB6984707226 --output data/offline.json
```

浏览器集成测试只打开自建本地页面，测试临时 profile、Canvas、虚拟行/列、两页收藏、ID 精确匹配、加入收藏弹窗，不访问账户。CI 执行单元测试，浏览器测试需本机安装 Chromium。

正式运行前，用实际飞书表和蓝鲸账号执行 `once`，检查 `latest.json` 的状态、warnings、商品数量和来源。真实站点 DOM、账号权限、所选国家/分组仍需联调；当前保留浏览器中的站点选择，不自动切换国家。多国家监控需确保当前站点能搜索对应 ID。

API/框架参考：[DrissionPage 4 浏览器配置](https://drissionpage.cn/dp40docs/ChromiumPage/browser_opt/)、[页面交互](https://drissionpage.cn/dp40docs/ChromiumPage/page_operation/)、[飞书多维表记录读取](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/list)、[飞书自建应用令牌](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)。

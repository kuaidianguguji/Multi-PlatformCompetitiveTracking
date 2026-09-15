# CompetitiveTracking

Python + DrissionPage 的模块化商品监控项目。当前实现 **飞书多维表读取 → Mercado/蓝鲸商品采集 → 本地 JSON 与日志**。飞书多维表写入、二维表写入、运营消息发送留待下一阶段。

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

`check-config` 仅检查本地配置，不验证远程权限。返回码：0 正常；1 运行失败或部分采集失败；2 配置缺少必要飞书字段；130 用户中断。

## 当前流程

1. 使用飞书 API 自动读取所有记录页，处理访问令牌过期、限流和暂时性网络错误。
2. 对每个平台筛选：平台为 `mercado`、商品 ID 非空、监控开关值恰为“开启”、数据推送人非空。监控字段不把布尔 true 或数字 1 当作开启。
3. 以 `平台:商品ID` 去重，例如 `mercado:MLB6984707226`。同一商品的所有飞书记录、接收人、推送开关保留在 `tracking_records`。推送开关当前只记录，不影响采集；未来发送时逐记录判断。
4. 无合格记录时直接进入下个平台，不启动浏览器。未实现的平台记为 `unsupported` 并继续。
5. 自动创建 `stron_token/mercado/profile`。专用 Chromium profile 保存 Cookie、localStorage 和浏览器会话信息；另外按来源域保存/恢复 sessionStorage。会话过期仍需登录。
6. 打开收藏页，文档加载完成后固定等 10 秒。若跳转 `/login`，点击“注册/登录”，最多等 300 秒，检测到 `/home` 后进入收藏页。
7. 从收藏第一页开始，遍历虚拟滚动的所有行和列，合并固定图片列，自动翻页；找到全部目标后可以提前结束。若仍缺商品，则必须完成收藏扫描，才能进入搜索补全。
8. 对收藏中缺少的 ID 进入搜索页、输入商品 ID、查询。严格对比“商品ID”，不会把被跟卖商品 ID 或第一条不匹配的搜索结果当成命中。
9. 搜索命中后采集商品，点击该行“加入收藏”。等待 2 秒及异步弹窗出现，选择“每日查询分组”，点击“确认”，通过按钮变为“取消收藏”验证成功。已收藏的不重复添加。请提前创建该分组。
10. 输出 JSON，并在控制台和轮转日志中打印每个命中商品。商品错误、收藏失败、数据缺失均留下状态和原因。

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
- 常驻依赖进程和电脑持续运行；Windows 可在“任务计划程序”创建用户登录时启动 `serve` 的任务。首次及过期登录需要交互桌面，请选择“仅当用户登录时运行”，不要无头运行。程序路径为虚拟环境 Python，参数为 `-m competitive_tracking --config "完整配置路径" serve`，起始目录设为项目目录。

`stron_token` 包含敏感登录数据；`config.toml` 包含密钥；`data`/`logs` 可能包含业务数据。它们已被 `.gitignore` 排除。不要手动强制加入 Git。此项目不会发送飞书消息，也不会写入飞书表；蓝鲸“加入收藏”是本阶段已实现的外部写操作。

## 模块结构

```text
config.example.toml                   # 每个配置项都有中文说明
src/competitive_tracking/
  cli.py                              # once / serve / check-config / parse-html
  config.py                           # TOML、环境变量、配置校验
  models.py                           # TrackingTarget / Source / Collector / Sink 接口
  sources/feishu.py                    # 飞书读取、分页、筛选与目标去重
  browser.py                          # 专用浏览器、profile、sessionStorage
  platforms/registry.py               # 平台注册表
  platforms/mercado/collector.py       # 登录、收藏、虚拟滚动、搜索、加入分组
  platforms/mercado/parser.py          # 纯 HTML 解析和标准化，可独立测试
  platforms/mercado/canvas.js          # 捕获实际绘制的 Canvas 文字
  platforms/mercado/snapshot.js        # 当前表格 DOM 快照
  runner.py                           # 调度、平台隔离、运行结果
  storage.py                          # JSON 原子写入、进程锁
tests/                                # 自建脱敏测试数据与单元测试
scripts/browser_smoke.py               # 可选真实浏览器本地集成测试
.github/workflows/ci.yml               # Windows/Linux + Python 3.11/3.12 检查
```

新增平台：实现 `collect(targets)` 返回 `{target.key: result}`，注册到 `COLLECTORS`，并加入配置 `app.platforms`。平台使用独立会话子目录；通用 `BrowserSession` 接收平台自己的来源 URL 和初始化脚本，Mercado 的 Canvas 逻辑由其采集器注入。飞书来源、调度和 JSON 输出不需要改动。

未来飞书写入和消息发送可以实现 `ResultSink`，使用标准化商品数据及 `tracking_records`，分别处理多维表、二维表、推送开关及接收人。

## 验证与离线诊断

```powershell
python -m unittest discover -s tests -v
python scripts/browser_smoke.py
python -m competitive_tracking parse-html "C:\path\蓝鲸选品.html" --product-id MLB6984707226 --output data/offline.json
```

浏览器集成测试只打开自建本地页面，测试临时 profile、Canvas、虚拟行/列、两页收藏、ID 精确匹配、加入收藏弹窗，不访问账户。CI 执行单元测试，浏览器测试需本机安装 Chromium。

正式运行前，用实际飞书表和蓝鲸账号执行 `once`，检查 `latest.json` 的状态、warnings、商品数量和来源。真实站点 DOM、账号权限、所选国家/分组仍需联调；当前保留浏览器中的站点选择，不自动切换国家。多国家监控需确保当前站点能搜索对应 ID。

API/框架参考：[DrissionPage 4 浏览器配置](https://drissionpage.cn/dp40docs/ChromiumPage/browser_opt/)、[页面交互](https://drissionpage.cn/dp40docs/ChromiumPage/page_operation/)、[飞书多维表记录读取](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/list)、[飞书自建应用令牌](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)。

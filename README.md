# 缠序交易工作台 · Chanxu Workbench

缠论结构研究、在线日线、实验买卖信号、历史回测与沪深主板新 B 点扫描。

这是 2026-11-09 的源码备份。业务代码和交易引擎逐字节保留；只新增发布说明、忽略规则和校验清单。**不是云端部署，也不会自动安装原机器的定时通知。**

## 功能

- K 线包含处理、分型、笔、线段/中枢代理、B1/B2/B3 与对应 S 信号。
- 在线历史日线、中文证券名称搜索、图表中文名称、自选股及最新收盘/信号。
- 同一引擎历史回测，确认后下一根开盘尝试成交，结构止损、费用和滑点模拟。
- 北京时间收盘状态判定；沪深主板非 ST 新 B 点扫描；冻结输入、版本哈希和离线审计。

规则版本 `chan-proxy-0.2-causal-events`。严格特征序列线段、递归级别及严格趋势背驰尚未完整实现，详见 [RULES.md](RULES.md)。本项目不连接券商、不下单。

## 运行环境

- 当前完整运行路径为 **Windows**，Python 3.12、Node.js 24；两者加入 PATH。
- Python 业务依赖均为标准库；前端是原生 HTML/CSS/JavaScript，无 npm 构建步骤。
- Node 用于扫描器复用同一 `engine.js` 以及 JS 测试。
- `daily_scan.py` 使用 Windows 文件锁；守护和启动器也有 Windows 依赖，不能当作直接可用的 Linux 云部署版本。
- 在线功能需要能访问 Yahoo Finance、Sina Finance 等现有数据接口。

## 下载与前台启动

```powershell
git clone https://github.com/paopaoluo/chanxu-workbench.git
cd chanxu-workbench
python server.py --port 8765
```

保持终端运行，浏览器打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)。按 Ctrl+C 停止。若旧项目占用 8765，用 `--port 18766`，并打开对应端口；不要结束原项目服务。

服务只监听本机回环地址。GitHub 保存源码不等于网站已托管，手机远程访问、域名、HTTPS 和云端调度需另行部署。

### 首次初始化证券名称目录

```powershell
python -c "from screener_data import load_universe; u=load_universe(); print('stock source total:', u['sourceTotal'])"
python refresh_instruments.py
```

源码不包含个人自选、证券目录缓存或扫描结果。初始化成功后才能获得相应的完整名称搜索目录。

### 扫描最新已收盘交易日

```powershell
python -u daily_scan.py --workers 8
```

等待完成后读取 `daily-reports/`。成功输入支持断点复用，通常不要使用 `--force`。数据未齐备会明确报告缺失，不以旧日信号代替。详见 [DAILY_SCAN.md](DAILY_SCAN.md)。原桌面 16:30/23:00 通知没有包含在源码中。

### 可选 Windows 托管启动

双击 `打开缠序.cmd` 或运行 `启动.ps1`。它会为当前用户和路径注册/启动 Windows 任务，不是无副作用的单次启动脚本。同端口任务若属于另一目录会拒绝覆盖；并存可指定 `启动.ps1 -Port 18766`。

## 文件导航

| 文件 | 作用 |
|---|---|
| index.html / styles.css / app.js | 网页、样式与交互 |
| engine.js | 结构分析、冻结信号与回测 |
| server.py / market_clock.py | 本地服务、行情与已收盘交易日判定 |
| instrument_catalog.py / refresh_instruments.py | 中文证券目录 |
| watchlist_api.py / watchlist_monitor.js | 自选存储与信号 |
| screener_data.py / daily_scan.py / daily_scan_engine.js | 股票池和新 B 点扫描 |
| daily_report_api.py / audit_daily_report.py / replay_saved_inputs.py | 报告读取、审计和输入重放 |
| supervisor.py / 启动.ps1 / 打开缠序.cmd | Windows 服务托管 |
| RULES.md / DATA.md / DAILY_SCAN.md | 规则及数据口径 |
| SOURCE_MANIFEST.json / SHA256SUMS.txt | 原文件和发布文件校验 |
| BACKUP_VERIFICATION.md | 本次备份的真实验证记录 |

## 离线测试

不要直接全目录 unittest 自动发现：`test_server.py` 含固定端口联网烟测。

```powershell
python -B -m unittest test_market_clock test_close_history test_screener_data test_daily_scan test_close_screener_audit test_daily_report_api test_server_regressions test_instrument_catalog test_watchlist_api test_bridge_reliability test_calendar_notice -v
node --test test_engine_regressions.js test_daily_scan_engine.js test_close_ui.js test_daily_list_ui.js test_ui_regressions.js test_symbol_search_ui.js test_watchlist_monitor.js test_watchlist_monitor_ui.js test_watchlist_ui.js
```

`test_engine.js`、`test_server.py`、`test_persistent_service.ps1` 是原环境在线/托管专项检查，不包含在上述离线回归中。前端测试包含模拟 DOM/接口，不等于真实浏览器验收。

`preview-fixture.json` 与 `verification/watch-monitor-20261105/` 下两份明确列出的股票 baseline JSON，是固定公开证券行情测试快照，不是实时数据、个人持仓或自选清单。运行 `python preview_server.py --port 18765` 可启动独立离线预览。

## 数据、隐私与维护

- 原始行情使用未复权口径；正式信号确认日期和极值日期不同，报告按确认日期筛选。
- 未上传用户数据、运行缓存、全市场报告、日志、截图、本机任务 XML、凭据及运行备份。
- [docs/LOCAL_HISTORY.md](docs/LOCAL_HISTORY.md)、TESTING.md、REPAIR_NOTES.md 是经过路径处理的历史记录；所提及的本机证据未完整发布，不表示新机器已有配置。
- 官方休市日历当前覆盖 2026 年，跨年须依据交易所公告维护；未核验年份不能按普通工作日猜测。
- 本次只备份，不增加或改变 B 点条件，不安装自动交易，不进行全市场重扫。

# 在线数据

网页默认通过同一台电脑上的 `server.py` 请求 Yahoo Finance Chart API，不再要求用户手动准备 CSV。浏览器只能访问固定的本地 `/api/history` 路由，服务端不提供任意 URL 代理。

## 启动

在 PowerShell 执行：

```powershell
Set-Location '<项目目录>'
.\启动.ps1
```

然后打开 `http://127.0.0.1:8765/`。服务只绑定 localhost，不接受局域网连接。

## 请求

`GET /api/history?symbol=510300.SS&range=2y`  
可选 `refresh=1` 跳过缓存。`range` 支持 `6mo`、`1y`、`2y`、`5y`，固定日线。

代码示例：`510300.SS`、`000300.SS`、`^SSEC`、`^HSI`、`AAPL`；六位 A 股/ETF 代码也可直接输入，服务会按首位映射到上海或深圳后缀。

## 数据口径

- 来源：Yahoo Finance Chart API。
- OHLC：未复权原始 OHLC；没有把 adjusted close 偷换为 close。
- 采用明确的保守口径：交易所当地日期为今天的 K 线一律剔除，收盘后也不纳入；`meta.completeThrough` 是本次保留数据的截止日，不代表已覆盖最新交易日或实时行情。
- `meta.completenessPolicy=exclude_exchange_today` 和 `meta.completenessNote` 说明上述限制；`fetchedAt` 与 `cacheAgeSeconds` 说明抓取时间与缓存年龄，不能代替行情截止日。
- 网络成功时缓存 15 分钟；网络刷新失败且已有缓存时返回缓存并标记 `meta.stale=true`，没有缓存则返回明确 HTTP 错误，不回退成示例数据。
- 服务端校验 OHLC 关系、正值、非负成交量和严格递增日期；空值不会转为 0。

实时行情、分时数据、复权选择和券商连接不在这一版范围内。

本地 CSV 的当日 K 线完成度需自行核实；引擎只校验日期与 OHLCV 关系，不证明提供方已收盘。币种沿用行情源 `meta.currency`，前端金额不做换汇；CSV 没有币种时明确显示报价单位。切换市场不会自动更改交易单位或 T+1 研究假设。

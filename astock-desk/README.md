# 躬行 Praxis

给 18–25 岁理财新手的金融决策训练场：**不荐股，教判断**。

它用 10 万元虚拟资金，把“看观点”改成“写理由—找反证—做模拟决策—收复盘反馈”。全程不连接券商，不会真实下单。

> 仅用于金融教育与模拟训练，不构成投资建议，不承诺收益。行情和模型输出都可能出错。

## 已实现

- **我的模拟盘**：虚拟资金、持仓估值、灵魂三问、情绪记录、完整决策日志。
- **投研委员会**：基本面、技术面、情绪资金面三个独立视角；首席研判官只讲共识、分歧与待回答问题，不给买入区间、目标价或止损价。
- **多空法庭**：红蓝双方各五条论据，其中一条故意埋入逻辑缺陷，用户判卷后才揭晓。
- **概念图鉴**：术语教学卡可收藏到浏览器本地，形成个人学习进度。
- **复盘信**：引用用户灵魂三问的原话，识别追涨、锚定、确认偏误、处置效应等认知习惯。
- **策略照妖镜**：MA / MACD / RSI 历史回测，与买入持有对照真实收益、最大回撤和夏普比率。
- **多源行情兜底**：腾讯、东方财富、新浪；网络全挂时进入明确标注的“课堂模式（快照数据）”。

## 本地运行

只需要 Python 3.10+，没有第三方 Python 依赖：

```bash
cd astock-desk
python3 server.py
```

浏览器打开 `http://127.0.0.1:8000`。不配置模型 Key 时，行情、搜索、回测和模拟盘仍然可用。

AI 功能通过环境变量配置，Key 不得写进源码：

```bash
export DEEPSEEK_API_KEY="..."
export STEP_API_KEY="..."
python3 server.py
```

可选覆盖项：

```bash
export DEEPSEEK_MODEL_FAST="deepseek-v4-flash"
export DEEPSEEK_MODEL_STRONG="deepseek-v4-pro"
export STEP_MODEL_BACKUP="step-3.5-flash"
export STEP_MODEL_TTS="step-tts-mini"
export ASTOCK_DATA_DIR="/data"
```

DeepSeek 是主通道，阶跃星辰是自动备用通道和语音复盘提供方。`GET /api/diag` 可检查连通性，但不会返回任何 Key 片段。

## Zeabur 部署

本仓库根目录已有 `Dockerfile`。

1. Zeabur 新建项目，区域选择 **阿里云杭州 2C4G**。
2. 从 GitHub 部署 `Simon-byte-png/astock-ai-desk`，构建方式选择 Dockerfile。
3. Variables 配置 `DEEPSEEK_API_KEY`、`STEP_API_KEY`，以及 **`ASTOCK_PUBLIC_ORIGIN=https://你的实际访问域名`**（包括非默认端口，不含路径）。浏览器来源和 Host 必须与此一致，反向代理需保留 Host；未配置时仅允许 localhost / 127.0.0.1 / ::1 的 API 请求。HTTPS 域名会自动启用 Secure 会话 Cookie。
4. 建议挂载持久卷到 `/data`；镜像已默认配置 `ASTOCK_DATA_DIR=/data`。容器以 UID 10001 运行，挂载目录须允许该用户读写。不挂载时，容器重建会清空模拟盘。
5. 健康检查路径使用 `/health`。

服务会自动读取 Zeabur 注入的 `$PORT`。

## 课堂快照

联网环境下运行：

```bash
cd astock-desk
python3 scripts/build_snapshot.py
```

脚本会为 30 只演示股票生成 `data/snapshot.json`。快照只负责黑客松现场断网兜底，前端会明确显示时间和“课堂模式”，不会伪装成实时行情。

## 代码结构

| 文件 | 职责 |
|---|---|
| `lib/market.py` | 行情、K 线、财务、资金流、新闻、搜索、指标和快照兜底 |
| `lib/backtest.py` | 三种教学策略回测与买入持有对照 |
| `lib/llm.py` | DeepSeek 主通道 + 阶跃备用通道、JSON、流式响应、TTS |
| `lib/agents.py` | 委员会、三问审核、多空法庭、术语教学和复盘信 |
| `lib/portfolio.py` | SQLite 模拟盘和决策日志 |
| `server.py` | Python 标准库 HTTP 服务、JSON API 与 SSE 进度 |
| `web/index.html` | 原生 HTML / CSS / JavaScript 五页签界面 |

## 安全边界

- 公开仓库曾出现过的模型 Token 必须在发放平台**撤销并轮换**。删掉源码中的 Token 不能让旧 Token 自动失效。
- 服务端不保存用户姓名，浏览器使用匿名随机 ID 关联本地模拟盘。
- 法庭答案只保存在服务器内存，提交判决前不会下发到前端。
- 这不是撮合系统：模拟成交直接使用当前报价，不处理滑点、涨跌停排队和 T+1，不能用于评估实盘执行效果。

## 本次加固后的运行约定

- 本地默认只监听 `127.0.0.1`。容器由 Dockerfile 设置为 `0.0.0.0`，公网访问必须填写上面的 `ASTOCK_PUBLIC_ORIGIN`。
- 行情连接验证 HTTPS 证书；模型连接必须使用 HTTPS，不跟随重定向转发密钥。上游错误正文不返回浏览器。
- API 拒绝跨站来源。POST 必须提交 `application/json`、唯一的 Content-Length，请求体上限 64 KiB；异常请求会关闭连接。
- 默认每个连接来源每分钟最多 180 次 API 请求，其中 AI 入口最多 12 次；全站 AI 入口每小时最多 120 次，可通过 `ASTOCK_AI_HOURLY_LIMIT` 调整，设为 0 禁用 AI 入口。AI 同时处理最多 4 个请求，总 HTTP 连接线程最多 32 个。该上限按入口请求计数，一个委员会请求仍可能多次调用模型，**不是账单金额上限**。
- 不信任客户端的 `X-Forwarded-For`。部署在反向代理后，同一代理转来的访问共享来源限额。限额保存在单进程内存中，重启会清空；多实例部署须另设统一的网关限流、身份认证及服务商消费额度。
- 模拟盘仍是匿名浏览器会话，没有登录、跨设备同步或账号恢复。Cookie 是访问凭据，不要转发；用户 ID 不再出现在 API JSON 中，客户端不能自选新会话 ID。清除 Cookie 后不会自动找回原模拟盘。
- 公网长期开放时，应在反向代理加访问认证。当前防护限制资源消耗，不代表匿名 AI 服务已经具备完整的用户授权和费用控制。

### 模拟订单重试

网页为每次下单生成一个 `request_id`。在同一个窗口里因网络问题重试，会使用相同编号，服务端只成交一次；同编号但内容不同的请求会被拒绝。提交结果不确定时，先刷新持仓确认，再决定是否重新打开下单窗口。

直接调用 API 时，先 `GET /api/portfolio` 保存会话 Cookie，再提交 `POST /api/order`；`request_id` 必填，为 16–80 位字母、数字、下划线或短横线。同一用户的编号及成交结果持久化到 SQLite，与现金、持仓、日志在一个事务内写入。已有数据库会自动添加新表，不删除旧数据。

### 回测口径

回测改为使用前一日收盘信号，在次日收盘执行。最后一天只结算、不新开仓；策略和买入持有基准均扣除费用，胜率按扣费后的完整交易统计，收益和回撤包含初始费用。新浪与东方财富默认均使用不复权日线，课堂快照会显式标记。

佣金 0.025%、卖出税费 0.1% 是现有教学参数，并非现行收费说明。回测仍忽略整手、最低佣金、分红、滑点、涨跌停和停牌执行限制，不等同于真实可成交收益。

## 验证

在仓库根目录执行（前端测试需要 Node.js 22+，服务运行不需要 Node.js）：

```bash
python3 -m unittest discover -s astock-desk/tests -v
node --test astock-desk/tests/frontend.test.cjs
```

测试不使用真实模型密钥、不发起真实交易、不联网拉行情。GitHub Actions 会在 Python 3.10 / 3.12 上运行检查。

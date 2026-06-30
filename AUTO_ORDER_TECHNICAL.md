# 12306 自动抢票下单技术文档

本文档说明本项目中“自动抢票下单”能力的实现方式、关键模块、接口、运行流程和故障排查。该能力只负责自动占座并生成 12306 待支付订单，不做自动支付。

## 当前状态

- 已支持后台轮询余票，命中指定席别后自动提交占座。
- 已支持任务持久化，服务重启后可恢复 `running` 任务。
- 已支持任务 `开始/继续`、`停止`、`删除`。
- 已支持通过页面打开官方 12306 Chrome 登录页，并一键导入网页登录态 Cookie。
- 已完成真实有票场景验证：命中可购车次后成功生成待支付订单。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| `app.py` | Flask API 入口，处理登录、乘客、下单任务管理等 HTTP 接口。 |
| `templates/index.html` | 页面交互，包含自动抢票配置、官方网页登录态导入、任务列表、日志查看。 |
| `ticket.py` | 12306 匿名余票查询、站点字典、车次/席别解析、买长乘短查询。 |
| `order12306.py` | 登录态管理和真实下单链路：提交订单、确认页解析、排队、占座、查订单号。 |
| `order_service.py` | 后台自动抢票任务管理器，每个任务一个线程。 |
| `cryptobox.py` | 本地敏感字段加密，密钥保存在 gitignore 的 `.secret.key`。 |
| `persist.py` | JSON 防抖落盘与 `0600` 权限原子写。 |
| `notify.py` | 微信/企业微信等通知发送。 |
| `import_12306_cookies.py` | 命令行辅助脚本，从 `/tmp/qp_12306_cookies.json` 导入官方 Chrome Cookie 到本地登录会话。 |
| `login_session.json` | 本地 12306 登录态持久化文件，敏感文件，不应提交。 |
| `order_jobs.json` | 抢票任务持久化文件，包含乘车人快照和通知配置，敏感文件，不应提交。 |

## 架构概览

自动下单链路分为三层：

1. 前端页面
   - 用户填写出发地、目的地、日期、车次、席别、乘车人和通知配置。
   - 调用 `/api/order/create` 创建后台任务。
   - 轮询 `/api/order/list` 和 `/api/order/<id>` 展示状态和日志。

2. 后台任务层
   - `order_service.OrderJob` 每个任务一个 daemon 线程。
   - 周期性调用 `ticket.query_tickets()` 查询余票。
   - 命中可购席别后调用 `order12306.LOGIN.submit_order()` 真实占座。
   - 成功后将任务置为 `done`，发送通知，并停止轮询。

3. 12306 下单层
   - `order12306.LoginSession` 持有独立的 `requests.Session` 和 Cookie。
   - 下单前校验登录态，刷新乘车人信息和 `allEncStr`。
   - 依次调用 12306 网页端接口完成占座。

## 登录态方案

主登录流程已经改为“官方网页登录 + 导入 Cookie”。页面内置二维码登录接口仍保留在实验入口，但不作为主流程。

原因是 12306 当前对网页登录依赖设备指纹 Cookie，例如 `RAIL_DEVICEID`、`RAIL_EXPIRATION`。本地直接生成二维码时容易出现 `uamtk票据内容为空` 或跳转错误页；官方页面登录可以由 12306 自己完成设备指纹和会话初始化。

页面主流程：

1. 点击“打开官方登录页”。
2. 系统启动带远程调试端口的独立 Chrome：

```bash
open -na "Google Chrome" --args \
  --remote-debugging-port=9222 \
  --remote-debugging-address=127.0.0.1 \
  --user-data-dir=/tmp/qp-chrome-12306 \
  --no-first-run \
  --new-window \
  https://kyfw.12306.cn/otn/resources/login.html
```

3. 用户在官方 12306 页面用 App 扫码并确认。
4. 回到本项目页面点击“导入登录态”。
5. 后端通过 Chrome DevTools Protocol 读取 12306 Cookie，导入 `order12306.LOGIN`，并保存到 `login_session.json`。
6. 后端调用 `check_online()` 验证登录态，成功后前端加载乘车人。

页面接口导入不会把 Cookie 值返回给前端，只返回：

- 是否登录成功
- Cookie 数量
- 简短状态消息

命令行辅助流程仍可使用。用户扫码登录成功后，通过 Chrome DevTools Protocol 导出 12306 Cookie 到：

```text
/tmp/qp_12306_cookies.json
```

然后执行：

```bash
python3 import_12306_cookies.py
```

导入脚本会：

- 读取 Chrome 导出的 Cookie。
- 按 `name/value/domain/path` 保存到 `login_session.json`。
- 调用 `check_online()` 验证登录态。
- 输出 `{"ok": true, "cookie_count": ...}` 表示导入成功。

注意：`login_session.json` 以前只保存扁平 Cookie dict，容易丢失 domain/path。现在已升级为 Cookie 列表格式，并兼容旧格式读取。

## 后端 API

### 登录相关

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/order/login/official/open` | 打开官方 12306 Chrome 登录页，主登录入口。 |
| `POST` | `/api/order/login/official/import` | 从官方 Chrome 调试端口读取并导入 12306 Cookie，主导入入口。 |
| `POST` | `/api/order/login/qr` | 创建页面内二维码登录，实验入口。 |
| `POST` | `/api/order/login/status` | 查询页面内二维码扫码状态，实验入口。 |
| `GET` | `/api/order/login/check` | 检查当前 Cookie 是否仍为登录态。 |
| `POST` | `/api/order/logout` | 清空本地登录态。 |
| `GET` | `/api/order/passengers` | 拉取乘车人列表，前端只暴露脱敏证件号和临时 token。 |

### 任务相关

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/order/create` | 创建并启动抢票任务。 |
| `GET` | `/api/order/list` | 获取任务列表。 |
| `GET` | `/api/order/<id>` | 获取任务详情和日志。 |
| `POST` | `/api/order/start` | 继续已停止或出错的任务。 |
| `POST` | `/api/order/stop` | 停止任务。 |
| `POST` | `/api/order/delete` | 删除任务。 |

`/api/order/create` 会重新拉取真实乘车人列表，通过前端乘车人 token 匹配后，把下单所需字段写入任务配置。

## 乘车人安全处理

前端不再保存或暴露完整证件号。实现方式：

- `app.py` 用进程级随机盐生成乘车人 HMAC token。
- `/api/order/passengers` 返回：
  - 姓名
  - 脱敏证件号
  - 证件类型
  - 乘车人类型
  - token
- `/api/order/create` 根据 token 从后端实时乘车人列表中反查真实乘车人。

后台任务只持久化乘车人精简快照（姓名、证件类型、乘车人类型、脱敏证件号），下单前会重新拉取当前登录态下的乘车人信息补全 `allEncStr`。因此 `order_jobs.json` 仍是敏感文件，但不再落盘明文证件号、手机号或 `allEncStr`。

## 自动抢票任务状态机

任务状态：

| 状态 | 含义 |
| --- | --- |
| `running` | 正在后台轮询。 |
| `stopped` | 用户停止或服务恢复后保留的停止态。 |
| `error` | 任务出错，可点“继续”重试。 |
| `done` | 已成功占座并拿到待支付订单提示。 |

主要字段：

- `cycle`：轮询轮次。
- `last_check`：最后检查时间。
- `last_error`：最后错误。
- `last_msg`：最后状态消息。
- `order_info`：占座成功后的订单提示。
- `log`：最近日志，最多保留 `_LOG_MAX` 条。

服务启动时 `OrderManager._load()` 会读取 `order_jobs.json`：

- `running` 任务自动重新启动。
- `done/stopped/error` 任务只恢复展示状态，不自动重试。

## 候选车次选择逻辑

入口：`OrderJob._candidate_result(date)`

流程：

1. 将出发站/到达站转为 12306 站码。
2. 调用 `ticket.query_tickets(from_code, to_code, date)` 查询直达余票。
3. 按车次类型过滤。
4. 按指定车次过滤。
5. 按用户勾选席别顺序寻找第一个有票席别。
6. 若启用买长乘短，则继续查延伸区段。

当前任务日志会记录每轮摘要，例如：

```text
第 3 轮：2026-07-01 查到 1 趟，暂无可购 硬卧
第 13 轮：2026-07-03 命中 1 个可购候选
```

这用于区分“没有运行”和“运行正常但没票”。

## 真实下单流程

入口：`order12306.LOGIN.submit_order(...)`

完整顺序：

1. `leftTicket/submitOrderRequest`
   - 提交 `secretStr`、日期、出发/到达站名等参数。
   - 建立确认订单上下文。

2. `confirmPassenger/initDc`
   - 进入确认页。
   - 从 HTML/JS 中解析：
     - `globalRepeatSubmitToken`
     - `key_check_isChange`
     - `leftTicketStr`
     - `train_no`
     - `station_train_code`
     - `train_location`
     - `from_station_telecode`
     - `to_station_telecode`

3. `confirmPassenger/checkOrderInfo`
   - 拼接并提交：
     - `passengerTicketStr`
     - `oldPassengerStr`
   - 校验乘车人、席别、订单合法性。

4. `confirmPassenger/getQueueCount`
   - 查询排队人数和剩余票。
   - `train_date` 使用 GMT+0800 格式。

5. `confirmPassenger/confirmSingleForQueue`
   - 真正提交占座。

6. `confirmPassenger/queryOrderWaitTime`
   - 轮询出票结果。
   - 拿到 `orderId` 后认为占座成功。

7. `confirmPassenger/resultOrderForDcQueue`
   - 最终确认订单结果。
   - 即使最终确认接口异常，只要已拿到订单号，也会提示用户去 12306 App 查看。

成功提示格式：

```text
占座成功！订单号 <order_id>，请尽快到 12306 App 付款
```

## 请求头和 Cookie 处理

下单接口使用接近 12306 网页端的 AJAX 请求头：

- `Referer`
- `Origin`
- `X-Requested-With: XMLHttpRequest`
- `Accept: application/json, text/javascript, */*; q=0.01`

错误响应解析已增强：

- JSON 正常返回时按接口字段解析。
- 非 JSON 返回时，会提取 HTML 文本摘要。
- 跳转响应会显示 `HTTP code -> Location`。
- 这可以定位“登录失效”“被风控跳转”“接口返回首页”等问题。

## 席别编码

`order12306.SEAT_TYPE_CODE` 映射：

| 席别 | seatType |
| --- | --- |
| 商务座 | `9` |
| 一等座 | `M` |
| 二等座 | `O` |
| 高级软卧 | `6` |
| 软卧 | `4` |
| 动卧 | `F` |
| 硬卧 | `3` |
| 软座 | `2` |
| 硬座 | `1` |
| 无座 | `1` |

无座与硬座使用同一 `seatType=1`，由 12306 后端按车次和余票规则处理。

## 运行和验证

启动服务：

```bash
bash start.sh
```

打开页面：

```text
http://127.0.0.1:5001
```

检查登录态：

```bash
curl -s http://127.0.0.1:5001/api/order/login/check
```

打开官方 12306 登录页：

```bash
curl -s -X POST http://127.0.0.1:5001/api/order/login/official/open
```

导入官方网页登录态：

```bash
curl -s -X POST http://127.0.0.1:5001/api/order/login/official/import
```

检查乘车人：

```bash
curl -s http://127.0.0.1:5001/api/order/passengers
```

检查任务：

```bash
curl -s http://127.0.0.1:5001/api/order/list
curl -s http://127.0.0.1:5001/api/order/<job_id>
```

继续任务：

```bash
curl -s -X POST http://127.0.0.1:5001/api/order/start \
  -H 'Content-Type: application/json' \
  -d '{"id":"<job_id>"}'
```

停止任务：

```bash
curl -s -X POST http://127.0.0.1:5001/api/order/stop \
  -H 'Content-Type: application/json' \
  -d '{"id":"<job_id>"}'
```

## 常见问题

### 页面显示已登录，但下单接口返回未登录首页

原因通常是 Cookie 过期、官方网页端已退出、或 Cookie 保存时丢失 domain/path。

处理：

1. 在官方 Chrome 页面内确认登录。
2. 回到本项目页面点击“导入登录态”。
3. 再调用 `/api/order/login/check` 或页面“检查登录”验证。
4. 若仍失败，重新点击“打开官方登录页”扫码后再导入。

### 内置二维码扫码后失败

可能原因：

- 缺少 12306 设备指纹 Cookie。
- `auth/uamtk` 被跳转到错误页。
- `uamauthclient` 返回 `uamtk票据内容为空`。

处理：

- 使用页面主流程：“打开官方登录页”扫码，再点击“导入登录态”。

### 命中可购但提示乘车人信息失效

原因：

- 12306 返回的乘车人 `allEncStr` 可能会变化。
- 旧任务中的乘车人快照与当前登录态不匹配。

当前修复：

- 下单前会重新拉取乘车人。
- 匹配时优先用 `allEncStr`、证件号，最后用姓名+证件类型+乘车人类型兜底。

### 日志显示暂无可购

表示任务正常运行，只是当前没有匹配席别。

示例：

```text
第 10 轮：2026-07-01 查到 1 趟，暂无可购 硬卧
```

### 任务成功后需要做什么

程序只占座，不付款。成功后必须到 12306 App 或官网处理待支付订单：

- 要票：及时付款。
- 测试单：及时取消。

## 安全注意事项

以下文件包含敏感信息，不应提交到代码仓库：

- `login_session.json`
- `order_jobs.json`
- `monitor_jobs.json`
- `.app_token`
- `.secret.key`
- `/tmp/qp_12306_cookies.json`

当前落盘策略：

- `login_session.json`：安装 `cryptography` 时整体加密，文件权限 `0600`。
- `order_jobs.json`：保存乘车人姓名、证件类型、乘车人类型、脱敏证件号、任务日志和订单状态；推送 token 字段加密，文件权限 `0600`。
- `monitor_jobs.json`：保存监控任务和命中记录；推送 token 字段加密，文件权限 `0600`。
- `.secret.key`：本地加密密钥，泄露后可解密本机密文，文件权限 `0600`。
- `.app_token`：本服务访问令牌，泄露后等同接口鉴权失效，文件权限 `0600`。

建议在 `.gitignore` 中忽略这些文件。

## 已知限制

- 12306 接口不是公开 API，字段和风控策略可能随时变化。
- 官方网页登录态可能短时间失效，需要重新导入 Cookie。
- 内置二维码登录依赖设备指纹，目前不作为主流程。
- 自动下单只生成待支付订单，不处理支付。
- 同一账号同一时间只应跑少量任务，避免触发风控或重复占座。

# 轻量 Python 邮件助手

功能：周期读取未读邮件，交给 AI 分类和摘要；重要邮件通知到固定联系人；每天定时发送摘要；也可以根据你给定的任务主动写信并发送到指定联系人。

使用 IMAP/SMTP，支持 QQ、163 等邮箱。Python 3.11+。不需要 Redis、数据库服务器或 Web 服务。这个目录与工作区其他项目独立。

## 1. 准备邮箱

在邮箱网页版开启 IMAP/SMTP，并生成**客户端授权码**。`MAIL_PASSWORD` 填授权码，不填网页登录密码。

| 个人邮箱 | IMAP（SSL） | SMTP（SSL） |
| --- | --- | --- |
| QQ | `imap.qq.com:993` | `smtp.qq.com:465` |
| 163 | `imap.163.com:993` | `smtp.163.com:465` |

企业邮箱或自定义域名应使用管理员提供的服务器设置。模板还支持 SMTP STARTTLS；端口按提供商要求填写。IMAP 只使用 SSL。163 等提供商可能要求 IMAP ID，代码会在服务器声明支持时发送客户端标识，再选择文件夹。

## 2. 安装

在本目录打开 PowerShell。使用官方 Python 或 MSYS Python 均可，下面自动选择虚拟环境中的解释器路径：

```powershell
python -m venv .venv
$AgentPython = if (Test-Path .\.venv\Scripts\python.exe) { '.\.venv\Scripts\python.exe' } else { '.\.venv\bin\python.exe' }
& $AgentPython -m pip install -r requirements.lock.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Linux/macOS 使用 `.venv/bin/python`。如果开启新终端，需要重新设置 `$AgentPython`。`requirements.lock.txt` 记录本次测试使用的版本；有意升级依赖时再使用 `requirements.txt` 中的版本范围并重新测试。

## 3. 填写配置

只编辑本机 `.env`，不要把授权码或 API Key 发到聊天中。配置中必须替换以下占位内容：

```dotenv
MAIL_USERNAME=你的完整邮箱地址
MAIL_PASSWORD=邮箱客户端授权码
CONTACTS_JSON='{"owner":"接收通知的完整邮箱地址","colleague":"另一个允许的邮箱地址"}'
LLM_API_KEY=模型服务的API密钥
```

QQ 用户可以保留默认服务器。163 用户修改：

```dotenv
MAIL_USERNAME=your_account@163.com
IMAP_HOST=imap.163.com
SMTP_HOST=smtp.163.com
```

模型示例使用 DeepSeek 的 Chat Completions 和 JSON 输出。默认模型为官方当前文档推荐的 `deepseek-flash`，请确认你的账号支持该模型，之后也可替换为服务商控制台提供的模型 ID：

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=你的模型API密钥
LLM_MODEL=deepseek-flash
LLM_JSON_MODE=true
LLM_EXTRA_JSON='{"thinking":{"type":"disabled"}}'
```

模板直接发 HTTP 请求，不依赖某个模型厂商 SDK。其他兼容服务要自行核对 `/chat/completions`、`max_tokens`、JSON 模式和返回格式是否支持；`LLM_BASE_URL` 包含服务商要求的 `/v1` 等前缀，但**不要**包含 `/chat/completions`。其他服务通常应将 `LLM_EXTRA_JSON` 改为 `{}`；不支持 JSON 模式时可设置 `LLM_JSON_MODE=false`，模型仍必须返回纯 JSON。程序会验证响应字段和类型，不接受模型自定的收件人。

使用本地兼容服务时，可以填 `LLM_BASE_URL=http://localhost:11434/v1`，模型名填已安装模型，并按服务要求设置密钥；不要求密钥的本地服务可以填 `local`。这只是接入形式，未对所有兼容服务做实测。

行为设置：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `DRY_RUN` | `true` | 保存 `.eml` 预览，不发送邮件；仍读取邮箱并把选中的正文发给模型 |
| `CONTACTS_JSON` | `owner` 示例 | 允许接收邮件的联系人名单，程序限定收件人，无抄送/密送 |
| `NOTIFY_CONTACT` | `owner` | 重要邮件提醒发给谁 |
| `DIGEST_CONTACT` | `owner` | 定时摘要发给谁 |
| `POLL_SECONDS` | `60` | 检查未读邮件的间隔 |
| `LOOKBACK_DAYS` | `1` | 从一天前的本地零点起查询未读；`0` 表示所有未读 |
| `MAX_PER_POLL` | `20` | 单轮最多处理多少封尚未处理的未读邮件 |
| `MAX_BODY_CHARS` | `8000` | 每封邮件交给模型的正文最大字符数 |
| `NOTIFY_IMPORTANT` | `true` | AI 判断重要后自动生成提醒 |
| `IMPORTANCE_RULE` | 见示例 | 用自然语言定义什么邮件重要 |
| `DIGEST_ENABLED` | `true` | 是否每天发送摘要 |
| `DIGEST_TIME` | `18:00` | 每天发送摘要的时间 |
| `TIMEZONE` | `Asia/Shanghai` | 上述时间的时区 |
| `DIGEST_INSTRUCTION` | 中文摘要 | 定时邮件的写作要求 |

例如，只关心项目进展与截止时间：

```dotenv
IMPORTANCE_RULE=项目进展、需要确认的问题、未来7天内截止的事项视为重要；广告、订阅和营销内容不重要。
DIGEST_TIME=20:30
DIGEST_INSTRUCTION=汇总本日已处理邮件，列出项目进展、待回复问题、截止时间；未明确给出的信息写为待确认。
```

## 4. 先检查，再试运行

```powershell
# 只测试登录和只读打开文件夹，绝不发信。
& $AgentPython agent.py check

# 再测试模型接口，只发送一条简短测试消息，不发送邮件正文。
& $AgentPython agent.py check --ai

# 默认 DRY_RUN=true：处理一批未读邮件，并生成重要邮件提醒预览。
& $AgentPython agent.py once

# 手动生成当天摘要预览，不受 DIGEST_ENABLED 开关影响。
& $AgentPython agent.py digest

# 查看处理数量与发件任务状态。
& $AgentPython agent.py status
```

预览在 `data/previews/*.eml`，可用邮件客户端或文本编辑器打开。读取使用只读文件夹和 `mark_seen=False`，不会把邮件标记为已读。已处理邮件在本地数据库去重，以邮箱服务器、端口、账号、文件夹、UIDVALIDITY、UID 作为身份。

`preview.sqlite3` 与真实发送的 `agent.sqlite3` 独立。改为真实模式后，搜索范围内的未读邮件会重新处理并按规则发送提醒；若邮箱中有很多历史未读，先减小日期范围或关闭 `NOTIFY_IMPORTANT`。预览本身不会直接作为真实待发任务补发。

## 5. 启用真实发送和常驻运行

确认收件人、预览和处理范围后，将 `.env` 中改为：

```dotenv
DRY_RUN=false
```

先发送一封你明确提供内容的测试邮件：

```powershell
& $AgentPython agent.py send --contact owner --key first-test --task '写一封简短测试邮件，说明邮件助手已配置完成，无需回复。'
```

`--contact` 只能使用 `CONTACTS_JSON` 中的名字。`--key` 是这件发送任务的唯一标识；同一模式重复使用同一个 key，不会重复生成或发送。要发第二封测试邮件，换一个 key。

然后常驻：

```powershell
& $AgentPython agent.py run
```

启动后立即检查一轮未读，此后按间隔运行；每天按配置时间生成摘要。`Ctrl+C` 停止。同一时刻只允许一个实例，正在运行时先停止，才能执行 `status`、`digest` 等其他命令。

每天的摘要汇总**本日处理成功的来信**，最多100封，不代表整个邮箱；无可用记录时发送明确的空摘要提示。这个版本只在进程运行时调度，关机错过的摘要不会在重启后自动补发。后台巡检出错会写日志并在下一轮继续尝试；单次 CLI 命令仍应同时核对日志和 `status`，不要只看退出码。

Windows 可以通过“任务计划程序”保持运行：触发器选登录时，程序选虚拟环境的 `python.exe` 完整路径，参数填 `agent.py run`，起始于填本项目目录；配置失败后重启任务，并选择已有实例运行时不启动新实例。睡眠或关机期间不会工作。需要全天运行时，把同一目录部署到长期在线的主机，使用其进程管理器管理 `agent.py run`。

## 6. 发送状态和异常

- `pending`：待发，后续处理命令可以继续发送。
- `preview`：已保存预览。
- `sent`：SMTP 服务器已接受，不保证最终送达或进入收件箱。
- `uncertain`：发送过程中断，可能已经送出；不会自动重发。
- `blocked`：收件人被拒绝或已移出允许名单。

对于 `uncertain`，先通过目标收件箱或服务商记录确认。SMTP 直接投递不一定自动保存到发件箱，不能仅凭发件箱没有邮件就认定未发送。确认后，使用 `status` 显示的完整任务 key：

```powershell
# 确认已经发送：只修正本地记录。
& $AgentPython agent.py resolve --key manual:first-test --result sent

# 确认需要重试：标为 pending，下一次处理命令再发送。
& $AgentPython agent.py resolve --key manual:first-test --result retry
```

这是减少重复发送的措施，不是 SMTP 的严格“恰好一次”保证。模板不会自动回复原发件人；只给配置中的联系人发通知、摘要或你明确发起的写信任务。跳过自身来信、带自动生成标记的来信和本助手发出的邮件，以减少邮件循环。附件不分析、不执行，HTML 不加载外部资源或访问链接。

本地 SQLite 中包含摘要和待发正文，预览中也有邮件内容；请按个人邮箱资料保管。配置中的密钥、原始邮件正文和模型原始响应不会写入常规日志；错误日志仅记录异常类型或 HTTP 状态码。

## 7. 测试和核查来源

```powershell
& $AgentPython -m unittest discover -s tests -v
```

测试替代邮箱和模型网络调用，覆盖去重、未读批次、收件人限制、试运行、发送不确定状态和恢复；不连接真实邮箱，不发信。

配置参考：

- QQ 邮箱帮助（授权码）：https://help.mail.qq.com/detail/106/985
- 网易帮助（客户端授权码）：https://help.mail.163.com/faq.do?m=list&categoryID=197
- Huawei Cloud WeLink 帮助（QQ、163 服务器配置）：https://support.huaweicloud.com/faq-welink/faq-welink.pdf
- imap_tools 官方仓库：https://github.com/ikvk/imap_tools
- DeepSeek API 首次调用：https://api-docs.deepseek.com/
- DeepSeek JSON 输出：https://api-docs.deepseek.com/guides/json_mode

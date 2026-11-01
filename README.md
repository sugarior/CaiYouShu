# CaiYouShu：邮件采购 Agent

当前版本使用 `config.yml`，邮件正文、表格提取内容、任务、增量游标、分析结果和发件队列均为 JSON。支持 CSV/TSV、XLSX、XLS 附件的内存解析并交给 DeepSeek。自带本机邮件工作台，无需数据库服务器、Redis、Node.js 或桌面 Excel。旧版 `.env`/SQLite 实现保存在 `legacy/`，与当前服务无关。

另已接入采购SQLite数据库和自然语言控制：控制邮箱直接写“请按附件请购单发起询价”，自动匹配供应商并按统一模板逐家发信；也可直接询问报价、请购单和询价进度，或通过邮件向模型提问。供应商回复填好的附件后自动校验报价入库。使用步骤见 [templates/使用说明.md](templates/使用说明.md)，数据库结构见 [DATABASE.md](DATABASE.md)。

云端演示和本地启动体验方式见 [运行说明.md](运行说明.md)。

## 本地三账号演示

运行 `.venv` 中的 Python：

```powershell
$AgentPython = if (Test-Path .\.venv\Scripts\python.exe) { '.\.venv\Scripts\python.exe' } else { '.\.venv\bin\python.exe' }
& $AgentPython multiuser.py
```

浏览器打开 `http://127.0.0.1:8788`。首次启动输入一次平台共用的 DeepSeek API Key；之后最多可注册 3 个账号。每个账号分别设置登录邮箱/密码、真实 Agent 邮箱及客户端授权码、1 到 3 个控制邮箱。注册和修改邮箱时会实际验证 IMAP 与 SMTP 登录。

新账号自动获得独立的 Agent 进程、邮件归档和采购 SQLite。初始业务数据采用原 `data/procurement.sqlite3` 的版本化快照：100 家供应商、32 个物料组、133 条供货关系、7 个状态、180 条请购单；报价单和订单当前为空。原物料组、供应商和请购单编号保持一致。原快照中 90 条已待报价但无报价或订单的请购单恢复为待询价，方便重新演示；原 `data/` 不变，历史询价和发件队列不复制。

供应商邮箱默认使用 `xyinchina@163.com`，注册或邮箱设置页可更改；也可从控制邮箱发送“请把全部供应商邮箱统一改为 name@example.com”，该动作只批量修改本账号现有供应商的邮箱并写入审计记录。新演示运行数据保存在被 Git 忽略的 `demo-data/`，各账号后续修改相互独立。若原有 `config.yml` 存在，平台保留原文件，并将原邮箱、授权码、控制邮箱和 DeepSeek 模型设置复制到一个待认领账号。

旧版简化演示库在平台重启时自动备份并迁移；用户已修改或已被业务引用的旧样例保留，历史邮件与实际询价不会重放。快照维护和迁移说明见 [demo-seed/README.md](demo-seed/README.md)。

平台进程只监听本机回环地址。服务器部署时使用 `deploy/` 中的 Nginx 配置，通过 HTTPS 代理开放公网访问；不要把 8788 端口直接暴露到公网。关闭平台进程时会停止它管理的演示 Agent。

### Ubuntu 公网 HTTPS

服务单元为 `deploy/caiyoushu-platform.service`，假设项目位于 `/home/ubuntu/CaiYouShu`。公网 IP 部署使用支持 IP 证书的 Certbot 5.4+ 和短期证书，安全组只需开放 TCP 80/443；证书需要由 Certbot 定时续期。

先安装 Nginx 与 Certbot，将 `deploy/nginx-caiyoushu-bootstrap.conf` 放入 `/etc/nginx/sites-available/caiyoushu` 并启用，创建 `/var/www/caiyoushu-acme`，确认 Nginx 启动后申请证书：

```sh
sudo certbot certonly --webroot -w /var/www/caiyoushu-acme --ip-address 81.69.224.197 --preferred-profile shortlived --non-interactive --agree-tos --register-unsafely-without-email
```

随后用 `deploy/nginx-caiyoushu.conf` 替换 bootstrap 配置，并安装 `deploy/certbot-reload-nginx.sh` 至 `/etc/letsencrypt/renewal-hooks/deploy/` 且设置可执行。启用 `caiyoushu-platform.service` 并停用旧的 `caiyoushu-agent.service`。`config.yml`、`data/` 和 `demo-data/` 不纳入发布包。

## 填写配置

编辑本机 `config.yml` 的四项：

1. `mail.address`：完整的 163 邮箱地址。
2. `mail.authorization_code`：163 网页版开启 IMAP/SMTP 后生成的客户端授权码。
3. `deepseek.api_key`：DeepSeek 官方 API Key。
4. `control.allowed_senders`：允许制定任务的发件邮箱列表，例如 `["owner@example.com"]`。

`control.token` 已在安装时随机生成；不需要替换。不要把实际配置提交到 Git、打包或发送到聊天。服务每轮重读配置，等待状态下保存后约 2 秒内开始尝试连接。正常处理时配置变更在当前操作结束后生效。

默认 IMAP 为 `imap.163.com:993`，SMTP 为 `smtp.163.com:465`，均使用 TLS 证书验证；登录后在服务端支持时发送 IMAP ID。官方模型接口为 `https://api.deepseek.com/chat/completions`，使用 JSON 输出；模型名可在配置中调整。

## 启动、停止和状态

当前电脑已经安装。迁移到新 Windows 电脑时，先安装 Python 3.11+，然后在解压目录执行 `powershell -ExecutionPolicy Bypass -File .\install.ps1`。它安装依赖、生成带随机控制口令的 `config.yml` 并启动等待进程，已有配置会保留。

安装好的 Windows 版本在当前目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
powershell -ExecutionPolicy Bypass -File .\status.ps1
powershell -ExecutionPolicy Bypass -File .\stop.ps1
```

这是当前用户下隐藏运行的后台 Python 进程，无需管理员权限。不是 Windows 系统服务；注销、重启或系统终止进程后需要再次运行 `start.ps1`。重复启动由文件锁阻止。停止会等待当前网络操作结束，随后退出。

## 查看邮件工作台

`start.ps1` 会同时启动收信 Agent 和本地前端。浏览器打开 **http://127.0.0.1:8787**，可搜索发件人、正文或摘要，按类型和文件夹筛选，查看邮件正文、AI 摘要、任务匹配、表格提取内容，也可查看自动化任务和发件记录。开启原件保留时可下载附件及原始 EML。页面每 15 秒刷新本地数据；实际收信频率由配置中的 `poll_seconds` 决定。

前端是独立的只读进程，仅监听本机 127.0.0.1，不读取 `config.yml`，邮件 HTML 和外链不执行。`stop.ps1` 只停止收信 Agent，面板仍可浏览已有归档。单独管理面板：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-dashboard.ps1
powershell -ExecutionPolicy Bypass -File .\stop-dashboard.ps1
```

8787 被占用时，可使用 `start-dashboard.ps1 -Port 8788`。不要将这个本地面板通过代理公开到互联网。

需要重装依赖时：

```powershell
python -m venv .venv
$AgentPython = if (Test-Path .\.venv\Scripts\python.exe) { '.\.venv\Scripts\python.exe' } else { '.\.venv\bin\python.exe' }
& $AgentPython -m pip install -r requirements.lock.txt
```

手工检查连接（先停止后台服务，不会发测试邮件）：

```powershell
& $AgentPython agent.py check --ai
& $AgentPython -m unittest discover -s tests -v
```

## 自然语言控制与邮件问答

当前本机使用`control.mode: natural_language`。`config.yml`配置白名单、允许动作及采购开关；正文不填写YAML或口令，不要求固定主题前缀。控制邮箱直接发送：

```text
请按附件请购单发起询价，收到供应商报价后自动入库。
```

附上原14列格式请购单，状态编号填10。查询或问答无需附件，例如“查一下请购单20261030AL的报价信息”“哪些供应商还没报价”“最小起订量是什么意思”。数据库查询使用受限的参数化接口，模型不执行SQL或任意代码；回信只发送给白名单中的原控制者。自然语言模式首次启用时间以前的控制邮件只归档，不补执行。

主题也参与自然语言解析，新邮件可以只写主题、正文留空。主题和正文合并理解，正文中明确的限制或更正优先；空白的Re/回复/转发邮件不重复执行原标题里的动作。此行为适用于查询、问答、请购及数据更新。

配置中`control.actions: [inquire, query, answer, update_data]`启用请购询价、业务查询、模型问答及已有业务记录更新。`control.update_tables`限定可修改表，`control.update_max_rows`限定单封行数；例如控制者可写“请按附件按供应商编号更新邮箱”，程序实际校验写库并回信变更结果，不再以问答代替执行。事务回滚、更新前后审计和邮件去重覆盖整个修改过程。

`procurement.enabled`控制采购流程，`procurement.max_sends_per_cycle`控制每轮询价封数。当前未启用通用任务增删改动作；需要时可将对应动作加入actions，沿用原任务校验。供应商来信不是控制者，仍按询价号和完整登记邮箱匹配报价。

## 旧版结构化任务协议（兼容模式）

以下内容仅适用于`control.mode: structured`；当前自然语言模式不需要这些YAML命令。

从白名单邮箱给配置的 163 邮箱发信，主题以 `[AGENT]` 开头，正文用**纯文本**。需要白名单和口令同时通过；检查通过前不会将控制内容发给 AI。请勿把口令放在主题中。

自然语言示例，记得把示例口令和收件人换成实际值：

```yaml
token: "复制 config.yml 中 control.token 的完整值"
request: |
  创建任务 invoice_alert。
  当来信涉及发票，并要求我补充材料时，
  发邮件到 target@example.com，说明原始发件人、需要补充的材料和截止日期。
```

`request` 会经 DeepSeek 转为结构化任务，再由本地程序校验。一次控制邮件只执行一个动作；成功或格式错误会向已通过双重验证的控制者发回执。未通过验证不会回信。

也可以直接使用明确的 YAML 命令，无需 AI 解释控制意图：

```yaml
token: "复制本机 control.token"
action: create
task:
  id: invoice_alert
  name: 发票材料提醒
  condition: 来信涉及发票且要求补充材料
  recipients:
    - target@example.com
  instruction: 用中文说明原始发件人、所需材料和明确给出的截止日期；没有提供的信息写待确认
```

查看任务：

```yaml
token: "复制本机 control.token"
action: list
```

修改任务，可只写需要变更的字段：

```yaml
token: "复制本机 control.token"
action: update
task_id: invoice_alert
task:
  condition: 来信涉及发票且需要我确认或补充材料
  recipients: ["another@example.com"]
```

暂停、恢复和删除分别使用 `action: pause`、`action: resume`、`action: delete`，并提供 `task_id`。删除保留审计记录，创建新任务要用新 id。所有白名单成员都是任务管理员；移出白名单后，该成员拥有的任务暂停触发和发送，可由仍在白名单中的管理员 update/resume 接管。

普通任务类型是“收到来信 → AI 根据正文及表格判断条件 → 发送提醒”，不支持定时任务、执行代码或联网检索。另提供下方本地授权的一次性回信测试。

## 读取 CSV 与 Excel 附件

CSV 使用 Python 标准库；XLSX 使用 openpyxl 只读模式；XLS 使用 xlrd。全部在内存中读取，不依赖本机路径或 Excel 程序。CSV 支持 UTF-8（含 BOM）、GB18030、Big5、UTF-16/32 编码，以及逗号、分号、制表符、竖线分隔、引号内换行和前导零。无法确定的编码或格式会标记错误，不用替代字符冒充完整内容。

`parsed.attachments[].extraction` 保存状态、工作表、逐行字符串数组和提取范围。DeepSeek 能读取这些单元格来摘要和判断任务；工作台邮件详情的附件区域可展开查看原文。单元格里的指令及邮箱属于数据，不会创建任务或改变收件人。XLSX 公式保存为公式原文；XLS 读取已保存值，不执行宏或公式，不提取图片、图表或 OCR。

每附件上限 5 MiB；每封邮件合计最多 5001 行（含表头及空行）、每行 100 列、20 个工作表、120000 字符（含结构预算）。超过范围标记 `truncated` 并说明只提取了部分内容；损坏、加密或不支持的文件标记 `error`。XLSX 解压总量上限 50 MiB。需要处理更大的业务表格时，应改为分批提取与分析。

当前本机配置已设为 `storage.keep_originals: false`：新邮件不保存原始 MIME 或附件 base64，不另存 CSV 文件，仅保存邮件文本、元数据、提取内容及 AI 结果。历史归档不删除。其他非表格附件只保留元数据。没有原件的附件不提供下载；要保留原件，可改为 `true` 后重启服务。配置缺省时沿用旧版的保留行为，示例配置明确设为 `false`。

## 一次性收信回执测试

先停止收信服务，再在项目目录启用，随后重新启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\stop.ps1
$AgentPython = if (Test-Path .\.venv\Scripts\python.exe) { '.\.venv\Scripts\python.exe' } else { '.\.venv\bin\python.exe' }
& $AgentPython agent.py reply-once
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

此任务仅对启用后服务器收到的第一封普通来信生效，向唯一有效的原始 `From` 地址回复正文 **收到**，不使用 `Reply-To` 或 AI 决定收件人。历史邮件、控制邮件、自发邮件、自动回执、退信、邮件列表和常见 no-reply 地址不触发。多封同时同步时优先选择服务器收信时间最早的一封。同一条任务只会选中一封邮件，发送后失效；SMTP 结果不确定时不自动重发，也不改选下一封。

CSV 提取回信模式使用 `agent.py reply-once --mode csv`：只选择启用后第一封带 CSV 附件的普通来信，向原发件人回复 **DeepSeek 摘要 + 解析器直接提取的 CSV 原文**。其他普通来信不会消耗测试。先归档、预留唯一来信，AI 分析成功后才入发件队列；AI 不可用时保留同一封并稍后重试，不改选下一封。格式错误或超出限制会在回信中说明。CSV 内所有邮箱仍然只是数据。已有测试尚未完成时先取消，再启用新模式。

在工作台“自动化任务”中查看等待/完成状态，在“发件记录”中查看实际发件结果。此功能不依赖 AI 摘要成功。预览模式下禁止启用实际回信；普通 AI 任务保持自己的执行规则。

取消尚未发送的测试（同样先停止收信服务，执行后重新启动）：

```powershell
& $AgentPython agent.py reply-once --cancel
```

取消无法撤回已发送或结果不确定的邮件。此测试通过本机命令授权，不允许普通来信内容自行启用。

## 同步与归档规则

- 首次按 UID 顺序同步全部历史邮件，已读、未读均包含；之后按文件夹和 UIDVALIDITY 增量同步。使用只读文件夹及 BODY.PEEK，不改变已读状态。
- `folders: ["*"]` 同步服务器通过 IMAP 暴露的所有可选文件夹。已发送和草稿也可能归档，但不触发任务。服务器未暴露的来信、已从服务器彻底删除的邮件无法追溯。
- 解析后的正文、邮件头、附件元数据及表格提取内容写入单封 JSON；是否保留原始 MIME/附件由 `storage.keep_originals` 决定。附件不落地执行，HTML 不加载外链；PDF/OCR 尚不支持。
- 归档 JSON 写盘后才推进游标；AI 不可用仍继续归档，分析稍后重试。邮件按原始字节 SHA256 跨文件夹去重，来源目录分别记录；不同字节的副本视为不同邮件。
- 历史普通来信也会逐步生成摘要，因此首次全量分析会使用 API 额度；`max_ai_per_cycle` 控制每轮分析数量，最新来信和控制邮件优先。持续高负载时旧邮件摘要会延后。
- 第一次有效配置成功加载是服务激活时间。早于该时间的控制邮件只归档；任务只匹配创建或修改/恢复生效后的来信，依据服务器 INTERNALDATE，不依据容易伪造的 Date 头。默认不回放历史发信。
- 任务实际收件人来自已授权的任务配置，AI 不能通过返回额外收件人改变路由。自身发送的邮件带实例标识，不再次触发任务。控制口令会从自然语言请求中移除，邮件授权码和 API Key 不进入模型提示。
- SMTP 发送结果不明确时置为 `uncertain`，避免自动重发造成重复；服务日志不记录正文和密钥。完整归档包含控制邮件中的口令，应限制本机数据访问并妥善备份。

## 本地文件

```text
data/
  service-status.json       # 进程、心跳、同步/分析/发件状态，无密钥
  service.log               # 轮转日志
  dashboard-status.json     # 本机前端进程状态
  accounts/<账号哈希>/
    state.json              # 游标、待处理队列、首次激活时间
    tasks.json              # 任务、版本、控制操作幂等记录
    reply-once.json         # 本地一次性回信任务及选中的来信
    mail/<内容哈希>.json     # 单封邮件文本、表格提取、分析和处理状态
    outbox/<任务哈希>.json   # 待发/已发/预览/不确定记录
```

更换邮箱自动使用独立目录，原账号数据保留。JSON 便于检查和迁移；大邮箱会占用较多磁盘和文件，原始 MIME 与附件同时保留会增加存储大小。

## 后续部署 Linux 云主机

核心 Python 服务跨平台。上传源码包并安装 Python 3.11+，在服务目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python setup_config.py
# 填写 config.yml 后启动；正式常驻可由 systemd/Supervisor 托管这条命令
.venv/bin/python agent.py run
```

迁移时只运行一个收信实例；如要延续现有游标、任务和发送状态，先停止旧实例，再复制配置和 `data/`。使用持久化磁盘保存 JSON 即可，不需要存 CSV 本体。云上查看面板可单独运行 `.venv/bin/python dashboard.py --port 8787`，通过 SSH 本地转发访问。后台进程的工作目录设为本项目，凭据配置和 `data/` 仅供服务账户访问。

`outbox.status`：`pending` 等待发送，`sending` 正在发送，`sent` SMTP 已接受，`sent_partial` 部分地址被接受，`preview` 预览，`uncertain` 结果不确定，`blocked` 任务权限/版本已变更。SMTP 接受不保证最终投递，仍应关注退信。预览不会因切换正式模式而自动补发。

核实不确定邮件后可手动处理（先停止服务）：

```powershell
& $AgentPython agent.py resolve <64位发件记录id> --as sent
# 仅在已确认未发送、确实要重试时：
& $AgentPython agent.py resolve <64位发件记录id> --as retry
# 放弃此封：
& $AgentPython agent.py resolve <64位发件记录id> --as cancelled
```

代码不使用任何已连接的聊天邮箱账号，所有收发只通过你本机配置的 163 邮箱完成。

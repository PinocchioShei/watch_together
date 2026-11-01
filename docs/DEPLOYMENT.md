# 部署指南

本文对应当前 `app:app` 实现。默认工作路径示例为 `D:\project\watch_together_local`，命令使用 Windows PowerShell。应用没有前端编译步骤，也不需要独立数据库服务。

## 1. 环境和首次安装

| 项目 | 要求 |
| --- | --- |
| Python | 本机验证为 64 位 CPython 3.14.3；其他版本需要自行完成本文验收 |
| Python 依赖 | 根目录 requirements.txt 固定核心依赖版本，传递依赖由 pip 解析；并非全部依赖的锁文件 |
| 外部工具 | PATH 中的 ffmpeg、ffprobe，且支持 libx264、AAC 编码 |
| 磁盘 | 项目目录可写；导入期间要同时容纳上传暂存、播放资源和封面 |
| 前端 | 支持原生 audio/video、ES modules、WebSocket 的现代浏览器 |

```powershell
$projectDir = "D:\project\watch_together_local"
$pythonExe = "C:\Python314\python.exe"
Set-Location -LiteralPath $projectDir

& $pythonExe --version
& $pythonExe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
ffmpeg -version
ffprobe -version

New-Item -ItemType Directory -Force -Path media, log/debug | Out-Null
```

直接调用虚拟环境解释器即可，不需要执行 Activate.ps1。`media` 在应用创建时被 StaticFiles 检查，`log/debug` 在模块导入时用于创建日志文件，两个目录必须提前存在。随后启动会自动初始化数据库、媒体子目录和作品元数据。

新服务器应复制代码、static 和 resource，重新创建虚拟环境。迁移已有站点还需要恢复数据库和完整 media 目录，见备份章节。

## 2. 环境变量与管理员账号

在启动服务的同一个 PowerShell 会话中设置变量。当前配置文件不会自动加载 `.env`。

```powershell
$env:WATCH_ADMIN_USER = "admin"
$adminPassword = Read-Host "设置首次管理员密码" -AsSecureString
$env:WATCH_ADMIN_PASSWORD = [System.Net.NetworkCredential]::new("", $adminPassword).Password
$env:WATCH_SYNC_DEBUG = "0"
```

| 变量 | 默认值 | 实际行为 |
| --- | --- | --- |
| WATCH_ADMIN_USER | admin | 数据库没有 admin_account 记录时初始化账号 |
| WATCH_ADMIN_PASSWORD | admin123 | 首次管理员登录时创建记录，已有记录时不覆盖密码 |
| WATCH_SYNC_DEBUG | 1 | 控制同步事件日志；0、false、off、no 关闭事件写入 |

已有管理员账号应通过管理页修改账号/密码，修改环境变量不会重置已有账号。管理员会话默认 12 小时且存在进程内，重启后需要重新登录。普通用户会话保存在 SQLite，默认 14 天，新登录覆盖同账号旧会话。

`WATCH_SYNC_DEBUG=0` 不免除日志目录的初始化要求。后台脚本启动的子进程继承当前环境；计划任务从自身运行环境继承，不继承你之前在某个终端临时设置的 `$env:`。

## 3. 启动方式和端口

### 推荐：单端口、单进程

```powershell
Set-Location -LiteralPath $projectDir
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8090 --workers 1
```

- 用户页：<http://127.0.0.1:8090/>
- 管理页：<http://127.0.0.1:8090/admin>
- 接口文档：<http://127.0.0.1:8090/docs>

Ctrl+C 停止前台服务。房间连接、在线成员和管理员 token 是进程内状态，正式运行保持一个 worker。开发调试可用 `--reload` 替换 `--workers 1`，重新加载会断开连接和清空进程内管理员会话。

### Windows 后台启动

完成安装和目录初始化后，可用现有脚本只启动 8090：

```powershell
& .\tools\start_watch_together_server.ps1 `
    -ProjectDir $projectDir `
    -PythonExe (Join-Path $projectDir ".venv\Scripts\python.exe") `
    -Ports @(8090) `
    -BindHost "127.0.0.1"
```

脚本隐藏运行，日志写入 `log/debug/server_8090.log` 和 `server_8090_err.log`，并尝试停止目标端口上匹配的旧 Uvicorn Python 进程。重复运行会重启目标服务；脚本返回后应检查日志和 HTTP 响应，确认应用已启动。

### 现有双端口部署

不传参数运行脚本时，项目和解释器路径默认位于 `D:\project\watch_together_local`，端口为 **8090、8091**，监听地址为 IPv6 `::`。

```powershell
& .\tools\start_watch_together_server.ps1
```

同机可访问 <http://[::1]:8090/> 和 <http://[::1]:8091/admin>。IPv4 回环是否同时可用取决于系统双栈行为，IPv4 部署可显式选择 `-BindHost "0.0.0.0"`。

两端口均提供所有页面和 API，没有按端口分离权限。两个进程共享 SQLite 与磁盘，不共享 WebSocket hub、在线状态、管理员 token；跨端口使用同一房间无法保证实时广播。8091 管理端对房间/用户的修改虽然落库，给 8090 房间的即时通知也不共享。兼容旧部署时应让所有一起看的用户统一使用 8090。

## 4. 局域网、隧道和反向代理

局域网 IPv4 访问时，将监听地址改为 `0.0.0.0`，浏览器使用服务器实际局域网 IP，例如 `http://192.168.1.10:8090/`。防火墙按需要允许对应 TCP 端口；0.0.0.0 是监听地址，不能作为浏览器访问地址。

HTTP 隧道可继续使用绑定 127.0.0.1 的服务。单独安装 cloudflared 后，临时隧道示例为：

```powershell
cloudflared tunnel --url http://127.0.0.1:8090
```

分享隧道返回的 HTTPS 地址。Python 依赖不会安装 cloudflared。用户页、管理页、API、WebSocket 和媒体使用同一来源路径；HTTPS 页面自动使用 WSS。

反向代理需要转发 HTTP、WebSocket Upgrade 和媒体 Range 请求。上传包括文件传输与 ffmpeg 处理；浏览器在文件发送阶段保留 15 分钟超时，发送完成后等待服务端处理，不再使用该时限。两端通过已认证的 `/api/import/jobs`、`/api/admin/import/jobs` 创建/轮询真实处理进度（详见合集接口文档）。这仍是一个持续等待上传响应的请求，进度轮询不能绕过代理/隧道的请求大小、空闲超时和总时长限制。业务接口的单文件上限是 1.5 GiB，不代表隧道可以接收同样大的请求；批量追加还会累计请求体积。

当前 `/media` 静态文件不要求登录，挂载范围包含 tmp 和作品元数据；对外共享时应按这个现有行为配置访问范围。管理页面和全部管理 API 也由同一个应用提供，管理操作依赖管理员认证。

## 5. 现有自启动和 DDNS 脚本

以下是可选 Windows 工具，本地运行无需安装计划任务；注册任务需要具备对应系统权限。

| 脚本 | 当前用途与前提 |
| --- | --- |
| start_watch_together_server.ps1 | 后台启动，支持 ProjectDir、PythonExe、Ports、BindHost |
| setup_autostart_tasks.ps1 | 注册登录启动的 WatchTogether-Server-Startup，以及每 2 分钟的 WatchTogether-DDNS-IPv6 |
| ddns_cf_ipv6.ps1 | 读取 Cloudflare 配置，将公网 IPv6 更新到 AAAA 记录 |
| ddns_cf_runner.ps1 | 周期调用 DDNS 脚本的常驻循环 |
| setup_ddns_guard_task.ps1 | 注册每 5 分钟的网络/DDNS 守护任务，依赖 run_hidden.vbs；使用前核对网络脚本是否适合当前机器 |

自启动注册脚本虽然接收 ProjectDir，但注册后的子脚本调用没有完整传递自定义路径、端口和监听参数。移动目录或采用单端口方案时，需先调整实际启动脚本默认值及任务动作；仅传 `setup_autostart_tasks.ps1 -ProjectDir ...` 不会迁移所有路径。

需要 IPv6 DDNS 时，从示例生成私有配置，填写 api_token、zone_id、record_name：

```powershell
Copy-Item .\tools\ddns_cf_config.example.json .\tools\ddns_cf_config.json
notepad .\tools\ddns_cf_config.json
```

把示例域名替换为自己的域名，API token 需要能维护对应区域的 DNS 记录。本机配置已被 Git 忽略。DDNS 脚本支持 ConfigPath，但后台循环/计划任务默认仍使用脚本中的固定路径，迁移时一起核对。DDNS 只更新域名记录，不建立隧道，也不自动解决端口、防火墙或公网 IPv6 可达性。

## 6. 部署验收

```powershell
.\.venv\Scripts\python.exe -m pip check
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8090/).StatusCode
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8090/admin).StatusCode
Get-Content .\log\debug\server_8090_err.log -Tail 30
```

两次 HTTP 检查应返回 200；最后一行仅适用于后台脚本启动的服务。自定义端口/监听地址时同步调整检查 URL。

浏览器完整验收：

1. 管理员能登录，已有站点的原账号仍有效。
2. 注册/登录普通用户，创建并加入房间；房间 WebSocket 握手为 101。
3. 上传小音视频，确认作品目录、封面、媒体卡片生成且可播放。
4. 两个不同用户通过同一地址进入同一房间，确认播放/暂停、拖动、选集和合集模式同步；切换作品后自动选择音视频播放器。
5. 拖动时检查媒体请求的 206/Content-Range，并确认代理/隧道下也能加载。

用户使用 `/api/import`（兼容 `/api/upload-video`），管理员使用 `/api/admin/import`，字段和行为一致。两端都支持新建/追加视频或音频合集，单批最多 30 文件；同一 work 不允许混合视频文件与音频文件。命名和接口详见 [媒体合集与接口](COLLECTIONS.md)。上传字节进度来自实际传输，服务器处理时间显示是估算，没有后台进度查询 API。

## 7. 数据、升级和备份恢复

```text
watch_together.db                  用户、会话、房间、状态、媒体索引
media/work/<分类>/<作品键>/<原文件名>.mp4 视频作品/合集；原文件名主体不变
media/work/<分类>/<作品键>/<原文件名>.m4a 音频作品/合集；与视频文件互斥
media/work/<分类>/<作品键>/cover.jpg       封面
media/work/<分类>/<作品键>/.meta.json      分类、标题、媒体种类、原始文件名等元数据
media/tmp/                         上传暂存
media/default_cover.jpg            默认封面
resource/                          页面背景图
```

数据库的 import_jobs 表保存上传处理进度，跨进程共享；仅任务所属用户/管理员会话可查询，不在公开媒体目录存放进度或认证信息。创建新任务时清理 24 小时无更新的记录。升级自动建表，无需手工迁移媒体。

users.last_room_joined_at 保存最近一次成功创建/进入任意房间的 UTC 时间，管理员成员接口返回 lastRoomJoinedAt，界面按浏览器本地时间显示。登录、房间状态轮询、WebSocket 心跳/断线重连不刷新该值；刷新网页成功恢复入房会更新。时间独立于房间成员关系，离房、删房不会清除。首次升级从现存 room_members.joined_at 中取每人的最新记录；已删除房间的历史无法还原，没有可确认时间时显示 No record。该列会自动迁移，不需手工执行 SQL。

`media/video`、`media/audio` 是旧布局，启动会迁移文件到 work、更新索引并补元数据。启动会写数据；更新代码或恢复旧备份后，首次启动前需要备份。

升级前停止全部使用同一数据库/媒体目录的服务进程（包括遗留 8091），备份后更新代码、安装 requirements，再以单进程启动并验收。前端修改还需要更新页面引用中的版本参数，并核对相对 import 子模块的缓存。

历史视频的独立音频副本和通用文件名可以用离线工具统一整理，具体预览、备份、迁移与恢复见 [历史媒体整理](LEGACY_MEDIA_MIGRATION.md)。该操作不会在启动时自动执行。

备份应把数据库、完整 media（含隐藏的 .meta.json）、自定义 resource 和私有部署配置作为同一批数据保存。建议全部停服后复制；若仍有 WAL 辅助文件，不能在未确认 checkpoint 的情况下只取主 db、丢弃 WAL。在线备份应使用 SQLite backup 接口或一致性快照。

恢复时先停服，恢复同一批次数据库和媒体，在目标机器重建虚拟环境并安装依赖，再创建日志目录、设置环境变量和启动。不要混用旧库 WAL 与另一备份的 db，不要在服务运行中手工删除 WAL/SHM。管理员内存会话失效，普通用户会话来自恢复的数据库。

## 8. 故障排查

| 现象 | 检查位置 |
| --- | --- |
| 首次启动提示 media 不存在或 sync.log 打不开 | 启动前创建 media、log/debug，检查写权限 |
| WebSocket 无法建立/缺少实现 | 安装 requirements 中的 websockets；核对代理 Upgrade、同源地址和 token |
| ffmpeg/ffprobe not found | 在实际服务/任务账号的 PATH 中验证；安装后可能需重开终端/服务获取新 PATH |
| 网页正常但多人不同步 | 统一端口和单 worker，查看 log/debug/sync.log |
| 环境变量改密码无效 | 已存在 admin_account；用原凭据登录后在管理页面修改 |
| 导入处理中或超时 | 查看错误日志、ffmpeg 进程、磁盘和代理限制；浏览器超时不代表服务端已取消 |
| 导入失败后 tmp 有文件 | 先确认没有正在导入的任务，再评估清理；当前没有统一后台清理机制 |
| 媒体改名/删除后房间无法播放 | 改名或改分类会更新持久化房间 URL；已打开的页面可刷新媒体库，删除后需要另选有效作品 |
| SQLite database is locked | 检查多进程、并发导入和长事务；busy_timeout 为 20 秒 |
| 缺背景图 | 核对 resource/day.jpg、night.jpg；resource 只在应用创建时存在才挂载 |

同步日志默认开启且没有轮转上限，可用 WATCH_SYNC_DEBUG 关闭事件写入。维护启动流程或部署拓扑时，请同步更新 README 和本文。

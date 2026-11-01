# Watch Together

基于 FastAPI、SQLite 和原生 JavaScript 的一起看/一起听网站，包含用户页面与管理页面。

- 用户注册登录、带密码的房间、房间聊天。
- 选中作品自动使用视频或音频播放器；WebSocket 同步播放/暂停、拖动、选集和合集连播模式，成员主动操作可以接管控制。
- 上传视频或音频，通过 ffmpeg 归一化格式、生成封面；新作品只存视频文件或音频文件，不额外提取视频音轨。
- 用户与管理员共用上传能力：批量创建/追加视频或音频合集，单次最多 30 文件，禁止混存。管理员还可维护用户、房间和媒体。
- 媒体保存在本地 `media/work/<作品键>/`，SQLite 保存账号、房间、播放状态和媒体索引。

## Windows 快速启动

本机验证环境为 **64 位 CPython 3.14.3**。先安装包含 `libx264`、AAC 编码器的 ffmpeg，并把 `ffmpeg`、`ffprobe` 加入 PATH。无需 Node.js 或前端构建。

在 PowerShell 中执行；按实际安装位置修改前两行。已有部署可以跳过虚拟环境创建，更新前先按部署指南备份数据。

```powershell
$projectDir = "D:\project\watch_together_local"
$pythonExe = "C:\Python314\python.exe"
Set-Location -LiteralPath $projectDir

& $pythonExe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
ffmpeg -version
ffprobe -version

# 必须在导入 app 之前创建；媒体子目录和数据库由启动过程初始化。
New-Item -ItemType Directory -Force -Path media, log/debug | Out-Null

# 仅在数据库尚无管理员记录时用于初始化；已有账号请在管理页面修改。
$env:WATCH_ADMIN_USER = "admin"
$adminPassword = Read-Host "设置首次管理员密码" -AsSecureString
$env:WATCH_ADMIN_PASSWORD = [System.Net.NetworkCredential]::new("", $adminPassword).Password

.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8090 --workers 1
```

访问用户页 <http://127.0.0.1:8090/>，管理页 <http://127.0.0.1:8090/admin>。前台运行时使用 Ctrl+C 停止。开发时可用 `--reload` 替换 `--workers 1`。

推荐所有用户和管理员使用同一个端口、同一个服务进程。历史启动脚本默认同时启动 8090、8091，但两者运行相同的应用；WebSocket 房间连接和管理员会话不跨进程共享。8091 的管理页面名称不构成接口权限隔离。

## 部署与维护

合集命名和上传字段见 [媒体合集与接口](docs/COLLECTIONS.md)，回归验证见 [测试说明](tests/README.md)。

完整操作见 [部署指南](docs/DEPLOYMENT.md)：包含后台启动、历史双端口、自启动脚本、局域网/隧道访问、环境变量、验收、备份恢复与故障排查。

| 路径 | 用途 |
| --- | --- |
| `app.py`、`wt_server/` | 应用入口、路由、认证、SQLite、媒体导入和连接管理 |
| `static/` | 用户和管理页面，浏览器直接加载 ES modules |
| `resource/` | 日夜主题背景图 |
| `watch_together.db` | SQLite 数据库，运行时可能有 `-wal` / `-shm` 辅助文件 |
| `media/work/<作品键>/` | 媒体文件、`cover.jpg` 和 `.meta.json` |
| `media/tmp/` | 上传暂存文件 |
| `media/video/`、`media/audio/` | 历史布局，启动时迁移到作品目录 |
| `log/debug/` | 服务及同步日志 |
| `requirements.txt` | 已知运行环境的核心 Python 依赖版本 |

媒体通过 HTTP 文件/Range 请求加载，WebSocket 负责同步播放状态。`/api/media` 要求登录，`/media` 静态文件 URL 当前可直接访问；对外部署时应按这个行为配置访问范围。

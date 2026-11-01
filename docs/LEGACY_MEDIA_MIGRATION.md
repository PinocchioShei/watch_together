# 历史媒体整理

`tools/migrate_legacy_media.py` 是离线维护工具，默认只生成计划，不在服务启动时自动执行。

## 规则

- 用 ffprobe 检查真实媒体流。
- 仅自动处理旧上传器的固定混存结构：一份 `video.mp4` 和一份 `audio.m4a`，视频具有内嵌音轨，两者音轨时长、采样率和声道数相符。独立音频会保留在完整备份中，从公开作品目录移出。其他混存形态或不匹配的音轨直接报错，不能靠文件扩展名认定可删除。
- 对单文件作品，若有原始文件名元数据就使用该名称作为 work 名；没有原名且媒体仍叫 `video.mp4` / `audio.m4a` 等通用名称时，用现有 work 名加媒体扩展名作为迁移后的名称。
- 已有音频合集保留 work 名和所有曲目文件名。不能从历史通用名恢复的曲目仍保留当前名，不伪造丢失的原始上传名称。
- 补齐 mediaKind、originalNames、trackDurations 和迁移来源信息；保留分类和其他扩展元数据。originalNames 对旧资源可表示迁移后的回退名称，不代表找回了上传源文件名。
- 同步数据库标题、默认媒体 URL、封面 URL、大小和时长；保留可匹配作品的数据库 ID，补齐文件存在但未入库的作品，清理文件已不存在的失效媒体索引。同步改写已持久化的房间媒体 URL，旧提取音轨引用指向保留的视频。
- 重名、未知混存、链接或嵌套目录等情况在写入前拒绝；不覆盖、不自动加后缀、不重新转码。

## 执行

在项目根目录执行只读预览，可保持服务运行：

```powershell
.\.venv\Scripts\python.exe tools/migrate_legacy_media.py --report migration-plan.json
```

审阅计划后，停止 **全部** 使用同一数据库和媒体目录的服务。默认工具会检查本机 IPv4/IPv6 的 8090、8091；自定义端口或远端进程也必须自行停掉。暂存上传尚未完成时不要执行。

```powershell
.\.venv\Scripts\python.exe tools/migrate_legacy_media.py --apply `
  --backup-dir 'D:\project\watch_together_local\backups\legacy-media-YYYYMMDD-HHMMSS'
```

备份目录必须是新目录，且位于公开的 `media` 之外。工具完整复制 media（含暂存和元数据），用 SQLite backup 接口备份数据库，逐文件核对 SHA-256 后才开始修改。迁移后保留媒体再次与备份核对 SHA-256，音视频内容不变。备份是独立副本，不使用硬链接。

备份包含 `media/`、`watch_together.db`、`manifest.json`、`plan.json`，成功后还会生成 `result.json`。备份和真实媒体均不提交 Git，也不经 `/media` 暴露。此工具不提供旧 URL 的 HTTP 重定向；浏览器刷新媒体库，原先复制的直链需替换成新 URL。

正常执行失败会回滚数据库事务、文件重命名、元数据和移出的音轨。断电或进程被强制终止时，依靠完整备份恢复：先停止全部服务，将当前 media 另行移出项目目录，恢复备份的 media；再通过 SQLite backup 接口从备份恢复数据库。数据库与媒体必须来自同一次备份，不把另一批 WAL/SHM 文件混入恢复。

完成后重启服务，核对媒体列表数量、作品类型、上传追加目标、封面和媒体 HTTP Range；维护流程仍需更新前端版本、中英文日志并创建 Git commit。

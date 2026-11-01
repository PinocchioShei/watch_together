# 历史媒体整理

`tools/migrate_legacy_media.py` 是离线维护工具，默认只生成计划，不在服务启动时自动执行。

## 按分类整理目录

当前新上传使用 `media/work/<分类>/<work>/`，分类为 movie、RJ、ASMR、music、shot。历史平铺 work 可使用下面的专用工具；作品名、媒体名和元数据扩展字段均保留，分类优先沿用已有目录/元数据，其次数据库分类。work 名仍全站唯一。

```powershell
# 只读检查现有作品的分类和目标路径
.\.venv\Scripts\python.exe tools/migrate_media_categories.py

# 停止全部服务后执行；完整备份路径必须是新目录且位于公开 media 外
.\.venv\Scripts\python.exe tools/migrate_media_categories.py --apply `
  --backup-dir 'D:\project\watch_together_local\backups\category-migration-YYYYMMDD-HHMMSS'
```

分类迁移完整备份 media 和 SQLite，校验 SHA-256 后移动目录并更新媒体索引、封面和房间保存的每一项 URL。文件不重新转码；普通失败回滚全部已移动目录及数据库事务。重复预览已迁移目录的 movedWorks 为 0。非法分类、重名、目标冲突、链接及无法直接嵌套的历史同名目录会停止迁移，保留源数据。恢复遵循下文同一批次数据库/媒体备份的要求。

服务兼容旧平铺路径和分类变化前的路径，返回 307 到当前资源，Range 可继续使用。管理员改分类也会移动目录并保持原始名称元数据。目录和索引的跨进程修改与导入使用同一把文件锁；不同端口仍不共享 WebSocket 广播。

下面的旧音轨/命名整理工具同时兼容平铺和分类目录，不会把分类目录当成作品。

## 规则

- 用 ffprobe 检查真实媒体流。
- 仅自动处理旧上传器的固定混存结构：一份 `video.mp4` 和一份 `audio.m4a`，视频具有内嵌音轨，两者音轨时长、采样率和声道数相符。独立音频会保留在完整备份中，从公开作品目录移出。其他混存形态或不匹配的音轨直接报错，不能靠文件扩展名认定可删除。
- work 名称不含末尾的媒体扩展名；已有带扩展名的目录和索引会同步改名，资源文件保留扩展名。单文件作品优先取原始文件名去掉媒体扩展名作为 work 名；没有原名且媒体仍叫 `video.mp4` / `audio.m4a` 等通用名称时，使用现有 work 标题，资源文件用标题加媒体扩展名命名。
- 已有音频合集保留标题（仅去掉末尾媒体扩展名）和所有曲目文件名。不能从历史通用名恢复的曲目仍保留当前名，不伪造丢失的原始上传名称。
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

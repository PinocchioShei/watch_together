# 媒体作品、合集与上传接口

## 作品和命名

- 单文件上传创建一个 work，work 名称等于上传文件名（含原扩展名），例如 `片段 #1.mp4`。
- 多文件上传创建一个合集，必须在创建时填写 `work_name`。同一批选择的文件归入该 work。
- 媒体保留原文件名主体。浏览器兼容转码输出为 MP4 或 AAC/M4A 时，仅调整扩展名；界面通过原始名称元数据显示完整上传文件名。
- 同名 work、同一 work 内同名输出文件直接返回冲突，不覆盖，也不自动追加 `_2` 等后缀。两个不同格式的输入若转码后同名，也会冲突。
- 视频文件可以有内嵌音轨。禁止混存指不能在同一 work 放视频文件和独立音频文件；新上传的视频不会再生成独立音频副本。
- 旧版本的视频与提取音轨保持原状，仍可播放视频，但混存旧 work 不作为追加目标，直接请求追加返回 409。

示例（磁盘与请求 URL 中的扩展名取决于转码输出）：

```text
media/work/
  片段 #1.mp4/                    单文件 work 名称等于上传文件名
    片段 #1.mp4
    cover.jpg
    .meta.json
  第一季/                         用户填写的合集名称
    第1集.mp4
    第2集.mp4
    cover.jpg
    .meta.json
  音乐集/
    曲目1.m4a                     上传曲目1.flac，界面仍显示原始名称
    曲目2.m4a
    cover.jpg
    .meta.json
```

目前合集按文件名自然排序，例如第2集在第10集之前；不按上传顺序排序。文件名需要满足本机文件系统要求，不能包含路径分隔符、保留设备名或其他非法字符。URL 由服务端做百分号编码，请直接使用返回的 URL，避免 `#`、中文等被浏览器误解。

## 相同能力的两个上传入口

| 身份 | 上传 | 追加目标 |
| --- | --- | --- |
| 用户 | `POST /api/import`，兼容旧路径 `/api/upload-video` | `GET /api/media/append-targets` |
| 管理员 | `POST /api/admin/import` | `GET /api/admin/media/append-targets` |

两端使用相同 multipart 字段和后端处理函数，仅认证不同：用户 session 与管理员 token 各自验证。用户端和管理端共用前端上传表单逻辑。媒体库当前是全站共享，追加操作没有引入新的作品归属权限模型。

| multipart 字段 | 内容 |
| --- | --- |
| `file` | 必填，可重复，1–30 个文件；每个文件最多 1.5 GiB |
| `import_mode` | `create`（默认）或 `append`；兼容 `append_audio`、`append_video`，兼容模式会额外限制相应类型 |
| `media_type` | create 时为 movie、RJ、ASMR、music、shot 之一，默认 movie；append 沿用目标作品分类 |
| `work_name` | create 多文件时必填；单文件按上传文件名命名，不使用此字段 |
| `target_work_key` | append 时必填，取追加目标接口返回的 workKey |
| `cover` | create 可选；append 不接受封面 |

示例使用 curl.exe；TOKEN、文件路径和目标名称需替换为实际值：

```powershell
# 用户创建视频合集；file 字段重复提交。
curl.exe http://127.0.0.1:8090/api/import `
  -H "Authorization: Bearer TOKEN" `
  -F "file=@D:/videos/Episode 1.mp4" `
  -F "file=@D:/videos/Episode 2.mp4" `
  -F "import_mode=create" -F "work_name=Season 1" -F "media_type=movie"

# 管理员向已有视频合集追加；同样的字段也可用于用户入口。
curl.exe http://127.0.0.1:8090/api/admin/import `
  -H "Authorization: Bearer ADMIN_TOKEN" `
  -F "file=@D:/videos/Episode 3.mp4" `
  -F "import_mode=append" -F "target_work_key=Season 1"
```

类型由 ffprobe 读取实际媒体流决定。整批文件先检测，混合批次或与目标类型不一致的请求拒绝发布。转码先在暂存目录完成，随后提交文件、元数据和数据库索引；正常请求失败时回滚本批新增内容。导入通过文件锁协调同一磁盘目录上的服务进程，重名并发追加不会覆盖已有文件。进程崩溃等异常仍需要按部署文档检查暂存文件。

成功响应含 `ok`、`workKey`、`mediaKind`、`videoUrl`、`audioUrl`、`videoTracks`、`audioTracks`、`imported`、`items` 和 `appendMode`。本次全部成功后才返回 `ok=true`；批量处理不再采用旧版的部分成功语义。主要失败状态：400 无效媒体、401 未认证、404 追加目标不存在、409 混存或名称冲突、413 单文件过大、422 字段/数量/名称不合法。

## 媒体库和同步

`GET /api/media` 和管理员媒体列表都返回每个 work 的：

- `workKey`、`mediaKind`（video / audio / 历史 mixed）。
- `videoTracks` 或 `audioTracks`：按自然顺序排列的可播放 URL。videoUrl/audioUrl 为默认资源，保留旧客户端兼容。
- `originalNames`：URL 到原始完整文件名的映射，用于显示转码前的名称。

播放页没有手动 Video Mode / Audio Mode。点击作品自动选用对应播放器，共用选集/曲目下拉框、上一项/下一项及播完停止（stop）、自动下一项（next）、重复当前项（repeat）。next 在合集最后一项结束后停止，不跳到另一个 work。

房间仍使用 `/ws/rooms/{room_id}`，单一控制者负责自然播完后的停播/切集，其他客户端跟随。手动选集、播放/暂停、拖动、改变合集模式可以接管控制。sync/state 载荷包含媒体 URL、时间、播放状态、trackIndex、albumMode、controllerUserId；服务端根据 URL 推导媒体种类和集数，不信任客户端随意填写的模式/索引。

`playMode` 保留为内部传输字段，供客户端自动选用播放器。`albumMode` 同时适用于视频和音频；历史 JSON 名称 audioAlbumMode 和 SQLite 列 audio_album_mode 暂保留兼容，不需要手工迁移数据库。刷新或重新连接时从持久状态恢复当前项和合集模式。

部署仍应遵循单服务进程原则：导入文件锁解决了同目录的写入冲突，不会让不同进程共享房间 WebSocket 广播。

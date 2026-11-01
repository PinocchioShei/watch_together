# 合集回归验证

所有检查在临时站点副本中生成独立数据库和媒体，不连接部署中的 8090/8091。

安装 requirements.txt 并确保 ffmpeg、ffprobe 在 PATH 后运行后端集成检查：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_collections.py -v
```

覆盖用户/管理员相同上传能力、批量创建/追加、文件命名和重名拒绝、原始名称显示元数据、禁止混存、旧混存作品兼容、失败清理、重启后的准确名称和跨进程并发追加，以及视频选集/模式/控制权的 WebSocket 同步与恢复。

可选的双用户浏览器检查：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Windows 检测到标准安装位置的 Edge 时优先使用 Edge，否则使用 Playwright Chromium；未安装浏览器依赖时跳过浏览器检查。浏览器检查实际操作两个上传表单，并验证视频上一集/下一集、选集、自动连播、重复、停止、暂停时另一成员接管，以及切换到音频作品后的自动播放器选择和连播。

`test_legacy_migration.py` 在独立临时目录验证历史整理：音轨归档和媒体字节不变、原名与合集保留、名称冲突、未知混存拒绝、数据库失败后文件及元数据回滚。不会修改实际站点。

`test_category_migration.py` 验证分类目录迁移、已有分类保留、索引 ID/房间 URL 更新、重复执行、离线和在线移动失败回滚及旧整理工具兼容。接口检查还覆盖改分类后的旧 URL 跳转、选集、追加、重命名、删除、路径越界拒绝，以及与分类同名的作品。

长中文作品名及文件名编码后可超过 500 字符，回归覆盖完整分类 URL 的视频/音频同步、第二集索引、REST 状态与重连恢复；浏览器用长中文合集名及 `#`、`%` 验证两端上传与双用户播放，防止路径被截断。

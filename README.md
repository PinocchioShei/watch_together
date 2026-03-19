# Watch Together (Local + Tunnel Ready)

A lightweight watch-together demo:

- multi-user register/login
- room creation and join
- real-time sync of play/pause/seek via WebSocket
- room chat
- upload local video and auto transcode to browser-playable MP4 (H.264/AAC)
- single-session login per account (new login invalidates old session)
- playback control lock in room (only controller can push sync)
- room owner can delete room
- SQLite persistence

## 1) Run locally

```bash
cd D:\project\watch_together_local
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

Open browser:

- http://127.0.0.1:8080

## 2) Let friends access

Use a tunnel on local 8080, for example (Cloudflare/ngrok). Then share the tunnel URL.

## Notes

- For best sync, use direct video files (`.mp4/.webm/.ogg`) accessible to all users.
- Requires `ffmpeg` and `ffprobe` installed in PATH for auto-transcode.
- This is a practical MVP and can be extended to HLS/DASH, JWT, Redis pubsub, etc.

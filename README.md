# Watch Together (Local + Tunnel Ready)

A lightweight watch-together demo:

- multi-user register/login
- room creation and join
- real-time sync of play/pause/seek + playback mode (video/audio) via WebSocket
- room chat
- media library playback (`media/video`, `media/audio`)
- upload local video and auto transcode/import to library (video + extracted audio)
- admin dashboard for user/media CRUD and import management
- single-session login per account (new login invalidates old session)
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

Admin dashboard (new port):

- http://127.0.0.1:8091/admin

Default admin credentials (change via env for production):

- username: `admin`
- password: `admin123`

## 2) Let friends access

Use a tunnel on local 8080, for example (Cloudflare/ngrok). Then share the tunnel URL.

## Notes

- Imported media is stored in `media/video` and `media/audio`.
- Requires `ffmpeg` and `ffprobe` installed in PATH for auto-transcode.
- This is a practical MVP and can be extended to HLS/DASH, JWT, Redis pubsub, etc.

# Local RTSP video simulator

This standalone Docker Compose project loops local video files and publishes
each one as an RTSP camera stream. MediaMTX serves the streams, while FFmpeg
decodes and re-encodes each clip at the configured frame rate and bitrate.

This simulates RTSP input for development and testing; it does not simulate
camera-specific metadata, variable network conditions, or camera hardware.

## Run locally

1. Install Docker Desktop with Docker Compose.
2. Copy `.env.example` to `.env` if you want to change the host RTSP port.
3. Put video files in `videos/`.
4. Edit `streams.json` using `streams.example.json` as a reference.
5. Start the services from this directory:

   ```powershell
   docker compose up --build -d
   ```

6. Check the logs:

   ```powershell
   docker compose logs -f
   ```

7. Open a stream in VLC or enter it as a camera source in the app:

   ```text
   rtsp://127.0.0.1:8554/front-door
   ```

   Replace `8554` with `RTSP_PORT` and `front-door` with a configured stream
   name. The app can connect to this address when it runs directly on the same
   host. If the app itself runs in Docker, configure Docker networking so it
   can reach the MediaMTX service; `127.0.0.1` inside the app container refers
   to that container, not the host.

## Configure streams

`streams.json` accepts one or more stream definitions:

```json
{
  "streams": [
    {
      "name": "front-door",
      "file": "front-door.mp4",
      "fps": 15,
      "bitrate": "1200k"
    }
  ]
}
```

- `name` becomes the RTSP path, such as `/front-door`.
- `file` is relative to `videos/`; paths outside that directory are rejected.
- `fps` is an output frame rate from 1 to 60.
- `bitrate` is an FFmpeg bitrate such as `1200k` or `2M`.
- Clips loop continuously. Audio is omitted from the simulated stream.

After changing `streams.json`, restart the publisher:

```powershell
docker compose restart publisher
```

The RTSP server is published only on the local machine by default. Do not
expose it publicly without adding authentication and appropriate network
controls. The container layout is suitable for local development; deploying
this separately on Railway requires attaching persistent storage for the
video files and configuring Railway networking for the RTSP port.

# Local RTSP video simulator

This standalone Docker Compose project loops local video files and publishes
each one as an RTSP camera stream. MediaMTX serves the streams, while FFmpeg
decodes and re-encodes each clip at the configured frame rate and bitrate.

This simulates RTSP input for development and testing; it does not simulate
camera-specific metadata, variable network conditions, or camera hardware.

## Run locally

1. Install Docker Desktop with Docker Compose.
2. Copy `.env.example` to `.env` if you want to change the host RTSP port.
3. Start the services from this directory:

   ```powershell
   docker compose up --build -d
   ```

4. Open the control panel at [http://127.0.0.1:8765](http://127.0.0.1:8765).
   Upload clips and add or edit streams there. The default upload limit is 2 GB.
5. Check the logs:

   ```powershell
   docker compose logs -f
   ```

6. Open a stream in VLC or enter it as a camera source in the app:

   ```text
   rtsp://127.0.0.1:8554/front-door
   ```

   Replace `8554` with `RTSP_PORT` and `front-door` with a configured stream
   name. The app can connect to this address when it runs directly on the same
   host. If the app itself runs in Docker, configure Docker networking so it
   can reach the MediaMTX service; `127.0.0.1` inside the app container refers
   to that container, not the host.

## Manage videos and streams

The control panel at `http://127.0.0.1:8765` lets you upload and delete video
files, then add, edit, or remove simulated streams. Stream changes are
validated and take effect automatically; only streams whose settings changed
are restarted, not the containers or other streams. A clip cannot be deleted
while a configured stream is using it.

Video files are stored in `videos/` and the stream list is persisted in
`streams.json`. Both are mounted read/write into the publisher container.
Configuration edits are detected automatically, so neither the GUI nor the
publisher container needs a restart.

## Deploying on Railway

Attach a persistent volume to the publisher service. The app uses Railway's
`RAILWAY_VOLUME_MOUNT_PATH` automatically, saving `streams.json` at the volume
root and video files in its `videos/` subdirectory. On a first deploy to an
empty volume it creates an empty stream configuration. The control panel uses
Railway's `PORT` environment variable unless `CONTROL_PANEL_PORT` is set.
Expose that port using a Railway public domain to open the panel. Configure the RTSP service's networking separately if a client outside
Railway needs to connect to the RTSP endpoint. Create a Railway TCP Proxy
targeting the RTSP server's internal port. Set `RTSP_PRIVATE_HOST` to the
RTSP server service's private Railway hostname, such as
`mediamtx.railway.internal`, and set `RTSP_PRIVATE_PORT` to its internal
port (normally `8554`). Set `RTSP_EXTERNAL_HOST` and `RTSP_EXTERNAL_PORT`
to the hostname and generated port from the TCP Proxy. The control panel
then shows a private URL for other Railway services and an external URL for
VLC on your computer.

The GUI is unauthenticated and designed for local development or a protected
private environment. Do not expose it publicly without placing authentication
and network restrictions in front of it.

You can still edit `streams.json` manually using this format:

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

The control panel and RTSP port are bound to the local machine by default in
Docker Compose. The container layout is suitable for local development;
Railway deployments require persistent storage for the video files and
separate networking configuration for the RTSP port.

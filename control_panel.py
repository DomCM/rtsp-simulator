import json
import logging
import os
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from publisher import SUPPORTED_VIDEO_EXTENSIONS, load_streams


LOGGER = logging.getLogger("rtsp-simulator.control-panel")
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
SAFE_FILENAME_PATTERN = re.compile(r"^[^/\\\x00-\x1f]+$")


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RTSP Simulator</title>
  <style>
    :root { color-scheme: dark; font: 15px/1.45 system-ui, sans-serif; background: #101827; color: #e7edf6; }
    * { box-sizing: border-box; }
    body { max-width: 980px; margin: 0 auto; padding: 32px 18px 60px; }
    h1 { margin: 0; font-size: 28px; } h2 { margin: 0 0 14px; font-size: 19px; }
    p { color: #9aabc1; } .sub { margin: 5px 0 24px; }
    section { background: #182437; border: 1px solid #2b3b52; border-radius: 12px; padding: 20px; margin: 18px 0; }
    .upload, .stream-form, .stream-row { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
    .stream-row { align-items: center; border-top: 1px solid #2b3b52; padding: 14px 0; }
    .stream-row:first-child { border-top: 0; } .field { display: flex; flex: 1 1 150px; flex-direction: column; gap: 5px; }
    label { color: #9aabc1; font-size: 12px; } input, select { width: 100%; padding: 10px; color: #e7edf6; background: #101827; border: 1px solid #3a4b63; border-radius: 7px; }
    button { padding: 10px 14px; color: white; border: 0; border-radius: 7px; background: #2563eb; font-weight: 650; cursor: pointer; }
    button.secondary { background: #34445c; } button.danger { background: #9f323b; } button:disabled { opacity: .5; cursor: wait; }
    .video-list { display: grid; gap: 8px; } .video-item { display: flex; justify-content: space-between; align-items: center; gap: 12px; border-top: 1px solid #2b3b52; padding: 10px 0; overflow-wrap: anywhere; }
    .video-item:first-child { border-top: 0; } .muted { color: #8192a9; font-size: 13px; }
    .url { flex: 1 1 100%; color: #87b4ff; font: 13px ui-monospace, monospace; overflow-wrap: anywhere; }
    #notice { display: none; position: sticky; top: 8px; padding: 11px 14px; margin: 0 0 14px; border-radius: 8px; background: #244b39; z-index: 2; }
    #notice.error { background: #6c2d35; } .empty { padding: 14px 0; color: #8192a9; }
    @media(max-width: 560px) { body { padding: 20px 12px; } section { padding: 15px; } .stream-row > * { flex-basis: 100%; } }
  </style>
</head>
<body>
  <div id="notice" role="status"></div>
  <h1>RTSP video simulator</h1>
  <p class="sub">Upload clips and configure looping camera streams. Changes take effect automatically.</p>
  <section>
    <h2>Video files</h2>
    <form id="upload-form" class="upload">
      <div class="field"><label for="video-file">Upload a clip (MP4, AVI, MKV, MOV, or WebM; up to 2 GB)</label><input id="video-file" type="file" accept=".mp4,.avi,.mkv,.mov,.webm" required></div>
      <button id="upload-button" type="submit">Upload video</button>
    </form>
    <div id="videos" class="video-list" style="margin-top:14px"></div>
  </section>
  <section>
    <h2>Streams</h2>
    <div id="streams"></div>
  </section>
  <section>
    <h2>Add a stream</h2>
    <form id="add-form" class="stream-form">
      <div class="field"><label for="new-name">Stream name / RTSP path</label><input id="new-name" name="name" pattern="[A-Za-z0-9_-]+" placeholder="front-door" required></div>
      <div class="field"><label for="new-file">Video file</label><select id="new-file" name="file" required></select></div>
      <div class="field"><label for="new-fps">FPS (1-60)</label><input id="new-fps" name="fps" type="number" min="1" max="60" value="15" required></div>
      <div class="field"><label for="new-bitrate">Bitrate (e.g. 1200k)</label><input id="new-bitrate" name="bitrate" value="1200k" pattern="[1-9][0-9]*[kKmM]?" required></div>
      <button type="submit">Add stream</button>
    </form>
  </section>
  <script>
    const notice = document.getElementById('notice');
    let state = { streams: [], videos: [] };
    function showNotice(message, error = false) {
      notice.textContent = message;
      notice.classList.toggle('error', error);
      notice.style.display = 'block';
      window.clearTimeout(showNotice.timer);
      showNotice.timer = window.setTimeout(() => notice.style.display = 'none', 5000);
    }
    async function api(path, options = {}) {
      const response = await fetch(path, options);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
      return result;
    }
    function field(labelText, control) {
      const wrapper = document.createElement('label');
      wrapper.className = 'field';
      const caption = document.createElement('span');
      caption.textContent = labelText;
      wrapper.append(caption, control);
      return wrapper;
    }
    function input(value, attributes = {}) {
      const element = document.createElement('input');
      element.value = value;
      for (const [key, item] of Object.entries(attributes)) element.setAttribute(key, item);
      return element;
    }
    function optionList(select, selected) {
      select.replaceChildren();
      for (const video of state.videos) {
        const option = document.createElement('option');
        option.value = video.name;
        option.textContent = `${video.name} (${video.size})`;
        select.append(option);
      }
      if (selected) select.value = selected;
    }
    function render() {
      const videos = document.getElementById('videos');
      videos.replaceChildren();
      if (!state.videos.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'No video files uploaded yet.';
        videos.append(empty);
      }
      for (const video of state.videos) {
        const row = document.createElement('div');
        row.className = 'video-item';
        const details = document.createElement('span');
        details.textContent = `${video.name} · ${video.size}`;
        const remove = document.createElement('button');
        remove.className = 'danger';
        remove.textContent = 'Delete';
        remove.addEventListener('click', async () => {
          if (!window.confirm(`Delete ${video.name}?`)) return;
          try {
            await api(`/api/videos?name=${encodeURIComponent(video.name)}`, { method: 'DELETE' });
            await refresh();
            showNotice('Video deleted.');
          } catch (error) { showNotice(error.message, true); }
        });
        row.append(details, remove);
        videos.append(row);
      }

      const streams = document.getElementById('streams');
      streams.replaceChildren();
      if (!state.streams.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'No streams configured yet.';
        streams.append(empty);
      }
      for (const stream of state.streams) {
        const row = document.createElement('div');
        row.className = 'stream-row';
        const name = input(stream.name, { required: '', pattern: '[A-Za-z0-9_-]+' });
        const file = document.createElement('select');
        optionList(file, stream.file);
        const fps = input(stream.fps, { required: '', type: 'number', min: '1', max: '60' });
        const bitrate = input(stream.bitrate, { required: '', pattern: '[1-9][0-9]*[kKmM]?' });
        const save = document.createElement('button');
        save.textContent = 'Save';
        save.addEventListener('click', async () => {
          if (![name, file, fps, bitrate].every(control => control.reportValidity())) return;
          const updated = state.streams.map(item => item.name === stream.name
            ? { name: name.value.trim(), file: file.value, fps: Number(fps.value), bitrate: bitrate.value.trim() }
            : item);
          try {
            await saveStreams(updated);
            showNotice('Stream saved; it will reconnect with the new settings.');
          } catch (error) { showNotice(error.message, true); }
        });
        const remove = document.createElement('button');
        remove.className = 'danger';
        remove.textContent = 'Remove';
        remove.addEventListener('click', async () => {
          try {
            await saveStreams(state.streams.filter(item => item.name !== stream.name));
            showNotice('Stream removed.');
          } catch (error) { showNotice(error.message, true); }
        });
        const url = document.createElement('div');
        url.className = 'url';
        url.textContent = stream.url;
        row.append(
          field('Name / RTSP path', name),
          field('Video file', file),
          field('FPS', fps),
          field('Bitrate', bitrate),
          save,
          remove,
          url,
        );
        streams.append(row);
      }

      const newFile = document.getElementById('new-file');
      optionList(newFile);
      newFile.required = state.videos.length > 0;
      document.querySelector('#add-form button[type="submit"]').disabled = !state.videos.length;
    }
    async function refresh() {
      state = await api('/api/state', { cache: 'no-store' });
      render();
    }
    async function saveStreams(streams) {
      await api('/api/streams', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ streams }),
      });
      await refresh();
    }
    document.getElementById('upload-form').addEventListener('submit', async event => {
      event.preventDefault();
      const file = document.getElementById('video-file').files[0];
      if (!file) return;
      const button = document.getElementById('upload-button');
      button.disabled = true;
      try {
        await api(`/api/videos?name=${encodeURIComponent(file.name)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/octet-stream' },
          body: file,
        });
        event.target.reset();
        await refresh();
        showNotice('Video uploaded.');
      } catch (error) { showNotice(error.message, true); }
      finally { button.disabled = false; }
    });
    document.getElementById('add-form').addEventListener('submit', async event => {
      event.preventDefault();
      const data = new FormData(event.target);
      const stream = {
        name: data.get('name').trim(),
        file: data.get('file'),
        fps: Number(data.get('fps')),
        bitrate: data.get('bitrate').trim(),
      };
      try {
        await saveStreams([...state.streams, stream]);
        event.target.reset();
        document.getElementById('new-fps').value = 15;
        document.getElementById('new-bitrate').value = '1200k';
        showNotice('Stream added and starting.');
      } catch (error) { showNotice(error.message, true); }
    });
    refresh().catch(error => showNotice(error.message, true));
    window.setInterval(() => refresh().catch(error => showNotice(error.message, true)), 5000);
  </script>
</body>
</html>
"""


def video_directory():
    return Path(os.environ.get("VIDEO_DIR", "/videos")).resolve()


def config_file():
    return Path(os.environ.get("STREAMS_CONFIG", "/config/streams.json"))


def read_config():
    with config_file().open(encoding="utf-8") as stream_file:
        config = json.load(stream_file)
    if not isinstance(config, dict) or not isinstance(config.get("streams"), list):
        raise ValueError("Configuration must contain a 'streams' array")
    return config


def safe_video_name(name):
    return (
        isinstance(name, str)
        and len(name) <= 255
        and SAFE_FILENAME_PATTERN.fullmatch(name) is not None
        and name not in {".", ".."}
        and Path(name).suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
    )


def list_videos():
    root = video_directory()
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for entry in root.iterdir():
        if (
            entry.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
            and entry.is_file()
            and not entry.is_symlink()
        ):
            files.append({
                "name": entry.name,
                "size": entry.stat().st_size,
            })
    return sorted(files, key=lambda item: item["name"].casefold())


def human_size(size):
    if size < 1024:
        return f"{size} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"


class ControlPanelHandler(BaseHTTPRequestHandler):
    server_version = "RTSPSimulatorControl/1.0"

    def log_message(self, format_string, *args):
        LOGGER.info("%s - %s", self.address_string(), format_string % args)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid Content-Length") from error
        if not 0 < content_length <= MAX_CONFIG_BYTES:
            raise ValueError("Configuration request must be between 1 byte and 1 MB")
        try:
            return json.loads(self.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Request body must be valid JSON") from error

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            try:
                config = read_config()
                public_port = int(os.environ.get("RTSP_PUBLIC_PORT", "8554"))
                public_host = os.environ.get("RTSP_PUBLIC_HOST", "127.0.0.1")
                stream_items = []
                for raw in config["streams"]:
                    stream_items.append({
                        "name": raw["name"],
                        "file": raw["file"],
                        "fps": raw.get("fps", 15),
                        "bitrate": raw.get("bitrate", "1200k"),
                        "url": f"rtsp://{public_host}:{public_port}/{raw['name']}",
                    })
                self.send_json(200, {
                    "streams": stream_items,
                    "videos": [
                        {**video, "size": human_size(video["size"])}
                        for video in list_videos()
                    ],
                })
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self.send_json(500, {"error": str(error)})
            return
        self.send_json(404, {"error": "Not found"})

    def do_PUT(self):
        if urlsplit(self.path).path != "/api/streams":
            self.send_json(404, {"error": "Not found"})
            return
        temporary_path = None
        try:
            payload = self.read_json()
            if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
                raise ValueError("Request must contain a 'streams' array")
            candidate = {"streams": payload["streams"]}
            path = config_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".streams-",
                suffix=".json",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(candidate, temporary_file)
            load_streams(temporary_path)
            os.replace(temporary_path, path)
            temporary_path = None
            self.send_json(200, {"ok": True})
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def do_POST(self):
        parsed_path = urlsplit(self.path)
        if parsed_path.path != "/api/videos":
            self.send_json(404, {"error": "Not found"})
            return

        query = parse_qs(parsed_path.query)
        names = query.get("name", [])
        name = names[0] if len(names) == 1 else None
        if not safe_video_name(name):
            self.send_json(400, {"error": "Choose a supported video filename"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if not 0 < content_length <= MAX_UPLOAD_BYTES:
            self.close_connection = True
            self.send_json(413, {"error": "Video must be between 1 byte and 2 GB"})
            return

        root = video_directory()
        root.mkdir(parents=True, exist_ok=True)
        destination = root / name
        if destination.exists():
            self.send_json(409, {"error": "A video with that filename already exists"})
            return

        temporary_path = root / f".upload-{uuid4().hex}.tmp"
        try:
            remaining = content_length
            with temporary_path.open("xb") as uploaded_file:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Upload ended before all bytes were received")
                    uploaded_file.write(chunk)
                    remaining -= len(chunk)
            if destination.exists():
                self.send_json(409, {"error": "A video with that filename already exists"})
                return
            os.link(temporary_path, destination)
            temporary_path.unlink()
            self.send_json(201, {"ok": True, "name": name})
        except FileExistsError:
            self.send_json(409, {"error": "A video with that filename already exists"})
        except (OSError, ValueError) as error:
            LOGGER.exception("Unable to save uploaded video %s", name)
            self.send_json(500, {"error": str(error)})
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def do_DELETE(self):
        parsed_path = urlsplit(self.path)
        if parsed_path.path != "/api/videos":
            self.send_json(404, {"error": "Not found"})
            return
        query = parse_qs(parsed_path.query)
        names = query.get("name", [])
        name = names[0] if len(names) == 1 else None
        if not safe_video_name(name):
            self.send_json(400, {"error": "Invalid video filename"})
            return
        try:
            config = read_config()
            if any(stream.get("file") == name for stream in config["streams"]):
                self.send_json(409, {"error": "Remove or change streams using this video first"})
                return
            path = video_directory() / name
            if path.is_symlink() or not path.is_file():
                self.send_json(404, {"error": "Video not found"})
                return
            path.unlink()
            self.send_json(200, {"ok": True})
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self.send_json(500, {"error": str(error)})

    def do_OPTIONS(self):
        self.send_json(405, {"error": "Method not allowed"})


def start_server():
    host = os.environ.get("CONTROL_PANEL_HOST", "0.0.0.0")
    port = int(os.environ.get("CONTROL_PANEL_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), ControlPanelHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="control-panel", daemon=True)
    thread.start()
    return server, thread

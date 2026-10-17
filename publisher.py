import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("rtsp-simulator")
STOP = threading.Event()
STREAM_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
BITRATE_PATTERN = re.compile(r"^[1-9][0-9]*[kKmM]?$")
SUPPORTED_VIDEO_EXTENSIONS = {".avi", ".mkv", ".mov", ".mp4", ".webm"}


def stop_requested(_signal_number, _frame):
    STOP.set()


def stream_config_path():
    configured_path = os.environ.get("STREAMS_CONFIG")
    if configured_path:
        return Path(configured_path)
    railway_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if railway_volume:
        return Path(railway_volume) / "streams.json"
    return Path("/config/streams.json")


def stream_video_directory():
    configured_path = os.environ.get("VIDEO_DIR")
    if configured_path:
        return Path(configured_path).resolve()
    railway_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if railway_volume:
        return (Path(railway_volume) / "videos").resolve()
    return Path("/videos").resolve()


def ensure_stream_config(path=None):
    path = Path(path or stream_config_path())
    if path.exists():
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    template_path = Path(__file__).with_name("streams.default.json")
    template = template_path.read_bytes() if template_path.is_file() else b'{"streams": []}\n'
    temporary_path = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with temporary_path.open("xb") as config_file:
            config_file.write(template)
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            pass
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return path


def load_streams(config_path=None):
    config_path = ensure_stream_config(config_path)
    video_root = stream_video_directory()
    rtsp_host = os.environ.get("RTSP_HOST", "mediamtx")
    rtsp_port = int(os.environ.get("RTSP_PORT", "8554"))
    if not 1 <= rtsp_port <= 65535:
        raise ValueError("RTSP_PORT must be between 1 and 65535")

    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict) or not isinstance(config.get("streams"), list):
        raise ValueError("Configuration must contain a 'streams' array")

    streams = []
    names = set()
    for index, item in enumerate(config["streams"]):
        if not isinstance(item, dict):
            raise ValueError(f"streams[{index}] must be an object")

        name = item.get("name")
        if not isinstance(name, str) or not STREAM_NAME_PATTERN.fullmatch(name):
            raise ValueError(
                f"streams[{index}].name must contain only letters, numbers, "
                "underscores, or hyphens"
            )
        if name in names:
            raise ValueError(f"Duplicate RTSP stream name: {name}")
        names.add(name)

        relative_file = item.get("file")
        if not isinstance(relative_file, str) or not relative_file:
            raise ValueError(f"streams[{index}].file must be a relative file path")
        video_path = (video_root / relative_file).resolve()
        if not video_path.is_relative_to(video_root):
            raise ValueError(f"streams[{index}].file must stay inside VIDEO_DIR")
        if video_path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            raise ValueError(
                f"streams[{index}].file must use one of: "
                f"{', '.join(sorted(SUPPORTED_VIDEO_EXTENSIONS))}"
            )
        if not video_path.is_file():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        fps = item.get("fps", 15)
        if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 60:
            raise ValueError(f"streams[{index}].fps must be an integer from 1 to 60")

        bitrate = item.get("bitrate", "1200k")
        if not isinstance(bitrate, str) or not BITRATE_PATTERN.fullmatch(bitrate):
            raise ValueError(
                f"streams[{index}].bitrate must look like '1200k' or '2M'"
            )

        streams.append({
            "name": name,
            "file": video_path,
            "fps": fps,
            "bitrate": bitrate,
            "url": f"rtsp://{rtsp_host}:{rtsp_port}/{name}",
        })

    return streams


def ffmpeg_command(stream):
    bitrate_unit = stream["bitrate"][-1] if stream["bitrate"][-1].isalpha() else ""
    bitrate_value = stream["bitrate"][:-1] if bitrate_unit else stream["bitrate"]
    buffer_size = f"{int(bitrate_value) * 2}{bitrate_unit}"

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-stream_loop", "-1",
        "-re",
        "-i", str(stream["file"]),
        "-an",
        "-vf", f"fps={stream['fps']}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-b:v", stream["bitrate"],
        "-maxrate", stream["bitrate"],
        "-bufsize", buffer_size,
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        stream["url"],
    ]


def stop_process(process, stream_name):
    if process.poll() is not None:
        return
    LOGGER.info("Stopping publisher for %s", stream_name)
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        LOGGER.warning("Force stopping publisher for %s", stream_name)
        process.kill()
        process.wait()


def stream_signature(stream):
    return stream["file"], stream["fps"], stream["bitrate"], stream["url"]


def start_publisher(stream, process_record):
    try:
        LOGGER.info("Publishing %s from %s", stream["url"], stream["file"].name)
        process_record["process"] = subprocess.Popen(ffmpeg_command(stream))
    except OSError:
        LOGGER.exception("Unable to start publisher for %s", stream["name"])
        process_record["next_start"] = time.monotonic() + process_record["restart_delay"]
        process_record["restart_delay"] = min(process_record["restart_delay"] * 2, 30)


def run():
    from control_panel import start_server

    control_server, control_thread = start_server()
    LOGGER.info(
        "Control panel listening on http://127.0.0.1:%s",
        control_server.server_address[1],
    )

    processes = {}
    last_config_error = None
    try:
        while not STOP.is_set():
            try:
                streams = load_streams()
                last_config_error = None
            except (OSError, ValueError, json.JSONDecodeError) as error:
                error_message = str(error)
                if error_message != last_config_error:
                    LOGGER.error("Invalid stream configuration: %s", error_message)
                    last_config_error = error_message
                STOP.wait(0.5)
                continue

            desired_streams = {stream["name"]: stream for stream in streams}

            for name, record in list(processes.items()):
                desired = desired_streams.get(name)
                if desired is None or stream_signature(desired) != record["signature"]:
                    if record["process"] is not None:
                        stop_process(record["process"], name)
                    del processes[name]

            for stream in streams:
                name = stream["name"]
                signature = stream_signature(stream)
                record = processes.get(name)
                if record is None:
                    record = {
                        "signature": signature,
                        "process": None,
                        "restart_delay": 1,
                        "next_start": 0,
                    }
                    processes[name] = record

                process = record["process"]
                if process is None:
                    if time.monotonic() >= record["next_start"]:
                        start_publisher(stream, record)
                    continue

                exit_code = process.poll()
                if exit_code is not None:
                    delay = record["restart_delay"]
                    LOGGER.warning("Publisher %s exited with status %s", name, exit_code)
                    record["process"] = None
                    record["next_start"] = time.monotonic() + delay
                    record["restart_delay"] = min(delay * 2, 30)
                else:
                    record["restart_delay"] = 1

            STOP.wait(0.5)
    finally:
        STOP.set()
        control_server.shutdown()
        control_server.server_close()
        control_thread.join(timeout=5)
        for name, record in processes.items():
            if record["process"] is not None:
                stop_process(record["process"], name)


def main():
    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)
    try:
        run()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        LOGGER.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

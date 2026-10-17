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


def load_streams():
    config_path = Path(os.environ.get("STREAMS_CONFIG", "/config/streams.json"))
    video_root = Path(os.environ.get("VIDEO_DIR", "/videos")).resolve()
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


def run():
    streams = load_streams()
    if not streams:
        LOGGER.info("No streams configured; add entries to streams.json and restart.")
    else:
        LOGGER.info("Starting %d simulated RTSP stream(s)", len(streams))

    processes = {}
    restart_delays = {stream["name"]: 1 for stream in streams}
    try:
        while not STOP.is_set():
            for stream in streams:
                name = stream["name"]
                process = processes.get(name)
                if process is None:
                    command = ffmpeg_command(stream)
                    LOGGER.info("Publishing %s from %s", stream["url"], stream["file"].name)
                    processes[name] = subprocess.Popen(command)
                    continue

                exit_code = process.poll()
                if exit_code is not None:
                    if STOP.is_set():
                        break
                    delay = restart_delays[name]
                    LOGGER.warning(
                        "Publisher %s exited with status %s; restarting in %s second(s)",
                        name,
                        exit_code,
                        delay,
                    )
                    if STOP.wait(delay):
                        break
                    restart_delays[name] = min(delay * 2, 30)
                    processes[name] = None
                else:
                    restart_delays[name] = 1

            STOP.wait(0.5)
    finally:
        for name, process in processes.items():
            if process is not None:
                stop_process(process, name)


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

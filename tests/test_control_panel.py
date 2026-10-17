import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import publisher
from control_panel import start_server


class ControlPanelTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.video_directory = self.root / "videos"
        self.video_directory.mkdir()
        self.config_path = self.root / "streams.json"
        self.config_path.write_text('{"streams": []}', encoding="utf-8")
        self.environment = patch.dict(os.environ, {
            "VIDEO_DIR": str(self.video_directory),
            "STREAMS_CONFIG": str(self.config_path),
            "CONTROL_PANEL_HOST": "127.0.0.1",
            "CONTROL_PANEL_PORT": "0",
            "RTSP_PRIVATE_HOST": "127.0.0.1",
            "RTSP_PRIVATE_PORT": "8554",
            "RTSP_HOST": "mediamtx",
            "RTSP_PORT": "8554",
        })
        self.environment.start()
        self.server, self.thread = start_server()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.environment.stop()
        self.temporary_directory.cleanup()

    def request_json(self, path, method="GET", body=None, content_type=None):
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_upload_configure_and_delete_video(self):
        status, payload = self.request_json(
            "/api/videos?name=sample.mp4",
            method="POST",
            body=b"sample clip",
            content_type="application/octet-stream",
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["name"], "sample.mp4")

        status, payload = self.request_json("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["videos"][0]["name"], "sample.mp4")

        config = {
            "streams": [{
                "name": "test-camera",
                "file": "sample.mp4",
                "fps": 12,
                "bitrate": "900k",
            }]
        }
        status, _ = self.request_json(
            "/api/streams",
            method="PUT",
            body=json.dumps(config).encode(),
            content_type="application/json",
        )
        self.assertEqual(status, 200)

        status, payload = self.request_json("/api/state")
        self.assertEqual(
            payload["streams"][0]["private_url"],
            "rtsp://127.0.0.1:8554/test-camera",
        )
        self.assertIsNone(payload["streams"][0]["external_url"])
        self.assertEqual(payload["streams"][0]["fps"], 12)

        status, payload = self.request_json(
            "/api/videos?name=sample.mp4",
            method="DELETE",
        )
        self.assertEqual(status, 409)
        self.assertIn("using this video", payload["error"])

        self.request_json(
            "/api/streams",
            method="PUT",
            body=b'{"streams":[]}',
            content_type="application/json",
        )
        status, _ = self.request_json(
            "/api/videos?name=" + quote("sample.mp4"),
            method="DELETE",
        )
        self.assertEqual(status, 200)
        self.assertFalse((self.video_directory / "sample.mp4").exists())

    def test_rejects_invalid_stream_and_path_traversal(self):
        status, payload = self.request_json(
            "/api/streams",
            method="PUT",
            body=json.dumps({
                "streams": [{
                    "name": "bad",
                    "file": "../outside.mp4",
                    "fps": 15,
                    "bitrate": "1200k",
                }]
            }).encode(),
            content_type="application/json",
        )
        self.assertEqual(status, 400)
        self.assertIn("inside VIDEO_DIR", payload["error"])
        self.assertEqual(json.loads(self.config_path.read_text())["streams"], [])

        status, payload = self.request_json(
            "/api/videos?name=..%2Foutside.mp4",
            method="POST",
            body=b"data",
            content_type="application/octet-stream",
        )
        self.assertEqual(status, 400)
        self.assertIn("filename", payload["error"])

    def test_serves_control_panel(self):
        with urlopen(self.base_url + "/") as response:
            page = response.read().decode()
        self.assertIn("RTSP video simulator", page)
        self.assertIn("Upload a clip", page)

    def test_state_remains_available_when_a_configured_video_is_missing(self):
        self.config_path.write_text(
            json.dumps({
                "streams": [{
                    "name": "missing-camera",
                    "file": "missing.mp4",
                    "fps": 15,
                    "bitrate": "1200k",
                }]
            }),
            encoding="utf-8",
        )
        status, payload = self.request_json("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["streams"][0]["name"], "missing-camera")

    def test_creates_config_and_video_directory_in_railway_volume(self):
        mount_path = self.root / "railway-volume"
        self.config_path.unlink()
        self.environment.stop()
        self.environment = patch.dict(os.environ, {
            "RAILWAY_VOLUME_MOUNT_PATH": str(mount_path),
        }, clear=True)
        self.environment.start()
        self.config_path = mount_path / "streams.json"

        status, payload = self.request_json("/api/state")

        self.assertEqual(status, 200)
        self.assertEqual(payload["streams"], [])
        self.assertTrue(self.config_path.is_file())
        self.assertTrue((mount_path / "videos").is_dir())

    def test_returns_private_and_external_urls(self):
        self.environment.stop()
        self.environment = patch.dict(os.environ, {
            "VIDEO_DIR": str(self.video_directory),
            "STREAMS_CONFIG": str(self.config_path),
            "CONTROL_PANEL_HOST": "127.0.0.1",
            "CONTROL_PANEL_PORT": str(self.server.server_address[1]),
            "RTSP_PRIVATE_HOST": "simulator.railway.internal",
            "RTSP_PRIVATE_PORT": "8554",
            "RTSP_EXTERNAL_HOST": "proxy.rlwy.net",
            "RTSP_EXTERNAL_PORT": "12345",
        }, clear=True)
        self.environment.start()
        self.config_path.write_text(
            json.dumps({
                "streams": [{
                    "name": "test-camera",
                    "file": "missing.mp4",
                    "fps": 15,
                    "bitrate": "1200k",
                }]
            }),
            encoding="utf-8",
        )

        status, payload = self.request_json("/api/state")

        self.assertEqual(status, 200)
        self.assertEqual(
            payload["streams"][0]["private_url"],
            "rtsp://simulator.railway.internal:8554/test-camera",
        )
        self.assertEqual(
            payload["streams"][0]["external_url"],
            "rtsp://proxy.rlwy.net:12345/test-camera",
        )


class PublisherReloadTests(unittest.TestCase):
    def test_changed_stream_restarts_publisher_without_restarting_server(self):
        class StopAfterReload:
            def __init__(self):
                self.wait_count = 0
                self.stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, _timeout):
                self.wait_count += 1
                if self.wait_count == 2:
                    self.stopped = True
                return self.stopped

            def set(self):
                self.stopped = True

        initial_stream = {
            "name": "test-camera",
            "file": Path("clip.mp4"),
            "fps": 15,
            "bitrate": "900k",
            "url": "rtsp://mediamtx:8554/test-camera",
        }
        changed_stream = {**initial_stream, "fps": 20}
        first_process = Mock()
        first_process.poll.return_value = None
        second_process = Mock()
        second_process.poll.return_value = None
        server = Mock()
        server.server_address = ("127.0.0.1", 8765)
        server_thread = Mock()
        stop = StopAfterReload()

        with (
            patch("control_panel.start_server", return_value=(server, server_thread)),
            patch("publisher.load_streams", side_effect=[
                [initial_stream],
                [changed_stream],
            ]),
            patch("publisher.subprocess.Popen", side_effect=[
                first_process,
                second_process,
            ]) as popen,
            patch("publisher.STOP", stop),
        ):
            publisher.run()

        self.assertEqual(popen.call_count, 2)
        first_process.terminate.assert_called_once_with()
        first_process.wait.assert_called_once_with(timeout=10)
        server.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        server_thread.join.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()

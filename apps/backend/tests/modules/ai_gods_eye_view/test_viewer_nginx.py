"""Exercise the deployed proxy configuration with a real Nginx and a local auth stub."""
import http.server
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request


@unittest.skipUnless(shutil.which("nginx"), "Requires the application's Nginx image")
class ViewerProxyTest(unittest.TestCase):
    def test_identity_outage_is_retryable_without_forwarding_unauthorized_requests(self):
        state = {"auth": 503, "forwarded": 0}
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                if self.path == "/api/gods-eye-view/session":
                    self.send_response(state["auth"])
                    self.send_header("X-Gods-Eye-View-User", "viewer:reader")
                    self.send_header("X-Gods-Eye-View-Role", "reader")
                else:
                    state["forwarded"] += 1
                    self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"configured":true}')
        backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        process = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = (Path(__file__).resolve().parents[5] / "docker/nginx.conf").read_text()
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                # TLS is outside this auth-subrequest check; all deployed locations remain intact.
                config = config.replace("worker_processes auto;", "worker_processes 1;")
                config = config.replace("pid /run/nginx.pid;", f"pid {root}/nginx.pid;")
                config = config.replace("listen 80 default_server;", f"listen unix:{root}/http.sock default_server;")
                config = config.replace("listen 443 ssl default_server;", f"listen 127.0.0.1:{port} default_server;")
                config = "\n".join(line for line in config.splitlines() if "ssl_certificate" not in line)
                config = config.replace("/var/log/nginx/access.log", "/dev/null").replace("/var/log/nginx/error.log", "/dev/null")
                for upstream in (8000, 8081):
                    config = config.replace(f"127.0.0.1:{upstream}", f"127.0.0.1:{backend.server_port}")
                path = root / "nginx.conf"
                path.write_text(config)
                subprocess.run(["nginx", "-t", "-c", str(path)], check=True, capture_output=True)
                process = subprocess.Popen(["nginx", "-c", str(path), "-g", "daemon off;"], stderr=subprocess.DEVNULL)
                for attempt in range(100):
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        if process.poll() is not None or attempt == 99:
                            self.fail("Nginx failed to start")
                        time.sleep(0.01)
                for auth_status, expected in ((503, 503), (401, 401), (403, 403), (200, 200)):
                    state.update(auth=auth_status, forwarded=0)
                    for route in ("/api/gods-eye-view/oci-provider", "/api/gods-eye-view/oci-voice"):
                        try:
                            response = urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=2)
                        except urllib.error.HTTPError as error:
                            response = error
                        with response:
                            self.assertEqual(response.status, expected)
                            body = response.read()
                            if expected == 503:
                                self.assertEqual(response.headers["Retry-After"], "2")
                                self.assertIn(b"Viewer access could not be verified", body)
                    self.assertEqual(state["forwarded"], 2 if expected == 200 else 0)
        finally:
            if process is not None:
                process.terminate()
                process.wait(timeout=5)
            backend.shutdown()
            backend.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

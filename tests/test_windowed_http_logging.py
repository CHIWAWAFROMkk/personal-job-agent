"""The desktop executable has no console streams; requests must still work."""
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.request import build_opener, ProxyHandler

from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.job_repository import JobRepository


def test_health_without_stderr():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        server = create_dashboard_server(JobRepository(root / 'jobs.sqlite3'), output_dir=root / 'output', port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch('sys.stderr', None):
                with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{server.server_port}/api/health', timeout=5) as response:
                    assert response.status == 200
        finally:
            server.shutdown()
            server.server_close()

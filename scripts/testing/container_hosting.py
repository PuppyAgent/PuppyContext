"""Container-side local test runner; byte-transparent proxies retain loopback guards.

Only the exact runner-owned Supabase services are reachable through these
listeners. No Docker socket, host credentials or production settings are needed.
"""
from __future__ import annotations

import json
import os
import platform
import re
import select
import socket
import socketserver
import subprocess
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse


def destinations(environ):
    stack = environ.get('HOSTING_TEST_STACK', '')
    if not re.fullmatch(r'puppy-baseline-[a-z0-9]{8}', stack):
        raise ValueError('container runner requires an owned stack')
    if environ.get('HOSTING_TEST_SUPABASE') != '1':
        raise ValueError('container runner requires actual Supabase')
    db = urlparse(environ.get('HOSTING_TEST_DB_URL', ''))
    api = urlparse(environ.get('SUPABASE_URL', ''))
    if (db.hostname not in {'127.0.0.1', 'localhost'} or not db.port
            or api.hostname not in {'127.0.0.1', 'localhost'} or not api.port or api.port == db.port):
        raise ValueError('container service listeners require distinct owned loopback ports')
    return [(db.port, 'supabase_db_' + stack, 5432),
            (api.port, 'supabase_kong_' + stack, 8000)]


class Bridge(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, destination):
        self.destination = destination
        self.slots = threading.BoundedSemaphore(128)
        super().__init__(('127.0.0.1', port), Forward)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Forward(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(self.server.destination, timeout=15) as remote:
                self.request.settimeout(30)
                remote.settimeout(30)
                while True:
                    ready, _, _ = select.select([self.request, remote], [], [], 30)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (remote if source is self.request else self.request).sendall(data)
        except (OSError, TimeoutError):
            # The caller observes an actual connection failure; no success body
            # is fabricated and the acceptance gate still requires real tests.
            return


def main():
    args = sys.argv[1:]
    if args[:2] != ['-m', 'pytest']:
        raise ValueError('container entrypoint accepts pytest only')
    if os.environ.get('SKIP_AUTH') != 'false' or os.environ.get('APP_ENV') != 'test':
        raise ValueError('container acceptance requires authenticated test settings')
    Path('/tmp/home').mkdir(exist_ok=True)
    hook = Path('/tmp/hosting-hook-probe')
    hook.write_text('#!/bin/sh\nexit 0\n')
    hook.chmod(0o700)
    try:
        subprocess.run([str(hook)], check=True, timeout=5)
    finally:
        hook.unlink()
    with ExitStack() as cleanup:
        for port, host, remote_port in destinations(os.environ):
            bridge = Bridge(port, (host, remote_port))
            cleanup.callback(bridge.server_close)
            cleanup.callback(bridge.shutdown)
            threading.Thread(target=bridge.serve_forever, daemon=True).start()
        # Import only after the test environment and loopback listeners exist.
        from src.config import settings
        from src.infra.supabase.client import SupabaseClient
        if settings.SKIP_AUTH:
            raise ValueError('application settings do not match owned container services')
        # Supabase configuration belongs to this production client, not Settings.
        # Exercise the connection, not merely the presence of environment keys.
        SupabaseClient().get_client().table('version_repositories').select('project_id').limit(1).execute()
        receipt = {
            'platform': platform.platform(), 'python': sys.version,
            'git': subprocess.check_output(['git', '--version'], text=True).strip(),
            'psql': subprocess.check_output(['psql', '--version'], text=True).strip(),
            'environment_names': sorted(k for k in os.environ if k.startswith(('HOSTING_TEST_', 'S3_', 'SUPABASE_'))),
            'skip_auth': settings.SKIP_AUTH, 'dotenv_inherited': False,
            'startup_checks': ['settings', 'production_supabase_client', 'executable_test_tmpfs'],
            'services': ['owned_postgres', 'owned_auth_postgrest', 'owned_s3'],
        }
        Path('/evidence/container-environment.json').write_text(json.dumps(receipt, indent=2) + '\n')
        return subprocess.call([sys.executable, *args, '-o', 'cache_dir=/tmp/pytest-cache'])


if __name__ == '__main__':
    sys.exit(main())

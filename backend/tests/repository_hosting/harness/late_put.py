"""Owned loopback S3 proxy: one buffered PUT survives its originating worker.

It forwards real signed requests, not an S3 mock. No headers, credentials or
payloads are logged. It cannot forward outside the selected Project namespace.
"""
from __future__ import annotations

import http.client
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_HOP_HEADERS = {'connection', 'proxy-connection', 'keep-alive', 'transfer-encoding', 'expect'}


class LatePut:
    def __init__(self, endpoint, bucket, project, key):
        self.origin = urlsplit(endpoint)
        if self.origin.scheme != 'http' or self.origin.hostname != '127.0.0.1' or not self.origin.port:
            raise ValueError('late PUT requires owned loopback S3')
        self.prefix = f'{self.origin.path}/{bucket}/version/{project}/'
        self.held_path = f'{self.origin.path}/{bucket}/{key}'
        if not self.held_path.startswith(self.prefix):
            raise ValueError('late PUT escaped its Project')
        self.started, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.selected = False
        self.status = None
        self.forward_started = None
        self.errors = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass  # Never log signed headers or request data.

            def forward(self):
                held = False
                upstream = None
                try:
                    self.connection.settimeout(5)
                    target = urlsplit(self.path)
                    if ((target.scheme, target.hostname, target.port) !=
                            (owner.origin.scheme, owner.origin.hostname, owner.origin.port)
                            or target.username or target.password or target.fragment
                            or not target.path.startswith(owner.prefix)):
                        raise ValueError('proxy target is outside its owned Project')
                    if self.headers.get('Transfer-Encoding'):
                        raise ValueError('fixture requires bounded non-chunked request input')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= length <= 1024 * 1024:
                        raise ValueError('fixture request byte budget exceeded')
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError('incomplete fixture request')
                    with owner.lock:
                        if self.command == 'PUT' and target.path == owner.held_path and not owner.selected:
                            owner.selected = held = True
                    if held:
                        owner.started.set()  # Complete signed request buffered outside the worker.
                        if not owner.release.wait(45):
                            raise TimeoutError('owned late PUT was not released')
                        owner.forward_started = time.monotonic()
                    upstream = http.client.HTTPConnection(target.hostname, target.port, timeout=15)
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_HEADERS}
                    path = target.path + ('?' + target.query if target.query else '')
                    upstream.request(self.command, path, body=body, headers=headers)
                    response = upstream.getresponse()
                    data = response.read(1024 * 1024 + 1)
                    if len(data) > 1024 * 1024:
                        raise ValueError('fixture response byte budget exceeded')
                    if held:
                        owner.status = response.status
                    self.send_response_only(response.status)
                    for name, value in response.getheaders():
                        if name.lower() not in _HOP_HEADERS | {'content-length'}:
                            self.send_header(name, value)
                    length = response.getheader('Content-Length', '0') if self.command == 'HEAD' else str(len(data))
                    self.send_header('Content-Length', length)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    if self.command != 'HEAD':
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    if not held or owner.status is None:
                        owner.errors.append('unexpected client disconnect')
                except Exception as exc:
                    owner.errors.append(type(exc).__name__)
                    self.close_connection = True
                finally:
                    if upstream is not None:
                        upstream.close()
                    if held:
                        owner.finished.set()

            do_GET = do_HEAD = do_PUT = forward

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name='hosting-late-put')
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()  # Join owned handlers; never abandon remote work.
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('owned late PUT server did not stop')

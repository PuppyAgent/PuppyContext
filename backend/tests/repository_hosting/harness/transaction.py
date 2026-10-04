"""Owned psql transaction with bounded, observable command barriers."""
from __future__ import annotations

import os
import select
import subprocess
import time
import uuid
from contextlib import contextmanager


class Transaction:
    def __init__(self, process):
        self.process = process

    def execute(self, statement):
        marker = ('barrier-' + uuid.uuid4().hex).encode()
        self.process.stdin.write((statement + ";SELECT '" + marker.decode() + "';\n").encode())
        self.process.stdin.flush()
        output, deadline = b'', time.monotonic() + 10
        while marker + b'\n' not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise AssertionError('owned SQL transaction barrier timed out')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise AssertionError('owned SQL transaction exited: ' + self.process.stderr.read().decode()[-2000:])
            output += chunk
        return output.split(marker + b'\n', 1)[0].decode().strip()


@contextmanager
def transaction(pg, statement=''):
    process = subprocess.Popen(['psql', pg.url, '-X', '-qAt', '-v', 'ON_ERROR_STOP=1'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    session = Transaction(process)
    try:
        session.execute('BEGIN;' + statement)
        yield session
    finally:
        try:
            if process.poll() is None:
                process.stdin.write(b'ROLLBACK;\n\\q\n')
                process.stdin.flush()
            process.communicate(timeout=10)
        except BrokenPipeError:
            process.kill()
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate(timeout=10)
            raise AssertionError('owned SQL transaction cleanup timed out') from exc


def wait_for_lock(pg, application_name):
    from .postgres import literal
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        waiting = pg.value("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name="
                           + literal(application_name) + " AND wait_event_type='Lock')")
        if waiting == 't':
            return
        time.sleep(0.02)
    raise AssertionError('competing SQL session did not reach the expected lock')

"""Disposable worker process; synthetic RuntimeGrant, real current SQL admission.

Original input comes from the test supervisor, not a production durable producer.
Secrets are inherited only from the owned stack environment, never argv/output.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.config import Config
from supabase import ClientOptions, create_client

from src.infra.s3.service import S3Service
from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.platform.repository_target.models import ProjectRootTarget, ResolvedRepositoryView
from src.version_engine.adapters.product.tree_patch import splice_batch
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.write_engine.native_operation_writer import NativeOperationWriter
from tests.repository_hosting.harness.s3_service import validate_s3_environment


async def run(request):
    validate_s3_environment(os.environ)
    proxy = urlsplit(request['proxy'])
    if (proxy.scheme != 'http' or proxy.hostname != '127.0.0.1' or not proxy.port
            or proxy.port < 1024 or proxy.path or proxy.query or proxy.fragment or proxy.username or proxy.password):
        raise ValueError('worker proxy must be an owned loopback listener')
    with httpx.Client(timeout=15, trust_env=False) as http:
        client = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'],
                               ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False))
        s3 = S3Service()
        try:
            s3.client.close()
            # Keep the original signed S3 endpoint. Only this worker's source
            # client uses the fault proxy; no global/environment proxy change.
            s3.client = boto3.client('s3', endpoint_url=s3.endpoint_url, region_name=s3.region,
                aws_access_key_id=s3.access_key_id, aws_secret_access_key=s3.secret_access_key,
                config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                              proxies={'http': request['proxy']}, connect_timeout=5, read_timeout=15,
                              retries={'max_attempts': 1}))
            target = ProjectRootTarget(request['project_id'])
            grant = RuntimeGrant(RuntimePrincipal(request['credential_id'], 'git_http_token'), target,
                                 ResolvedRepositoryView(target, '', (), 'rw'), RuntimeMode.READ_WRITE)
            service = VersionRepoManager(s3, SimpleNamespace(client=client)).get_native_service(target.project_id)
            writer = NativeOperationWriter(service)
            arguments = {name: request[name] for name in ('request_key', 'base', 'input_sha256', 'message')}
            # Match production ingress: exact current-read replay precedes lease.
            result = await asyncio.to_thread(writer.replay, grant, **arguments)
            if result is None:
                async with ProjectWriteLease(target.project_id, 'hosting-independent-product', ttl_seconds=30,
                                             repository=ProjectWriteLeaseRepository(client)):
                    result = await asyncio.to_thread(writer.apply, grant, **arguments,
                        splice=lambda store, root: splice_batch(store, root, [('put', 'file', b'data')]))
            return {'pid': os.getpid(), 'result': result}
        finally:
            s3.close()


if __name__ == '__main__':
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise ValueError('worker request budget exceeded')
    print('HOSTING_RESULT=' + json.dumps(asyncio.run(run(json.loads(raw)))), flush=True)

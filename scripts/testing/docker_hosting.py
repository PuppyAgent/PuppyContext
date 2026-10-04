"""Host-side orchestration for a disposable, local-only Linux test container."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

ENVIRONMENT_KEYS = frozenset({
    'APP_ENV', 'SKIP_AUTH', 'MANAGED_AI_ENABLED', 'HOSTING_TEST_STACK',
    'HOSTING_TEST_DB_URL', 'HOSTING_TEST_SUPABASE', 'HOSTING_TEST_ANON_KEY',
    'HOSTING_TEST_S3', 'SUPABASE_URL', 'SUPABASE_KEY', 'SUPABASE_SERVICE_ROLE_KEY',
    'S3_ENDPOINT_URL', 'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY', 'S3_REGION', 'S3_BUCKET_NAME',
    'JWT_SECRET',
})


def check_source(root):
    for relative in ('.env', 'backend/.env', 'backend/mcp_service/.env'):
        if (root / relative).exists():
            raise ValueError('Docker acceptance refuses source worktrees containing dotenv files')


def local_docker(environ):
    host = environ.get('DOCKER_HOST') or subprocess.check_output(
        ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
        env=environ, text=True, timeout=15).strip()
    if not host.startswith(('unix://', 'npipe://')):
        raise ValueError('Docker acceptance requires a local daemon')


def build_image(root, environ):
    local_docker(environ)
    check_source(root)
    paths = [root / 'scripts/testing/Dockerfile.hosting', root / 'backend/pyproject.toml', root / 'backend/uv.lock']
    digest = hashlib.sha256(b'\0'.join(path.read_bytes() for path in paths)).hexdigest()
    tag = 'puppyone-issue-062-tests:' + digest[:20]
    exists = subprocess.run(['docker', 'image', 'inspect', tag], env=environ,
                            capture_output=True, timeout=15, check=False)
    if exists.returncode:
        # Copy only public dependency manifests, never .env, .git, credentials,
        # the host virtualenv, or unrelated files into the build context.
        with tempfile.TemporaryDirectory(prefix='issue-062-docker-build-') as directory:
            for path in paths:
                shutil.copyfile(path, Path(directory) / ('Dockerfile' if path.name == 'Dockerfile.hosting' else path.name))
            subprocess.run(['docker', 'build', '--label', 'puppyone.owner=issue-062', '-t', tag, directory],
                           env=environ, check=True, timeout=900)
    image = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag],
                                    env=environ, text=True, timeout=15).strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('Docker image identity is missing')
    return image


def container_environment(environ):
    values = {key: value for key, value in environ.items() if key in ENVIRONMENT_KEYS}
    stack = values.get('HOSTING_TEST_STACK', '')
    if not re.fullmatch(r'puppy-baseline-[a-z0-9]{8}', stack):
        raise ValueError('Docker runner requires an owned Supabase stack')
    if values.get('HOSTING_TEST_SUPABASE') != '1' or values.get('HOSTING_TEST_S3') != '1':
        raise ValueError('Docker acceptance requires actual Auth/PostgREST/S3')
    if values.get('SKIP_AUTH') != 'false' or values.get('APP_ENV') != 'test':
        raise ValueError('Docker acceptance cannot bypass authentication')
    for key in ('SUPABASE_KEY', 'SUPABASE_SERVICE_ROLE_KEY', 'HOSTING_TEST_ANON_KEY', 'JWT_SECRET',
                'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY', 'S3_REGION'):
        if not values.get(key):
            raise ValueError('missing Docker test configuration: ' + key)
    if values.get('S3_BUCKET_NAME') != 'hosting-' + stack:
        raise ValueError('Docker bucket does not belong to the test stack')
    api = urlparse(values.get('SUPABASE_URL', ''))
    db = urlparse(values.get('HOSTING_TEST_DB_URL', ''))
    if (api.scheme != 'http' or api.hostname not in {'127.0.0.1', 'localhost'}
            or api.path not in {'', '/'} or api.username or api.password or api.query or api.fragment
            or db.scheme != 'postgresql' or db.hostname not in {'127.0.0.1', 'localhost'}
            or db.path != '/postgres'
            or values.get('S3_ENDPOINT_URL') != values.get('SUPABASE_URL', '').rstrip('/') + '/storage/v1/s3'):
        raise ValueError('Docker services must originate from owned loopback configuration')
    if not api.port or not db.port or api.port == db.port:
        raise ValueError('owned API and PostgreSQL require distinct explicit ports')
    # Keep the declared public origin byte-for-byte. Kong supplies that port to
    # Storage's SigV4 verifier; substituting a container-local port breaks S3 auth.
    values.update(NO_PROXY='*', LANG='C.UTF-8', LC_ALL='C.UTF-8')
    if any('\n' in value or '\r' in value or '\0' in value for value in values.values()):
        raise ValueError('invalid multiline Docker environment value')
    return values


def owned_network(stack, environ):
    if not re.fullmatch(r'puppy-baseline-[a-z0-9]{8}', stack):
        raise ValueError('unowned Docker stack')
    networks = []
    for service in ('db', 'kong'):
        raw = subprocess.check_output(
            ['docker', 'inspect', '--format', '{{json .NetworkSettings.Networks}}', f'supabase_{service}_{stack}'],
            env=environ, text=True, timeout=15)
        networks.append(set(json.loads(raw)))
    shared = networks[0] & networks[1]
    expected = 'supabase_network_' + stack
    if shared != {expected}:
        raise ValueError('Supabase services do not share the expected owned network')
    return expected


def run_tests(root, output, command, environ, cli_env, image, result):
    check_source(root)
    values = container_environment(environ)
    network = owned_network(values['HOSTING_TEST_STACK'], cli_env)
    if output == root or output in root.parents or output == root / 'backend':
        raise ValueError('evidence mount must not expose the source tree for writing')
    args = [('--junitxml=/evidence/junit.xml' if arg.startswith('--junitxml=') else arg) for arg in command[1:]]
    name = 'issue-062-tests-' + values['HOSTING_TEST_STACK'].removeprefix('puppy-baseline-')
    environment_receipt = output / 'container-environment.json'
    environment_receipt.unlink(missing_ok=True)
    resource_receipt = output / 'container-resources.json'
    resource_receipt.unlink(missing_ok=True)
    redis_name = 'issue-062-redis-' + values['HOSTING_TEST_STACK'].removeprefix('puppy-baseline-')
    values.update(AUTH_SECURITY_REDIS_URL=f'redis://{redis_name}:6379/1',
                  NOTIFICATIONS_REDIS_URL=f'redis://{redis_name}:6379/2',
                  ETL_REDIS_URL=f'redis://{redis_name}:6379/0')
    with tempfile.TemporaryDirectory(prefix='issue-062-docker-env-') as directory:
        path = Path(directory) / 'test.env'
        path.write_text(''.join(f'{key}={value}\n' for key, value in sorted(values.items())))
        path.chmod(0o600)
        run = ['docker', 'run', '--rm', '--init', '--name', name, '--label', 'puppyone.owner=issue-062',
               '--network', network, '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--memory=4g', '--cpus=4', '--pids-limit=1024', '--tmpfs', '/tmp:rw,nosuid,exec,size=2g',
               '--mount', f'type=bind,src={root},dst=/source,readonly',
               '--mount', f'type=bind,src={output},dst=/evidence',
               '--env-file', str(path), image, *args]
        try:
            subprocess.run(['docker', 'run', '-d', '--rm', '--name', redis_name,
                            '--label', 'puppyone.owner=issue-062', '--network', network,
                            '--memory=128m', '--cpus=1', '--pids-limit=128', 'redis:6-alpine'],
                           env=cli_env, stdout=subprocess.DEVNULL, check=True, timeout=120)
            completed = subprocess.run(run, env=cli_env, check=False, timeout=3600)
            if environment_receipt.exists():
                metadata = json.loads(environment_receipt.read_text())
                result['container_environment'] = metadata
                result['orchestrator_environment'] = {
                    'git_version': result.get('git_version'), 'python_version': result.get('python_version'),
                }
                result['git_version'] = metadata['git']
                result['python_version'] = metadata['python']
            else:
                result['infrastructure_error'] = 'Docker environment receipt missing'
            if resource_receipt.exists():
                result['container_resources'] = json.loads(resource_receipt.read_text())
                if result['container_resources']['failures']:
                    result['infrastructure_error'] = 'Docker resource exhaustion'
            else:
                result['infrastructure_error'] = 'Docker resource receipt missing'
            return completed.returncode
        finally:
            # One exact owned name, never daemon restart, compose down, or prune.
            subprocess.run(['docker', 'rm', '-f', name], env=cli_env, capture_output=True, timeout=30, check=False)
            subprocess.run(['docker', 'rm', '-f', redis_name], env=cli_env, capture_output=True, timeout=30, check=False)

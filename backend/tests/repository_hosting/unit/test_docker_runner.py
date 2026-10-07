"""Docker configuration must not inherit secrets or weaken owned-service guards."""
import importlib.util
from pathlib import Path

import pytest

from tests.repository_hosting.unit.test_evidence_runner import runner

pytestmark = pytest.mark.hosting_component
ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('docker_hosting', ROOT / 'scripts/testing/docker_hosting.py')
docker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docker)
spec = importlib.util.spec_from_file_location('container_hosting', ROOT / 'scripts/testing/container_hosting.py')
container = importlib.util.module_from_spec(spec)
spec.loader.exec_module(container)


@pytest.mark.parametrize('event,name', [('pids.events', 'max'), ('memory.events', 'oom'),
                                      ('memory.events', 'oom_kill'), ('memory.events', 'oom_group_kill')])
def test_resource_exhaustion_never_becomes_a_successful_run(event, name):
    before = {'pids.events': {}, 'memory.events': {}}
    after = {'pids.events': {}, 'memory.events': {}}
    assert container.resource_failures(before, after) == []
    after[event][name] = 1
    assert container.resource_failures(before, after) == [event + ':' + name]


def test_resource_receipt_requires_real_cgroup_counters(tmp_path):
    with pytest.raises(FileNotFoundError):
        container.resources(tmp_path)
    for name, value in {'pids.current': '3', 'pids.max': '1024', 'memory.peak': '40',
                        'memory.max': '4096', 'pids.events': 'max 0', 'memory.events': 'oom 0'}.items():
        (tmp_path / name).write_text(value)
    result = container.resources(tmp_path)
    assert result['pids.events'] == {'max': 0}
    assert result['memory.events'] == {'oom': 0}


def environment():
    return dict(APP_ENV='test', SKIP_AUTH='false', MANAGED_AI_ENABLED='false', JWT_SECRET='owned-jwt-secret',
                HOSTING_TEST_STACK='puppy-baseline-1234abcd', HOSTING_TEST_SUPABASE='1', HOSTING_TEST_S3='1',
                HOSTING_TEST_DB_URL='postgresql://postgres:postgres@127.0.0.1:54322/postgres',
                SUPABASE_URL='http://127.0.0.1:54321', SUPABASE_KEY='owned-service',
                SUPABASE_SERVICE_ROLE_KEY='owned-service', HOSTING_TEST_ANON_KEY='owned-anon',
                S3_BUCKET_NAME='hosting-puppy-baseline-1234abcd', S3_ACCESS_KEY_ID='owned-access',
                S3_SECRET_ACCESS_KEY='owned-secret', S3_REGION='local',
                S3_ENDPOINT_URL='http://127.0.0.1:54321/storage/v1/s3')


def test_container_configuration_drops_unrelated_secrets_and_proxy_settings():
    values = docker.container_environment(environment() | {
        'OPENAI_API_KEY': 'must-not-inherit', 'HTTP_PROXY': 'http://untrusted',
        'AWS_SECRET_ACCESS_KEY': 'must-not-inherit', 'SUPABASE_ACCESS_TOKEN': 'must-not-inherit',
        'GIT_CONFIG_GLOBAL': '/host/private',
    })
    assert not any('must-not-inherit' in value or 'untrusted' in value for value in values.values())
    assert 'GIT_CONFIG_GLOBAL' not in values
    assert values['SUPABASE_URL'] == environment()['SUPABASE_URL']
    assert values['HOSTING_TEST_DB_URL'] == environment()['HOSTING_TEST_DB_URL']
    assert values['S3_ENDPOINT_URL'] == values['SUPABASE_URL'] + '/storage/v1/s3'
    assert values['NO_PROXY'] == '*'


@pytest.mark.parametrize('key,value', [
    ('HOSTING_TEST_STACK', 'production'), ('HOSTING_TEST_SUPABASE', '0'), ('HOSTING_TEST_S3', '0'),
    ('SKIP_AUTH', 'true'), ('APP_ENV', 'production'), ('SUPABASE_KEY', ''), ('JWT_SECRET', ''),
    ('S3_SECRET_ACCESS_KEY', ''), ('HOSTING_TEST_ANON_KEY', ''), ('S3_BUCKET_NAME', 'production'),
    ('SUPABASE_URL', 'https://hosted.example'), ('S3_ENDPOINT_URL', 'https://external.example'),
    ('HOSTING_TEST_DB_URL', 'postgresql://user@production/db'), ('S3_REGION', 'local\nBAD=value'),
    ('HOSTING_TEST_DB_URL', 'postgresql://postgres:postgres@127.0.0.1:54321/postgres'),
])
def test_container_configuration_fails_closed(key, value):
    with pytest.raises(ValueError):
        docker.container_environment(environment() | {key: value})


@pytest.mark.parametrize('path', ['.env', 'backend/.env', 'backend/mcp_service/.env'])
def test_source_dotenv_never_enters_docker(tmp_path, path):
    secret = tmp_path / path
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('not-a-test-secret')
    with pytest.raises(ValueError, match='dotenv'):
        docker.check_source(tmp_path)


@pytest.mark.parametrize('resources', [None, {}, {'init_process': 'docker-init', 'failures': []},
                                     {'init_process': 'docker-init', 'before': {'pids': 1},
                                      'after': {'pids': 5}, 'failures': ['pids.events:max']}])
def test_missing_or_failed_resource_receipt_fails_strict_acceptance(resources):
    result = dict(execution_environment='docker', docker_image='sha256:test',
                  container_environment={'skip_auth': False, 'dotenv_inherited': False},
                  container_resources=resources, pytest_exit=0, target=True,
                  layers={'hosting_component': {'passed': 1}})
    assert runner.result_exit_code(result) == 1


def test_actual_application_layer_cannot_be_omitted_when_requested():
    result = dict(application=True, pytest_exit=0, target=True, layers={'hosting_s3': {'passed': 1}})
    assert runner.result_exit_code(result) == 1
    result['layers']['hosting_application'] = {'passed': 1}
    assert runner.result_exit_code(result) == 0


def test_remote_docker_daemon_is_not_accepted():
    with pytest.raises(ValueError, match='local daemon'):
        docker.local_docker({'DOCKER_HOST': 'tcp://remote.example:2376'})


@pytest.mark.parametrize('image,metadata', [(None, {}), ('sha256:fake', {}), (None, {'platform': 'linux'})])
def test_docker_acceptance_requires_actual_container_evidence(image, metadata):
    result = dict(execution_environment='docker', docker_image=image, container_environment=metadata,
                  pytest_exit=0, target=True, layers={'hosting_component': {'passed': 1}})
    assert runner.result_exit_code(result) != 0

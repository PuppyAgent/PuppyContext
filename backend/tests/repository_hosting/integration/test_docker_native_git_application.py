"""Real credential -> canonical router -> checked native PG/S3 publication.

Enrollment and entitlement projection are synthetic owner-installed facts; this
is neither migration acceptance nor external PuppyPay/product-write acceptance.
Selected Product read APIs use real JWT admission and the same native refs.
"""
import base64
import secrets
import uuid
from types import SimpleNamespace

import pytest

from tests.repository_hosting.harness.application import Application
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.integration.test_docker_application import authorize_git
from tests.repository_hosting.integration.test_repository_file_policy import enroll_file_policy
from tests.repository_hosting.integration.test_repository_logical_billing import enroll_billing

pytestmark = pytest.mark.hosting_application


@pytest.fixture
def application(tmp_path):
    app = Application(tmp_path, profile='native Git and selected Product reads; synthetic enrollment/entitlements; Product writes/Scope not accepted')
    try:
        app.start()
        yield app
    finally:
        app.close()


def enroll_empty_native(pg, project, org, format):
    # Do not hide a migration/backfill in this routing test or invent durability
    # receipts. This Project has never acknowledged a legacy Git commit.
    assert pg.value(f'SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)}') == '0'
    pg.sql(f"""
      INSERT INTO public.version_repositories(project_id,authority,object_format)
      VALUES({literal(project)},'native',{literal(format)});
      INSERT INTO public.version_repository_refs(project_id,name,object_format,symbolic_target)
      VALUES({literal(project)},decode('48454144','hex'),{literal(format)},decode({literal(b'refs/heads/trunk'.hex())},'hex'));
      INSERT INTO public.version_organization_capacity(org_id,initialized) VALUES({literal(org)},true);
      INSERT INTO public.version_repository_capacity(project_id,org_id,initialized) VALUES({literal(project)},{literal(org)},true);
    """)
    a = enroll_billing(SimpleNamespace(pg=pg, project=project, org=org))
    enroll_file_policy(a, maximum=64)
    pg.sql(f"UPDATE public.organization_entitlements SET entitlements=jsonb_set(entitlements,'{{limits,storage.max_bytes}}','4096'::jsonb) WHERE org_id={literal(org)}")


def assert_native_product_reads(app, pg, project, commit, format):
    legacy = pg.value(f'SELECT version_root_hash FROM public.projects WHERE id={literal(project)}')
    sequence = pg.value(f'SELECT ref_sequence FROM public.version_repositories WHERE project_id={literal(project)}')
    for action in ('ls', 'tree'):
        listing = app.api('GET', f'/content/{project}/{action}')
        assert [e['name'] for e in listing['entries']] == ['readme']
        assert listing['entries'][0]['git_mode'] == '100644'
        assert listing['head_commit_id'] == commit
        assert listing['repository_revision']['target_ref'] == 'refs/heads/trunk'
        assert listing['repository_revision']['expected_oid'] == commit
        assert listing['repository_revision']['object_format'] == format
    cat = app.api('GET', f'/content/{project}/cat', params={'path': 'readme'})
    assert cat['content_text'] == 'hello\n' and cat['head_commit_id'] == commit
    stat = app.api('GET', f'/content/{project}/stat', params={'path': 'readme'})
    assert stat['exists'] and stat['head_commit_id'] == commit and stat['git_mode'] == '100644'
    raw = app.request('GET', f'/api/v1/content/{project}/raw', params={'path_bytes_b64': base64.b64encode(b'readme').decode()})
    assert raw.content == b'hello\n'
    app.request('GET', f'/api/v1/content/{project}/ls', token=False, expected=401)
    assert pg.value(f'SELECT version_root_hash FROM public.projects WHERE id={literal(project)}') == legacy
    assert pg.value(f'SELECT ref_sequence FROM public.version_repositories WHERE project_id={literal(project)}') == sequence
    assert pg.value(f'SELECT count(*) FROM public.version_commits WHERE project_id={literal(project)}') == '0'


@pytest.mark.parametrize('format', ['sha1', 'sha256'])
def test_docker_native_git_canonical_auth_refs_policy_cold_restart(application, tmp_path, format):
    app, pg = application, Postgres()
    org = app.api('POST', '/organizations/', expected=201, json={'name': 'Native Git routing'})['id']
    project = app.api('POST', '/projects/', expected=201,
                      headers={'Idempotency-Key': str(uuid.uuid4())},
                      json={'name': 'Native repository', 'org_id': org})['id']
    credential = 'pwg_' + secrets.token_urlsafe(32)
    issued = app.api('POST', f'/projects/{project}/git-credentials', expected=201,
                     headers={'Idempotency-Key': str(uuid.uuid4())}, json={
                         'target': {'kind': 'project_root', 'project_id': project},
                         'mode': 'rw', 'credential': credential,
                     })
    remote = issued['remote']['url']
    assert remote == app.url + f'/git/{project}.git' and credential not in remote
    enroll_empty_native(pg, project, org, format)
    headers = {'Authorization': 'Basic '+base64.b64encode(('x:'+credential).encode()).decode()}
    health_path = f'/git/{project}.git/health'
    health = app.client.get(health_path, headers=headers)
    assert health.status_code == 200, health.text
    assert health.json()['data']['health'] == 'empty'
    assert health.json()['data']['object_format'] == format
    client = Git.init(tmp_path / 'native-client', format=format)
    authorize_git(client, credential)
    client.run('branch', '-m', 'trunk')
    client.run('remote', 'add', 'origin', remote)
    first = client.commit({'readme': b'hello\n'})
    client.run('push', '-u', 'origin', 'trunk')
    # All-ref batches and typed tags traverse the public route, not the internal
    # ASGI native fixture. Default HEAD is metadata, not hard-coded main.
    client.run('branch', 'topic')
    client.run('tag', '-a', 'annotated', '-m', 'retained tag')
    blob = client.text('rev-parse', first+':readme')
    client.run('tag', 'blob', blob)
    client.run('push', '--atomic', 'origin', 'topic', 'refs/tags/annotated', 'refs/tags/blob')
    before = client.run('ls-remote', '--symref', 'origin').stdout
    assert b'ref: refs/heads/trunk\tHEAD' in before and b'refs/heads/main' not in before
    assert_native_product_reads(app, pg, project, first, format)
    rejected = client.commit({'too-large': b'x'*65})
    oversized = client.text('rev-parse', rejected+':too-large')
    assert client.run('push', 'origin', 'trunk', check=False).returncode != 0
    assert client.run('ls-remote', '--symref', 'origin').stdout == before
    client.run('reset', '--hard', first)
    assert pg.value(f"SELECT count(*) FROM public.version_repository_object_capacity WHERE project_id={literal(project)} AND object_id={literal(oversized)}") == '0'
    app.stop()
    app.start()
    cold = Git.init(tmp_path / 'native-cold.git', bare=True, format=format)
    authorize_git(cold, credential)
    cold.run('-c', 'protocol.version=2', 'fetch', remote, '+refs/*:refs/*')
    assert cold.text('rev-parse', 'refs/heads/trunk') == first
    assert cold.refs() == {'refs/heads/trunk': first, 'refs/heads/topic': first,
                           'refs/tags/annotated': client.text('rev-parse', 'annotated'), 'refs/tags/blob': blob}
    assert cold.run('show', first+':readme').stdout == b'hello\n'
    cold.run('fsck', '--full', '--strict')
    assert len(set(app.starts)) == 2
    assert_native_product_reads(app, pg, project, first, format)
    assert app.client.get(health_path, headers=headers).json()['data']['health'] == 'healthy'
    # A newly issued read credential can discover/fetch but cannot advertise a
    # receive service or mutate. Foreign locators do not retarget this grant.
    read_credential = 'pwg_' + secrets.token_urlsafe(32)
    app.api('POST', f'/projects/{project}/git-credentials', expected=201,
            headers={'Idempotency-Key': str(uuid.uuid4())}, json={
                'target': {'kind': 'project_root', 'project_id': project},
                'mode': 'r', 'credential': read_credential,
            })
    read_headers = {'Authorization': 'Basic '+base64.b64encode(('x:'+read_credential).encode()).decode()}
    assert app.client.get(f'/git/{project}.git/info/refs', params={'service': 'git-upload-pack'}, headers=read_headers).status_code == 200
    assert app.client.get(f'/git/{project}.git/info/refs', params={'service': 'git-receive-pack'}, headers=read_headers).status_code == 403
    assert app.client.get('/git/foreign.git/info/refs', params={'service': 'git-upload-pack'}, headers=headers).status_code == 401
    assert app.client.get(f'/git/{project}.git/info/refs', params={'service': 'git-upload-pack'}).status_code == 401
    assert pg.value(f"SELECT value FROM public.organization_usage_counters WHERE org_id={literal(org)} AND metric='storage.logical_bytes'") == '6'
    app.api('DELETE', f'/projects/{project}/git-credentials/{issued["id"]}')
    assert cold.run('ls-remote', remote, check=False).returncode != 0
    assert pg.value(f"SELECT target_oid FROM public.version_repository_refs WHERE project_id={literal(project)} AND name=decode({literal(b'refs/heads/trunk'.hex())},'hex')") == first

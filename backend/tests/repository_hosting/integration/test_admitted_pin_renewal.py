"""Current-authority renewal through the production admitted control (PG shim)."""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    AdmittedRefAuthorityRepository,
)
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.transaction import transaction, wait_for_lock
from tests.repository_hosting.integration.test_repository_write_admission import (
    admission as admission_fixture,
)
from tests.repository_hosting.integration.test_repository_write_admission import seed_credential

pytestmark = pytest.mark.hosting_live
admission = admission_fixture


class Client:
    def __init__(self, pg, application_name='pin-renewal'):
        self.pg, self.name = pg, application_name

    def rpc(self, name, args):
        def execute():
            value = self.pg.value(f'SET application_name={literal(self.name)}; SET ROLE service_role; SELECT to_jsonb(public.'
                                 + name+'('+','.join(key+'=>'+literal(value) for key, value in args.items())+'))')
            return SimpleNamespace(data=json.loads(value))
        return SimpleNamespace(execute=execute)


def control(a, name='pin-renewal'):
    lease = SimpleNamespace(project_id=a.project, is_active=True, lease_id=a.lease, holder_id=a.holder)
    return AdmittedRefAuthorityRepository(Client(a.pg, name), lease_provider=lambda _: lease)


def expiration(a, pin):
    return a.pg.value(f'SELECT expires_at FROM public.version_object_pins WHERE id={literal(pin)}')


def begin(c, a, actor, pin, purpose):
    if purpose == 'read':
        c.begin_read(a.project, actor, pin)
    else:
        c.begin(a.project, actor, pin, 1, {'a'*40: 'commit'})


@pytest.mark.parametrize('purpose', ['read', 'publication'])
def test_admitted_pin_renewal_cannot_extend_revoked_credential(admission, purpose):
    a = admission
    _, credential, actor = seed_credential(a)
    c, pin = control(a), str(uuid.uuid4())
    begin(c, a, actor, pin, purpose)
    assert c.renew(a.project, actor, pin)
    before = expiration(a, pin)
    a.pg.sql(f"UPDATE public.access_surface_credentials SET status='revoked' WHERE id={literal(credential)}")
    with pytest.raises(AssertionError, match='repository_action_denied'):
        c.renew(a.project, actor, pin)
    assert expiration(a, pin) == before


def test_admitted_pin_renewal_expired_publication_lease_preserves_pin(admission):
    a = admission
    c, pin = control(a), str(uuid.uuid4())
    begin(c, a, a.actor, pin, 'publication')
    before = expiration(a, pin)
    a.pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(a.lease)}")
    with pytest.raises(AssertionError, match='repository_write_lease_unavailable'):
        c.renew(a.project, a.actor, pin)
    assert expiration(a, pin) == before


def test_admitted_read_pin_renewal_does_not_require_write_lease(admission):
    a = admission
    c, pin = control(a), str(uuid.uuid4())
    begin(c, a, a.actor, pin, 'read')
    a.pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(a.lease)};"
             f"UPDATE public.project_members SET role='viewer' WHERE user_id={literal(a.user)}")
    c.lease_provider = lambda _: None
    assert c.renew(a.project, a.actor, pin)


@pytest.mark.parametrize('expires', ['credential', 'lease', 'pin'])
def test_admitted_pin_renewal_expiry_after_repository_wait_rolls_back(admission, expires):
    a = admission
    _, credential, actor = seed_credential(a)
    name, pin = 'pin-renewal-'+uuid.uuid4().hex, str(uuid.uuid4())
    c = control(a, name)
    begin(c, a, actor, pin, 'publication' if expires == 'lease' else 'read')
    table, identity, expected = {
        'credential': ('access_surface_credentials', credential, 'repository_action_denied'),
        'lease': ('project_write_leases', a.lease, 'repository_write_lease_unavailable'),
        'pin': ('version_object_pins', pin, 'publication_pin_unavailable'),
    }[expires]
    a.pg.sql(f"UPDATE public.{table} SET expires_at=clock_timestamp()+interval '2 seconds' WHERE id={literal(identity)}")
    before = expiration(a, pin)
    with ThreadPoolExecutor() as pool:
        with transaction(a.pg, f'SELECT 1 FROM public.version_repositories WHERE project_id={literal(a.project)} FOR UPDATE'):
            pending = pool.submit(c.renew, a.project, actor, pin)
            wait_for_lock(a.pg, name)
            time.sleep(2.1)
        with pytest.raises(AssertionError, match=expected):
            pending.result(timeout=10)
    assert expiration(a, pin) == before


def test_admitted_pin_renewal_acl_and_fixed_search_path(admission):
    a = admission
    signature = 'public.renew_admitted_version_object_pin(text,text,uuid)'
    for role in ('anon', 'authenticated', 'service_role'):
        assert a.pg.value(f'SELECT has_function_privilege({literal(role)},{literal(signature)},\'EXECUTE\')') == ('t' if role == 'service_role' else 'f')
    assert a.pg.value(f'SELECT proconfig::text FROM pg_proc WHERE oid={literal(signature)}::regprocedure') == '{"search_path=pg_catalog, public, pg_temp"}'

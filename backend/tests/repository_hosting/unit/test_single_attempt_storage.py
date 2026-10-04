"""Native mutation capability/configuration, not remote quiescence proof."""
from types import SimpleNamespace

import pytest
from botocore.credentials import Credentials

from src.infra.s3.exceptions import S3FileNotFoundError, S3OperationError
from src.infra.s3.service import S3Service
from src.version_engine.domain.errors import StorageWriteError
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.storage.mutation_context import collection_storage, publication_storage

pytestmark = pytest.mark.hosting_component


def test_single_attempt_view_does_not_mutate_shared_sdk_configuration(monkeypatch):
    clients = []

    def client(*_args, **kwargs):
        result = SimpleNamespace(meta=SimpleNamespace(config=kwargs['config'],
            endpoint_url=kwargs.get('endpoint_url') or 'http://owned.invalid',
            region_name=kwargs.get('region_name') or 'us-east-1'), close=lambda: None,
            _request_signer=SimpleNamespace(_credentials=Credentials('owned-access', 'owned-secret', 'owned-session')))
        clients.append(result)
        return result

    monkeypatch.setattr('src.infra.s3.service.boto3.client', client)
    service = S3Service()
    original = dict(service.client.meta.config.retries)
    strict = service.for_single_attempt_io()
    assert strict is not service and strict.client is not service.client
    assert strict.client.meta.config.retries == {'total_max_attempts': 1, 'mode': 'standard'}
    assert strict.client.meta.config.connect_timeout == service.client.meta.config.connect_timeout
    assert strict.client.meta.config.read_timeout == service.client.meta.config.read_timeout
    assert strict.client.meta.config.proxies == service.client.meta.config.proxies
    assert strict.multipart_threshold == strict.max_file_size <= 5 * 1024**3
    assert strict._single_attempt_io and not service._single_attempt_io
    assert strict.client._request_signer._credentials is service.client._request_signer._credentials
    assert service.for_single_attempt_io().client is strict.client
    assert service.client.meta.config.retries == original and len(clients) == 2
    source = service.client
    replacement = client(config=source.meta.config, endpoint_url='http://replacement.invalid')
    service.client = replacement
    rebound = service.for_single_attempt_io()
    assert rebound.client is not strict.client
    assert rebound.client.meta.endpoint_url == replacement.meta.endpoint_url
    service.client = source
    assert service.for_single_attempt_io().client is strict.client
    replacement.close()
    service.close()


@pytest.mark.asyncio
async def test_strict_delete_never_uses_ambiguous_head_as_absence():
    service = object.__new__(S3Service)
    service._single_attempt_io = True
    service.bucket_name = 'owned'
    calls = []

    def delete(**kwargs):
        calls.append(kwargs['Key'])
        return {'ResponseMetadata': {'RetryAttempts': 0}}

    async def absent(_key):
        assert not service._single_attempt_io, 'strict DELETE must not use HEAD'
        return False

    service.client = SimpleNamespace(delete_object=delete)
    service.file_exists = absent
    await service.delete_file('key')
    assert calls == ['key']
    service._single_attempt_io = False
    with pytest.raises(S3FileNotFoundError):
        await service.delete_file('key')
    assert calls == ['key']


@pytest.mark.parametrize('attempts', [None, True, 1, -1])
def test_missing_or_retried_mutation_receipt_is_uncertain(attempts):
    service = object.__new__(S3Service)
    service._single_attempt_io = True
    with pytest.raises(S3OperationError, match='uncertain retry history'):
        service._check_mutation_attempts({'ResponseMetadata': {'RetryAttempts': attempts}})
    service._check_mutation_attempts({'ResponseMetadata': {'RetryAttempts': 0}})


@pytest.mark.parametrize('context', [publication_storage('project', 'actor', 'pin'),
                                   collection_storage('project', 'token')])
def test_native_mutation_requires_explicit_single_attempt_capability(context):
    backend = S3StorageBackend(SimpleNamespace(), 'project')
    with context, pytest.raises(StorageWriteError, match='single-attempt storage capability unavailable'):
        backend._physical_s3()

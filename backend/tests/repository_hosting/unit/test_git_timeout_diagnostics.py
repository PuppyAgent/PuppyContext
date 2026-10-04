"""Timeout diagnostics preserve the original Git failure and resource budget."""
import subprocess

import pytest

from tests.repository_hosting.harness.git import Git

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize('broken_diagnostic', [False, True])
def test_git_deadline_captures_threads_without_masking_failure(tmp_path, monkeypatch, broken_diagnostic):
    error = subprocess.TimeoutExpired('synthetic git', 30)
    dumps = []

    def timeout(_command, **kwargs):
        assert kwargs['timeout'] == 30
        raise error

    def dump(**kwargs):
        assert kwargs['all_threads'] is True
        assert set(kwargs) == {'file', 'all_threads'}
        dumps.append(True)
        if broken_diagnostic:
            raise OSError('diagnostic stream unavailable')

    monkeypatch.setattr('tests.repository_hosting.harness.git.subprocess.run', timeout)
    monkeypatch.setattr('tests.repository_hosting.harness.git.faulthandler.dump_traceback', dump)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        Git(tmp_path).run('status')
    assert raised.value is error and dumps == [True]

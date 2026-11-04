import asyncio
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import pytest

from app.ai_data_governance import native_governance
from app.ai_data_governance.governance_source import governance_agent_source
from test_ai_dg_governance_source import binding


class ProviderError(Exception):
    def __init__(self, status):
        self.status = status


@pytest.fixture(params=['portable', 'assembled'])
def runtime(request, monkeypatch):
    if request.param == 'portable':
        return native_governance
    import sys
    module = ModuleType('governance_throttling_runtime')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(governance_agent_source(binding()), 'governance_runtime.py', 'exec'), module.__dict__)
    return module


def test_rejected_call_backs_off_and_returns_only_the_successful_response(runtime, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    call = AsyncMock(side_effect=[ProviderError(429), 'complete'])
    identity = Mock()
    retry = runtime.ConsultationModelRetry()
    assert asyncio.run(retry.invoke(call, identity, 'G3')) == 'complete'
    assert call.await_count == identity.call_count == retry.calls == 2
    assert sleep.await_count == 1 and 15 <= sleep.await_args.args[0] <= 18


@pytest.mark.parametrize('error', [ProviderError(401), ProviderError(403), ProviderError(500),
    TimeoutError('response unknown'), ValueError('429 text is not a provider rejection'), asyncio.CancelledError()])
def test_only_explicit_provider_throttling_is_retried(runtime, error, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    call = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        asyncio.run(runtime.ConsultationModelRetry().invoke(call, Mock(), 'G3'))
    assert call.await_count == 1
    sleep.assert_not_awaited()


def test_wrapped_native_throttling_and_local_context_are_recognized(runtime):
    native_type = type('AidpSystemException', (Exception,), {'__module__': 'aidputils.agents.exceptions'})
    assert runtime.is_consultation_throttling(native_type(
        'OCI GenAI: The model is rate-limited. Please retry shortly.'))
    wrapped = RuntimeError('provider details intentionally omitted')
    wrapped.__context__ = ProviderError(429)
    assert runtime.is_consultation_throttling(wrapped)
    wrapped.__context__ = ProviderError(403)
    assert not runtime.is_consultation_throttling(wrapped)


def test_per_phase_and_whole_invocation_limits_include_rejected_calls(runtime, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    retry = runtime.ConsultationModelRetry()
    call = AsyncMock(side_effect=ProviderError(429))
    with pytest.raises(ProviderError):
        asyncio.run(retry.invoke(call, Mock(), 'G3'))
    assert retry.calls == call.await_count == 3 and sleep.await_count == 2
    assert 30 <= sleep.await_args.args[0] <= 33
    success = AsyncMock(return_value='complete')
    for _ in range(6):
        assert asyncio.run(retry.invoke(success, Mock(), 'G6')) == 'complete'
    with pytest.raises(RuntimeError, match='call budget'):
        asyncio.run(retry.invoke(success, Mock(), 'G6'))
    assert retry.calls == 9 and success.await_count == 6


def test_identity_is_rechecked_after_the_backoff_before_another_call(runtime, monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    call = AsyncMock(side_effect=[ProviderError(429), 'must not be sent'])
    identity = Mock(side_effect=[None, PermissionError('revoked')])
    retry = runtime.ConsultationModelRetry()
    with pytest.raises(PermissionError):
        asyncio.run(retry.invoke(call, identity, 'G3'))
    assert retry.calls == call.await_count == 1

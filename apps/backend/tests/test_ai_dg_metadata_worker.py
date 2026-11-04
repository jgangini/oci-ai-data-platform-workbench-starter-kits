import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai_data_governance import central_sql, projection_query
from test_ai_dg_consultation import harness


@pytest.mark.parametrize('surface',['projection','catalog','single_confirmation','batch_confirmation'])
@pytest.mark.parametrize('worker_fails',[False,True])
def test_cancelled_metadata_reads_drain_the_actual_worker_before_the_invocation_closes_its_client(monkeypatch,surface,worker_fails):
    entered,release,finished=Event(),Event(),Event()
    closed=[]
    def worker(*args):
        entered.set()
        try:
            assert release.wait(3)
            if worker_fails: raise RuntimeError('Read ended with an SDK failure')
            return None
        finally: finished.set()
    config=dict(region='us-chicago-1')
    transport=SimpleNamespace(access_mode='CENTRAL_METADATA',signer=object(),check_identity=Mock())
    if surface=='projection':
        monkeypatch.setattr(projection_query,'run_central_projection',worker)
        invoke=lambda:projection_query.native_projection_query(config,'SELECT payload FROM controls LIMIT 2',limit=1,transport=transport)
    elif surface=='catalog':
        hooks,rows,_,caller,_=harness(); caller.side_effect=worker
        invoke=lambda:hooks._table_observation(rows[0])
    elif surface=='single_confirmation':
        monkeypatch.setattr(central_sql,'confirm_central_description',worker)
        invoke=lambda:central_sql.native_description_confirmation(config,'catalog.schema.orders','a'*64,transport=transport)
    else:
        monkeypatch.setattr(central_sql,'confirm_central_descriptions',worker)
        invoke=lambda:central_sql.native_descriptions_confirmation(config,[],transport=transport)
    async def run():
        async def invocation():
            try: await invoke()
            finally: closed.append(finished.is_set())
        task=asyncio.create_task(invocation())
        try:
            while not entered.is_set(): await asyncio.sleep(0)
            for _ in range(3):
                task.cancel()
                for _ in range(3): await asyncio.sleep(0)
            assert not task.done() and not closed
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError): await task
        assert closed==[True]
    asyncio.run(run())

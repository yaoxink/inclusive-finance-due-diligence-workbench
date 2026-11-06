"""S5-A07: three executions and a new service over the persisted synthetic root."""
import hashlib
import json
from types import SimpleNamespace

from test_farmer_model_v2 import env
from due_app.diligence import DiligenceService
from due_app.farmer_model_v2 import FarmerModelV2Service
from due_app.store import DueStore


def stable_business(value):
    runtime = {'id', 'created_at', 'business_hash', 'stale', 'stale_reasons', 'current_effective_limit'}
    return {key: item for key, item in value.items() if key not in runtime}


def independent_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def test_v2_three_identical_runs_and_persisted_service_replay(env):
    draft = env.service.save_inputs(env.cid, env.raw, 0)
    revision = draft['draft']['revision']
    runs = [env.service.run(env.cid, revision) for _ in range(3)]
    assert len({value['id'] for value in runs}) == 3
    expected = stable_business(runs[0])
    expected_business_hash = independent_hash(expected)
    expected_trace_hash = independent_hash(runs[0]['trace'])
    assert runs[0]['state'] == 'valid'
    assert (runs[0]['score'], runs[0]['rating'], runs[0]['effective_limit']) == ('75.0000', 'AA', '306600')
    for value in runs:
        assert stable_business(value) == expected
        assert value['business_hash'] == expected_business_hash
        assert independent_hash(value['trace']) == expected_trace_hash
    # No new calculation or writes: reconstruct every domain object from the
    # same persisted, exclusively synthetic root and read each original ID.
    reopened_store = DueStore(env.store.root)
    reopened_diligence = DiligenceService(reopened_store, SimpleNamespace(get=lambda *args: None))
    reopened = FarmerModelV2Service(reopened_store, reopened_diligence)
    for previous in runs:
        replay = reopened.run_detail(env.cid, previous['id'])
        assert replay['id'] == previous['id'] and replay['created_at'] == previous['created_at']
        assert not replay['stale']
        assert replay['input_snapshot'] == env.raw
        assert stable_business(replay) == expected
        assert replay['business_hash'] == expected_business_hash
        assert independent_hash(replay['trace']) == expected_trace_hash

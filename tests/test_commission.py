import csv, io, json

import pytest

from app.errors import DomainError
from app.modules.m5_calculate.commission import decide
from app.modules.m7_archive.export_out import bundle
from app.workflow import load, run

HEADER = 'id,policy_id,version,kind,effective,start,end,annual_premium,paid,ref,cover,note'
# 责任降低：实收3650，重算应收2730，退费920
REFUND_CSV = ('\n'.join([HEADER,
    'A01,PA,1,issue,2026-01-01,2026-01-01,2027-01-01,3650,3650,,基础险,保单原件',
    'A02,PA,2,change,2026-07-01,2026-01-01,2027-01-01,1825,0,,基础险,责任降低']) + '\n').encode()
# 追溯加保已收费后被撤销：实收7300，重算应收3650，退费3650
CANCEL_CSV = ('\n'.join([HEADER,
    'B01,PB,1,issue,2026-01-01,2026-01-01,2027-01-01,3650,3650,,基础险,保单原件',
    'B02,PB,2,change,2026-07-01,2026-01-01,2027-01-01,7300,3650,,基础险,追溯加保',
    'B03,PB,3,cancel,2026-08-01,2026-01-01,2027-01-01,0,0,B02,基础险,撤销批单']) + '\n').encode()


def commission_of(state, pid):
    return next(c['commission'] for c in state['calculations'] if c['id'] == pid)


def test_cross_month_offsets_next_period():
    # 跨两个结算月：原结算月2026-01已关账，应追佣金下期抵扣
    c = commission_of(run(REFUND_CSV, {'commission_rate': '0.10'}), 'PA')
    assert c['recover'] == '92.00' and c['method'] == 'OFFSET'
    assert c['offset_period'] == '2026-07' and c['original_period'] == '2026-01'
    assert c['settlement_period'] == '2026-07' and c['basis'] == ['A02']


def test_same_month_reverses_original_statement():
    # 原结算期间未关账：回冲原结算单，抵扣期间即原结算期间
    c = commission_of(run(REFUND_CSV, {'settlement_period': '2026-01'}), 'PA')
    assert c['method'] == 'REVERSE' and c['offset_period'] == '2026-01'


def test_closed_period_rolls_offset_forward():
    # 本期也已关账：抵扣期间顺延至关账截止月次月
    c = commission_of(run(REFUND_CSV, {'settlement_period': '2026-01', 'closed_through': '2026-01'}), 'PA')
    assert c['method'] == 'OFFSET' and c['offset_period'] == '2026-02'


def test_decide_rule():
    assert decide('2026-01', '2026-01', '') == ('REVERSE', '2026-01')
    assert decide('2026-01', '2026-11', '') == ('OFFSET', '2026-11')
    assert decide('2026-01', '2026-01', '2026-01') == ('OFFSET', '2026-02')


def test_cancel_endorsement_is_basis():
    # 撤销批单驱动的更正：原批改依据为撤销批单号
    c = commission_of(run(CANCEL_CSV, {'commission_rate': '0.10'}), 'PB')
    assert c['recover'] == '365.00' and c['method'] == 'OFFSET'
    assert c['offset_period'] == '2026-08' and c['basis'] == ['B03']


def test_default_rate_applied():
    state = run(REFUND_CSV, {})
    assert state['business']['commission_rate'] == '0.10'
    assert commission_of(state, 'PA')['recover'] == '92.00'


def test_zero_rate_no_recovery():
    c = commission_of(run(REFUND_CSV, {'commission_rate': '0'}), 'PA')
    assert c['method'] == 'NONE' and c['recover'] == '0.00'


def test_no_refund_no_recovery():
    state = run(load('clean.csv'), {})
    assert all(c['commission']['method'] == 'NONE' for c in state['calculations'])
    assert state['business']['commission_recover'] == '0.00'


def test_method_choice_moves_expense_period():
    # 同一保单跨两个结算月：回冲落在原期间，抵扣落在本期，费用归属不同
    offset = commission_of(run(REFUND_CSV, {}), 'PA')
    reverse = commission_of(run(REFUND_CSV, {'settlement_period': '2026-01'}), 'PA')
    assert offset['offset_period'] != reverse['offset_period']
    b1, b2 = run(REFUND_CSV, {})['business'], run(REFUND_CSV, {'settlement_period': '2026-01'})['business']
    assert (b1['commission_offset'], b1['commission_reverse']) == ('92.00', '0.00')
    assert (b2['commission_offset'], b2['commission_reverse']) == ('0.00', '92.00')
    assert b1['commission_recover'] == b2['commission_recover'] == '92.00'


@pytest.mark.parametrize('params', [
    {'commission_rate': '2'}, {'commission_rate': '-0.1'}, {'commission_rate': 'abc'},
    {'settlement_period': '2026-1'}, {'settlement_period': '2026/01'},
    {'closed_through': '2026-13'}, {'original_period': '0000-00'}])
def test_invalid_parameters_rejected(params):
    with pytest.raises(DomainError) as e:
        run(REFUND_CSV, params)
    assert e.value.code == 'PARAMETERS'


def test_recalculation_is_deterministic():
    a, b = run(REFUND_CSV, {'commission_rate': '0.10'}), run(REFUND_CSV, {'commission_rate': '0.10'})
    assert a['run_id'] == b['run_id'] and a['calculations'] == b['calculations']


def test_no_duplicate_recovery(client):
    payload = {'csv': REFUND_CSV.decode(), 'parameters': {'commission_rate': '0.10'}}
    a = client.post('/api/runs', json=payload).get_json()
    b = client.post('/api/runs', json=payload).get_json()
    assert a['run_id'] == b['run_id']
    runs = client.get('/api/runs').get_json()
    assert len([r for r in runs if r['id'] == a['run_id']]) == 1
    entries = [c for c in a['calculations'] if c['commission']['method'] != 'NONE']
    assert len(entries) == len({c['id'] for c in entries}) == 1


def test_failed_switch_keeps_stored_version(client):
    good = client.post('/api/runs', json={'csv': REFUND_CSV.decode()}).get_json()
    before = [r['id'] for r in client.get('/api/runs').get_json()]
    bad = client.post('/api/runs', json={'csv': REFUND_CSV.decode(), 'parameters': {'commission_rate': '9'}})
    assert bad.status_code == 422 and bad.get_json()['code'] == 'PARAMETERS'
    assert [r['id'] for r in client.get('/api/runs').get_json()] == before
    stored = client.get('/api/runs/' + good['run_id']).get_json()
    assert stored['calculations'] == good['calculations']
    assert stored['decision'] == good['decision'] and stored['review_version'] == good['review_version']


def test_review_survives_resubmit(client):
    payload = {'csv': REFUND_CSV.decode()}
    good = client.post('/api/runs', json=payload).get_json()
    client.post('/api/runs/' + good['run_id'] + '/review', json={'version': 1, 'decision': '通过'})
    client.post('/api/runs', json=payload)
    stored = client.get('/api/runs/' + good['run_id']).get_json()
    assert stored['decision'] == '通过' and stored['review_version'] == 2


def test_archive_export_keeps_recovery_chain():
    state = run(REFUND_CSV, {'commission_rate': '0.10'})
    assert state['archive']['recovery_count'] == 1
    assert state['archive']['evidence_count'] == len(state['calculations']) + len(state['quarantine']) + 1
    rows = list(csv.reader(io.StringIO(bundle(state).lstrip('﻿'))))
    recovery = [r for r in rows if r[0] == '佣金追偿']
    assert len(recovery) == 1 and recovery[0][3] == 'PA' and recovery[0][4] == 'OFFSET'
    content = json.loads(recovery[0][5])
    assert content['recover'] == '92.00' and content['offset_period'] == '2026-07'
    assert content['basis'] == ['A02'] and content['original_period'] == '2026-01'
    assert state['reconcile']['business']['commission_recover'] == '92.00'


def test_export_download_includes_recovery(client):
    posted = client.post('/api/runs', json={'csv': REFUND_CSV.decode()}).get_json()
    body = client.get('/api/archive/' + posted['run_id'] + '/export.csv').data.decode('utf-8-sig')
    assert '佣金追偿' in body and '92.00' in body


def test_pages_render_recovery(client):
    client.post('/api/runs', json={'csv': REFUND_CSV.decode()})
    assert '应追佣金' in client.get('/m5_calculate').data.decode()
    assert '下期抵扣' in client.get('/m6_reconcile').data.decode()
    assert '佣金追偿' in client.get('/m7_archive').data.decode()

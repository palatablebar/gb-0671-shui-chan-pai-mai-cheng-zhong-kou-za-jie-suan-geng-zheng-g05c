"""M8 跨日承接测试：封账不可变、承接差额可追溯、阻断规则、幂等与发布回滚。"""
import pytest
from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from app.modules.carryover.rules import build_carryover

HEADER = "id,ticket,kind,weight,unit,fish,price,rate,version,replaces,paid"


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)


def _run(c, csv_text):
    return c.post('/api/runs', json={'csv_text': csv_text}).json()


def _seal(c, run_id):
    return c.post(f'/api/runs/{run_id}/seal').json()


# ---- 规则层：票级差额与闭合 ----
def test_happy_path_carries_delta_only():
    base = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0"""})
    current = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
v2g,00001,gross,120,kg,黄鱼,10.00,0.00,2,1,900.00
v2t,00001,tare,10,kg,黄鱼,10.00,0.00,2,1,0"""})
    draft = build_carryover(base, current)
    assert draft['closed'] is True
    assert draft['status'] == '可承接'
    row = draft['rows'][0]
    assert row['delta_amount'] == '200.00'
    assert row['delta_paid'] == '900.00'
    assert row['delta_due'] == '-700.00'  # 待付下降即应收回款
    assert row['base_version'] == 1 and row['current_version'] == 2


def test_missing_base_kept_pending():
    base = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0"""})
    # v2 未声明 replaces，更正裁决将整票打入 version_conflicts，次日无生效版。
    current = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
v2g,00001,gross,120,kg,黄鱼,10.00,0.00,2,,0
v2t,00001,tare,10,kg,黄鱼,10.00,0.00,2,,0"""})
    draft = build_carryover(base, current)
    assert draft['status'] == '待处理' and draft['closed'] is False
    assert draft['blocked'][0]['code'] in {'MISSING_BASE', 'STALE_CORRECTION'}


def test_stale_correction_kept_pending():
    base = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
v2g,00001,gross,110,kg,黄鱼,10.00,0.00,2,1,0
v2t,00001,tare,10,kg,黄鱼,10.00,0.00,2,1,0"""})
    # 封账已到 v2，次日却来了替代 v1 的 v3（陈旧分支）。
    current = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
v2g,00001,gross,110,kg,黄鱼,10.00,0.00,2,1,0
v2t,00001,tare,10,kg,黄鱼,10.00,0.00,2,1,0
v3g,00001,gross,120,kg,黄鱼,10.00,0.00,3,1,0
v3t,00001,tare,10,kg,黄鱼,10.00,0.00,3,1,0"""})
    draft = build_carryover(base, current)
    assert draft['closed'] is False
    assert any(r['code'] == 'STALE_CORRECTION' for r in draft['blocked'])


def test_duplicate_correction_kept_pending():
    base = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
v2g,00001,gross,110,kg,黄鱼,10.00,0.00,2,1,0
v2t,00001,tare,10,kg,黄鱼,10.00,0.00,2,1,0"""})
    current = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
v2g,00001,gross,110,kg,黄鱼,10.00,0.00,2,1,0
v2t,00001,tare,10,kg,黄鱼,10.00,0.00,2,1,0"""})
    # 模拟同一更正再次推送：同一 (票,版本,替代目标) 审计计数超过两份批次的正常出现次数。
    current['version_audit'].append(dict(current['version_audit'][-1]))
    draft = build_carryover(base, current)
    assert draft['status'] == '待处理'
    assert draft['blocked'][0]['code'] == 'DUPLICATE_CORRECTION'


def test_new_ticket_excluded_from_delta():
    base = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0"""})
    current = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
n1g,00009,gross,30,kg,带鱼,10.00,0.00,1,,0
n1t,00009,tare,3,kg,带鱼,10.00,0.00,1,,0"""})
    draft = build_carryover(base, current)
    assert draft['closed'] is True
    assert draft['new_tickets'] == ['00009']
    assert draft['delta_amount'] == '0.00'  # 新票不进承接差额


def test_batch_unbalanced_forces_pending(monkeypatch):
    base = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0"""})
    current = evaluate({'csv_text': HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
v2g,00001,gross,120,kg,黄鱼,10.00,0.00,2,1,0
v2t,00001,tare,10,kg,黄鱼,10.00,0.00,2,1,0"""})
    # 人为破坏票级闭合：直接篡改次日应付，使 delta_amount != delta_paid + delta_due。
    current['final'][0]['amount'] = '9999.00'
    draft = build_carryover(base, current)
    assert draft['closed'] is False
    assert draft['status'] == '待处理'
    assert all(r['status'] == '待处理' for r in draft['blocked'])


# ---- API 与持久化 ----
BASE_CSV = HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
b2g,00002,gross,50,kg,带鱼,20.00,0.10,1,,0
b2t,00002,tare,5,kg,带鱼,20.00,0.10,1,,0"""
NEXT_CSV = HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
v2g,00001,gross,120,kg,黄鱼,10.00,0.00,2,1,900.00
v2t,00001,tare,10,kg,黄鱼,10.00,0.00,2,1,0
b2g,00002,gross,50,kg,带鱼,20.00,0.10,1,,0
b2t,00002,tare,5,kg,带鱼,20.00,0.10,1,,0"""


def test_seal_is_immutable_and_idempotent(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = _run(c, BASE_CSV)
    rid = run['manifest']['run_id']
    first = _seal(c, rid)
    second = _seal(c, rid)
    assert first == second
    seals = c.get('/api/seals').json()['seals']
    assert len(seals) == 1 and seals[0]['run_id'] == rid
    # 封账快照独立保存，runs 表复核版本变化不影响封存码。
    c.post(f'/api/runs/{rid}/review', json={'revision': 1})
    assert _seal(c, rid)['seal'] == first['seal']


def test_preview_does_not_persist(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = _run(c, BASE_CSV); _seal(c, base['manifest']['run_id'])
    nxt = _run(c, NEXT_CSV)
    resp = c.post('/api/carryover/preview', json={
        'base_run_id': base['manifest']['run_id'], 'current_run_id': nxt['manifest']['run_id']})
    assert resp.status_code == 200
    draft = resp.json()['draft']
    assert draft['closed'] is True and draft['carried_count'] == 1
    assert c.get('/api/carryover').json()['records'] == []


def test_full_carryover_flow_and_page(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = _run(c, BASE_CSV); brid = base['manifest']['run_id']; _seal(c, brid)
    nxt = _run(c, NEXT_CSV); nrid = nxt['manifest']['run_id']
    saved = c.post('/api/carryover', json={'base_run_id': brid, 'current_run_id': nrid}).json()
    assert saved['record']['published'] == 0
    published = c.post('/api/carryover/publish', json={'base_run_id': brid, 'current_run_id': nrid}).json()
    assert published['record']['published'] == 1
    assert published['record']['status'] == '已发布'
    # 页面可渲染封账金额、承接差额、更正版本所需的核对区。
    page = c.get(f'/carryover?run_id={nrid}').text
    assert '跨日承接核对' in page and 'carry-table' in page
    # 记录可按批号取回。
    got = c.get(f'/api/carryover/{brid}/{nrid}').json()
    assert got['draft']['delta_due'] == '-700.00'


def test_rerun_does_not_add_carryover_record(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = _run(c, BASE_CSV); brid = base['manifest']['run_id']; _seal(c, brid)
    nxt = _run(c, NEXT_CSV); nrid = nxt['manifest']['run_id']
    c.post('/api/carryover', json={'base_run_id': brid, 'current_run_id': nrid})
    c.post('/api/carryover/publish', json={'base_run_id': brid, 'current_run_id': nrid})
    # 次日重跑同一输入：同 run_id，再保存承接不得新增/改写。
    again = c.post('/api/carryover', json={'base_run_id': brid, 'current_run_id': nrid}).json()
    assert again['changed'] is False
    assert again['record']['published'] == 1
    assert len(c.get('/api/carryover').json()['records']) == 1


def test_publish_blocked_draft_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = _run(c, BASE_CSV); brid = base['manifest']['run_id']; _seal(c, brid)
    bad_csv = HEADER + """
b1g,00001,gross,100,kg,黄鱼,10.00,0.00,1,,0
b1t,00001,tare,10,kg,黄鱼,10.00,0.00,1,,0
v2g,00001,gross,120,kg,黄鱼,10.00,0.00,2,,0
v2t,00001,tare,10,kg,黄鱼,10.00,0.00,2,,0
b2g,00002,gross,50,kg,带鱼,20.00,0.10,1,,0
b2t,00002,tare,5,kg,带鱼,20.00,0.10,1,,0"""
    nxt = _run(c, bad_csv); nrid = nxt['manifest']['run_id']
    saved = c.post('/api/carryover', json={'base_run_id': brid, 'current_run_id': nrid}).json()
    assert saved['record']['status'] == '待处理'
    resp = c.post('/api/carryover/publish', json={'base_run_id': brid, 'current_run_id': nrid})
    assert resp.status_code == 422
    assert resp.json()['code'] == 'CARRYOVER_BLOCKED'
    # 待处理记录仍然保留，可在修复后重新保存。
    assert c.get(f'/api/carryover/{brid}/{nrid}').json()['published'] == 0


def test_publish_failure_rolls_back_and_keeps_seal(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = _run(c, BASE_CSV); brid = base['manifest']['run_id']
    seal_before = _seal(c, brid)
    nxt = _run(c, NEXT_CSV); nrid = nxt['manifest']['run_id']
    c.post('/api/carryover', json={'base_run_id': brid, 'current_run_id': nrid})
    resp = c.post('/api/carryover/publish',
                  json={'base_run_id': brid, 'current_run_id': nrid, 'fail_point': 'before_commit'})
    assert resp.status_code == 500
    record = c.get(f'/api/carryover/{brid}/{nrid}').json()
    assert record['published'] == 0 and record['status'] == '可承接'
    # 旧版封账与原证据保持不变。
    assert c.post(f'/api/runs/{brid}/seal').json()['seal'] == seal_before['seal']
    # 回滚后仍可正常发布。
    ok = c.post('/api/carryover/publish', json={'base_run_id': brid, 'current_run_id': nrid}).json()
    assert ok['record']['published'] == 1


def test_carryover_requires_sealed_base(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = _run(c, BASE_CSV)
    nxt = _run(c, NEXT_CSV)
    resp = c.post('/api/carryover/preview',
                  json={'base_run_id': base['manifest']['run_id'], 'current_run_id': nxt['manifest']['run_id']})
    assert resp.status_code == 404 and resp.json()['code'] == 'SEAL_NOT_FOUND'


def test_same_run_id_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = _run(c, BASE_CSV); rid = run['manifest']['run_id']; _seal(c, rid)
    resp = c.post('/api/carryover/preview', json={'base_run_id': rid, 'current_run_id': rid})
    assert resp.status_code == 422


def test_export_zip_contains_carryover_evidence(tmp_path, monkeypatch):
    import zipfile, io
    c = client(tmp_path, monkeypatch)
    base = _run(c, BASE_CSV); brid = base['manifest']['run_id']; _seal(c, brid)
    nxt = _run(c, NEXT_CSV); nrid = nxt['manifest']['run_id']
    c.post('/api/carryover', json={'base_run_id': brid, 'current_run_id': nrid})
    c.post('/api/carryover/publish', json={'base_run_id': brid, 'current_run_id': nrid})
    bundle = zipfile.ZipFile(io.BytesIO(c.get(f'/api/export/{brid}').content))
    assert 'carryover.json' in bundle.namelist()
    carried = bundle.read('carryover.json').decode()
    assert nrid in carried and '已发布' in carried

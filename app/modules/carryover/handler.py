from fastapi import APIRouter, Request, Body
from app.web import render
from app import store
from app.errors import BusinessError
from app.modules.carryover.rules import build_carryover

router=APIRouter()


def _require_ids(payload):
    base_run_id=payload.get('base_run_id')
    current_run_id=payload.get('current_run_id')
    if not isinstance(base_run_id,str) or not base_run_id:
        raise BusinessError('BAD_CONTEXT','缺少封账批号 base_run_id')
    if not isinstance(current_run_id,str) or not current_run_id:
        raise BusinessError('BAD_CONTEXT','缺少次日批号 current_run_id')
    if base_run_id==current_run_id:
        raise BusinessError('BAD_CONTEXT','封账批号与次日批号不能相同')
    return base_run_id, current_run_id


@router.get('/carryover')
def page(request:Request):
    return render(request,'carryover')


@router.post('/api/carryover/preview')
def preview(payload:dict=Body(...)):
    """纯计算：核对封账金额、承接差额与更正版本，不写承接结果。"""
    from app.main import DB_PATH
    base_run_id, current_run_id=_require_ids(payload)
    seal=store.get_seal(DB_PATH,base_run_id)
    current_state=store.get(DB_PATH,current_run_id)
    draft=build_carryover(seal['snapshot'],current_state)
    return {'draft':draft,'sealed_at':seal['sealed_at']}


@router.post('/api/carryover')
def save_draft(payload:dict=Body(default={})):
    """生成或刷新待处理/可承接草稿。同一对批号重跑不新增记录，已发布记录不被覆盖。

    无批号入参时返回承接索引，与其他模块的空参重跑行为保持一致。
    """
    from app.main import DB_PATH
    if not payload or not payload.get('base_run_id') or not payload.get('current_run_id'):
        return {'records':store.list_carryovers(DB_PATH),'seals':store.list_seals(DB_PATH)}
    base_run_id, current_run_id=_require_ids(payload)
    seal=store.get_seal(DB_PATH,base_run_id)
    current_state=store.get(DB_PATH,current_run_id)
    draft=build_carryover(seal['snapshot'],current_state)
    record, changed=store.upsert_carryover(DB_PATH,draft)
    return {'record':record,'changed':changed,'sealed_at':seal['sealed_at']}


@router.post('/api/carryover/publish')
def publish(payload:dict=Body(...)):
    """发布承接结果：仅闭合草稿可发布；失败回滚并保留旧版封账与原证据。"""
    from app.main import DB_PATH
    base_run_id, current_run_id=_require_ids(payload)
    fail_point=payload.get('fail_point')
    record, changed=store.publish_carryover(DB_PATH,base_run_id,current_run_id,fail_point=fail_point)
    return {'record':record,'changed':changed}


@router.get('/api/carryover')
def list_records():
    from app.main import DB_PATH
    return {'records':store.list_carryovers(DB_PATH),'seals':store.list_seals(DB_PATH)}


@router.get('/api/carryover/{base_run_id}/{current_run_id}')
def get_record(base_run_id:str,current_run_id:str):
    from app.main import DB_PATH
    return store.get_carryover(DB_PATH,base_run_id,current_run_id)

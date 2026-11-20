"""跨日承接规则：在不重开已封账批次的前提下，用承接差额承接迟到更正。

设计要点：
- 封账批次与次日批次各自独立封存，旧封账和原始证据不可变；
- 逐票比较封账生效版与次日生效版，形成票级承接差额；
- 缺少前版/陈旧更正、同一更正重复到达、差额无法闭合时只进待处理，不进承接结果；
- 同一对（封账批号 × 次日批号）无论重跑多少次，只生成同一条承接记录。
"""
from collections import Counter
from decimal import Decimal
from app.coerce import money
from app.reasons import REASONS


def _ticket_index(rows):
    return {row['ticket']: row for row in rows}


def _decimal(value):
    return Decimal(str(value))


def _detect_duplicate_corrections(base_state, current_state):
    """同一更正（票+版本+替代目标）在封账与次日证据中累计出现超过两次即重复到达。

    同一份次日输入重跑会产生完全相同的两份审计，各出现一次，属于正常承接；
    真正的重复到达（同票同版同替代目标再次推送）会使计数超过两次。
    """
    seen = Counter()
    for state in (base_state, current_state):
        for audit in state.get('version_audit', []):
            seen[(audit['ticket'], audit['version'], audit['replaces'])] += 1
    return {key for key, count in seen.items() if count > 2}


def _build_row(ticket, base_row, current_row, duplicate_keys, current_conflicts):
    base_version = base_row['version'] if base_row else None
    current_version = current_row['version'] if current_row else None
    row = {
        'ticket': ticket,
        'base_version': base_version,
        'current_version': current_version,
        'base_amount': base_row['amount'] if base_row else None,
        'current_amount': current_row['amount'] if current_row else None,
        'base_paid': base_row['paid'] if base_row else None,
        'current_paid': current_row['paid'] if current_row else None,
        'base_due': base_row['due'] if base_row else None,
        'current_due': current_row['due'] if current_row else None,
        'delta_amount': None,
        'delta_paid': None,
        'delta_due': None,
        'status': '待处理',
        'code': '',
        'detail': '',
        'lines': list(current_row.get('lines', [])) if current_row else [],
    }
    if current_row is None and base_row is not None:
        # 迟到更正未通过更正裁决（缺前版/陈旧分支），票从次日生效票中消失。
        conflict = next((c for c in current_conflicts if c.get('ticket') == ticket), None)
        row['code'] = conflict['code'] if conflict else 'MISSING_BASE'
        row['detail'] = REASONS.get(row['code'], REASONS['MISSING_BASE']) + '：次日无生效版本，保留待处理'
        return row
    if base_row is None:
        # 次日新票不属于迟到更正，不产生承接差额，单独计数。
        row['status'] = '新票'
        row['detail'] = '封账中无此票，按次日新票入账，不进承接差额'
        return row

    duplicate = any(key[0] == ticket and key[1] == current_version for key in duplicate_keys)
    if duplicate:
        row['code'] = 'DUPLICATE_CORRECTION'
        row['detail'] = REASONS['DUPLICATE_CORRECTION']
        return row
    replaces = str(current_row.get('replaces', '') or '').strip()
    if current_version == base_version:
        # 版本未推进：无迟到更正，不是承接对象。
        row['status'] = '无变更'
        row['delta_amount'] = money(Decimal(0))
        row['delta_paid'] = money(Decimal(0))
        row['delta_due'] = money(Decimal(0))
        row['detail'] = '版本未变化，承接差额为零'
        return row
    if not replaces:
        row['code'] = 'MISSING_BASE'
        row['detail'] = REASONS['MISSING_BASE'] + '：更正未声明替代版本'
        return row
    if replaces != str(base_version):
        row['code'] = 'STALE_CORRECTION'
        row['detail'] = REASONS['STALE_CORRECTION'] + f'：应替代封账版本{base_version}，实际替代{replaces}'
        return row

    delta_amount = _decimal(current_row['amount']) - _decimal(base_row['amount'])
    delta_paid = _decimal(current_row['paid']) - _decimal(base_row['paid'])
    delta_due = _decimal(current_row['due']) - _decimal(base_row['due'])
    # 票级闭合：应付差额必须等于已付差额与待付差额之和。
    if delta_amount != delta_paid + delta_due:
        row['code'] = 'UNBALANCED_DELTA'
        row['detail'] = REASONS['UNBALANCED_DELTA'] + '（票级）'
        return row

    row.update({
        'delta_amount': money(delta_amount),
        'delta_paid': money(delta_paid),
        'delta_due': money(delta_due),
        'status': '可承接',
        'detail': f'版本{base_version}→{current_version}',
    })
    return row


def build_carryover(base_state, current_state):
    """纯函数：由封账批次状态与次日批次状态构造承接草稿（不落库）。"""
    base_rows = _ticket_index(base_state['final'])
    current_rows = _ticket_index(current_state['final'])
    duplicate_keys = _detect_duplicate_corrections(base_state, current_state)

    rows = []
    for ticket in sorted(set(base_rows) | set(current_rows)):
        rows.append(_build_row(
            ticket,
            base_rows.get(ticket),
            current_rows.get(ticket),
            duplicate_keys,
            current_state.get('version_conflicts', []),
        ))

    carried = [r for r in rows if r['status'] == '可承接']
    blocked = [r for r in rows if r['status'] == '待处理']
    new_tickets = [r['ticket'] for r in rows if r['status'] == '新票']

    sum_delta_amount = sum((_decimal(r['delta_amount']) for r in carried), Decimal(0))
    sum_delta_paid = sum((_decimal(r['delta_paid']) for r in carried), Decimal(0))
    sum_delta_due = sum((_decimal(r['delta_due']) for r in carried), Decimal(0))

    # 批级闭合：承接票差额合计必须与两批生效票总额差一致（新票不计入承接）。
    base_total = sum((_decimal(r['amount']) for r in base_state['final']), Decimal(0))
    current_total = sum((_decimal(r['amount']) for r in current_state['final']), Decimal(0))
    new_total = sum((_decimal(current_rows[t]['amount']) for t in new_tickets), Decimal(0))
    batch_closed = (
        not blocked
        and sum_delta_amount == sum_delta_paid + sum_delta_due
        and sum_delta_amount == (current_total - new_total) - base_total
    )
    if not blocked and not batch_closed:
        # 票级各自闭合但批级对不上账：全部打回待处理，不允许部分承接。
        for row in carried:
            row['status'] = '待处理'
            row['code'] = 'UNBALANCED_DELTA'
            row['detail'] = REASONS['UNBALANCED_DELTA'] + '（批级合计）'
        blocked = [r for r in rows if r['status'] == '待处理']
        carried = []

    status = '可承接' if batch_closed else '待处理'
    return {
        'base_run_id': base_state['manifest']['run_id'],
        'current_run_id': current_state['manifest']['run_id'],
        'base_seal': base_state['archive']['seal'],
        'current_seal': current_state['archive']['seal'],
        'base_total': money(base_total),
        'current_total': money(current_total),
        'delta_amount': money(sum_delta_amount),
        'delta_paid': money(sum_delta_paid),
        'delta_due': money(sum_delta_due),
        'new_tickets': new_tickets,
        'new_total': money(new_total),
        'carried_count': len(carried),
        'blocked_count': len(blocked),
        'closed': batch_closed,
        'status': status,
        'rows': rows,
        'blocked': blocked,
    }


def apply(state):
    """模块入口：无封账上下文时给出空承接视图，真实草稿由 handler 装配。"""
    state['views']['carryover'] = {
        'title': '跨日承接',
        'rows': [],
        'metrics': {'承接票': 0, '待处理': 0, '状态': '待选择封账批次'},
    }

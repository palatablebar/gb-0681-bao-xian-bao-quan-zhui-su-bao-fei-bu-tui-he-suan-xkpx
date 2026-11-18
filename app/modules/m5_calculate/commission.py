from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.errors import DomainError

CENT = Decimal('.01')
DEFAULT_RATE = '0.10'


def _period(value, name):
    if value is None:
        return None
    text = str(value).strip()
    parts = text.split('-')
    if (len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2
            or not all(p.isdigit() for p in parts) or not 1 <= int(parts[1]) <= 12):
        raise DomainError('PARAMETERS', name + '须为YYYY-MM格式')
    return text


def _month_after(period):
    year, month = int(period[:4]), int(period[5:7]) + 1
    if month > 12:
        year, month = year + 1, 1
    return '%04d-%02d' % (year, month)


def settings(state):
    params = state.get('parameters') or {}
    raw = params.get('commission_rate', DEFAULT_RATE)
    try:
        rate = Decimal(str(raw).strip())
    except InvalidOperation:
        raise DomainError('PARAMETERS', '佣金率无效')
    if not rate.is_finite() or rate < 0 or rate > 1:
        raise DomainError('PARAMETERS', '佣金率须在0到1之间')
    return {'rate': rate,
            'settlement': _period(params.get('settlement_period'), '本期结算期间'),
            'original': _period(params.get('original_period'), '原结算期间'),
            'closed': _period(params.get('closed_through'), '关账截止期间') or ''}


def decide(original, settlement, closed):
    # 原结算期间未关账则回冲原结算单；已关账则下期抵扣。
    # 默认早于本期结算月即视为已关账；closed_through 可显式锁定。
    def locked(period):
        return period <= closed if closed else period < settlement
    if not locked(original):
        return 'REVERSE', original
    offset = settlement if not locked(settlement) else _month_after(closed)
    return 'OFFSET', offset


def entry(policy, difference, cfg):
    base = policy['base']
    original = cfg['original'] or base['start'][:7]
    months = [c['effective'][:7] for c in policy['changes']]
    months += [c['effective'][:7] for c in policy.get('cancels', [])]
    settlement = cfg['settlement'] or max([base['start'][:7]] + months)
    basis = [c['id'] for c in policy['changes']] + [c['id'] for c in policy.get('cancels', [])]
    result = {'rate': str(cfg['rate']), 'original_period': original,
              'settlement_period': settlement, 'basis': basis,
              'recover': '0.00', 'method': 'NONE', 'offset_period': ''}
    gap = -Decimal(str(difference))  # 实收减应收，正数为多收
    if gap <= 0:
        return result
    amount = (gap * cfg['rate']).quantize(CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        return result
    method, offset_period = decide(original, settlement, cfg['closed'])
    result.update(recover=str(amount), method=method, offset_period=offset_period)
    return result


def summarize(calculations):
    def total(method):
        return sum((Decimal(c['commission']['recover']) for c in calculations
                    if c['commission']['method'] == method), Decimal(0))
    reverse, offset = total('REVERSE'), total('OFFSET')
    count = sum(1 for c in calculations if c['commission']['method'] != 'NONE')
    return {'commission_recover': str((reverse + offset).quantize(CENT)),
            'commission_reverse': str(reverse.quantize(CENT)),
            'commission_offset': str(offset.quantize(CENT)),
            'commission_count': count}

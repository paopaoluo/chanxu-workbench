"""Deterministic mainland exchange clock; independent of Windows tzdata.

Latest completed daily session is today's date only at/after 15:00 Beijing
on an exchange trading day. A clock decision does not assert vendor readiness.
"""
from datetime import date, datetime, time, timedelta, timezone

CN = timezone(timedelta(hours=8))
CALENDAR_SOURCE = 'https://www.szse.cn/disclosure/notice/general/t20251222_618087.html'
CALENDAR_VERIFIED_ON = '2026-11-05'
CLOSURES = {
    2026: [('01-01','01-03'),('02-15','02-23'),('04-04','04-06'),
           ('05-01','05-05'),('06-19','06-21'),('09-25','09-27'),('10-01','10-07')]
}


class CalendarError(ValueError):
    """No guessing when the exchange calendar has not been verified."""


def now_cn():
    return datetime.now(CN)


def calendar_warnings(now=None, notice_days=90):
    """Operational notices only; never add or infer unverified trading days."""
    now = now or now_cn()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('日历预警时间必须包含时区')
    local = now.astimezone(CN)
    warnings = []
    try:
        # Also checks the previous year when the latest session falls there.
        market_status(local)
    except CalendarError as exc:
        warnings.append(str(exc))
    next_year = local.year + 1
    days_left = (date(next_year, 1, 1) - local.date()).days
    if 0 < days_left <= notice_days and next_year not in CLOSURES:
        warnings.append(
            f'距{next_year}年还有{days_left}天，尚未核验{next_year}年交易所休市日历；'
            '请在跨年前依据交易所官方公告更新并测试，禁止按普通工作日自动补齐'
        )
    return warnings


def is_trading_day(value):
    if isinstance(value, str):
        value = date.fromisoformat(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('交易日时间必须包含时区')
        value = value.astimezone(CN).date()
    if value.year not in CLOSURES:
        raise CalendarError(f'尚未核验{value.year}年交易所休市日历，禁止按普通工作日猜测；请更新官方日历')
    return value.weekday() < 5 and not any(a <= value.strftime('%m-%d') <= b for a,b in CLOSURES[value.year])


def market_status(now=None):
    now = now or now_cn()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('实时时间判定需要包含时区的datetime')
    now = now.astimezone(CN)
    today = now.date()
    trading = is_trading_day(today)
    local_time = now.time().replace(tzinfo=None)
    if not trading:
        phase = 'non_trading_day'
    elif local_time < time(9,30):
        phase = 'preopen'
    elif local_time < time(11,30):
        phase = 'morning'
    elif local_time < time(13):
        phase = 'lunch'
    elif local_time < time(15):
        phase = 'afternoon'
    else:
        phase = 'closed'
    target = today if trading and local_time >= time(15) else today-timedelta(days=1)
    for _ in range(40):
        if is_trading_day(target):
            break
        target -= timedelta(days=1)
    else:
        raise CalendarError('无法确定最近已收盘交易日')
    return {'serverTime':now.isoformat(), 'timezone':'Asia/Shanghai',
            'isTradingDay':trading, 'isTradingTime':phase in ('morning','afternoon'),
            'phase':phase, 'targetDate':target.isoformat(), 'marketCloseTime':'15:00',
            'policy':'cn_close_v1', 'calendarSource':CALENDAR_SOURCE,
            'calendarVerifiedOn':CALENDAR_VERIFIED_ON}

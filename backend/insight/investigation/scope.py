"""Bounded date requests, resolved in the shop timezone and frozen with the run."""
import calendar
import re
from datetime import date, timedelta

VERSION = 'investigation-date-scope-v2'
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
DAY = r'(?<!\d)(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?!\d)'
MONTH = r'(?<!\d)(20\d{2})[-/年](\d{1,2})(?:月)?(?![-/\d])'


NUMBER = r'(?:\d+|[零〇一二两三四五六七八九十百千万几数半]+|one|two|three|four|five|six|seven|eight|nine|ten|a|an|few|several|couple(?:\s+of)?|dozen)'
UNIT = r'(?:个?工作日|工作天|天|日|个?周|个?星期|个?月|个?年|个?季度|(?:business|working)\s+days?|days?|weeks?|fortnights?|months?|years?|quarters?|weekends?)'
WEEKDAY = r'(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)'


def request_date_text(query):
    """Mask explicitly attributed source quotations and named identifiers only.

    Source records themselves never enter this parser. Quoted user date requests
    remain date requests unless the user explicitly attributes a source quote.
    The original query is retained separately for content retrieval.
    """
    text = query.lower()
    quote = r"“[^”]*”|「[^」]*」|\"[^\"]*\"|‘[^’]*’|(?<!\w)'[^'\n]+'(?!\w)"
    for match in list(re.finditer(quote, text)):
        before = re.split(r'[，,；;。]', text[:match.start()])[-1]
        if re.search(r'原文|引用|评论(?:写|说|称)|客户(?:写|说)|标题|\b(?:quote|review says|customer wrote|text says|title)\b', before):
            text = text[:match.start()] + ' ' * (match.end() - match.start()) + text[match.end():]
    for match in list(re.finditer(r'(?:订单号|订单id|\border\s+(?:id|number)|\bsku|\brecord[_ ]id)\s*[:：=#]?\s*[a-z0-9][a-z0-9._/-]*', text)):
        text = text[:match.start()] + ' ' * (match.end() - match.start()) + text[match.end():]
    return text


def date_limit_spans(text):
    """Detect temporal syntax independently of the supported range grammar."""
    names = '|'.join(sorted(MONTHS, key=len, reverse=True))
    patterns = [
        # A time modifier may intervene between the quantity and unit. Detect
        # the limit independently without claiming a calendar/fiscal mapping.
        rf'(?:最近|近|过去|未来|接下来|连续|前|后|上|本|这|下|每|\b(?:last|past|previous|prior|next|this|current|recent|coming|every|each|rolling)\b)\s*(?:{NUMBER}\s*个?\s*)?(?:(?:calendar|fiscal|working|business|自然|工作|交易|日历|财务)\s*)+{UNIT}',
        rf'(?:最近|近|过去|未来|接下来|连续|前|后|上|本|这|下|每|\b(?:last|past|previous|prior|next|this|current|recent|coming|every|each|rolling)\b)\s*(?:{NUMBER}\s*)?{UNIT}',
        rf'{NUMBER}\s*{UNIT}\s*(?:前|后|以内|内|以来|之前|之后|\bago\b)',
        r'(?:星期|礼拜|周)[一二三四五六日天1-7]',
        rf'\b(?:since|before|after|until|from|on|during|through|excluding|except)\s+(?:(?:last|next|this)\s+)?{WEEKDAY}\b',
        rf'\b{WEEKDAY}\s+(?:refunds?|returns?|sales|reviews?)\b',
        rf'\b(?:in|since|before|after|from|until|during|on|excluding|except)\s+(?:{names})\b',
        rf'\b(?:{names})\s+(?:refunds?|returns?|sales|reviews?)\b',
        r'(?<![a-z0-9])(?:19|20|21)\d{2}(?![a-z0-9])|\d{1,2}月',
        r'去年|今年|明年|前年|后年|季度|昨日|今天|昨天|明天|至今|以来|截至|自从|\b(?:yesterday|today|tomorrow|since)\b',
        r'\b(?:before|after|until)\s+(?:launch|shipment|delivery|purchase|christmas|easter|holiday)\b',
    ]
    return [(match.start(), match.end()) for pattern in patterns for match in re.finditer(pattern, text)]


def has_unparsed_date_limit(text):
    return bool(date_limit_spans(text))


def corrected_content_query(query):
    """Retain business intent/source quotations while removing date syntax."""
    text = request_date_text(query)
    names = '|'.join(sorted(MONTHS, key=len, reverse=True))
    spans = date_limit_spans(text)
    for pattern in (DAY, MONTH, r'\b(' + names + r')\s+(20\d{2})\b'):
        spans.extend((match.start(), match.end()) for match in re.finditer(pattern, text))
    return re.sub(r'\s+', ' ', ''.join(' ' if any(start <= index < end for start, end in spans) else char for index, char in enumerate(query))).strip()


def resolve_scope(query, *, today, timezone='site_timezone'):
    """Reject ambiguous/multiple/negated date scopes instead of widening reads."""
    today = date.fromisoformat(str(today))
    text = request_date_text(query)
    found = []
    error = ''

    def add(match, start, end):
        if not any(match.start() < stop and match.end() > begin for begin, stop, *_ in found):
            found.append((match.start(), match.end(), start, end))

    for match in re.finditer(DAY, text):
        try:
            day = date(*map(int, match.groups()))
            add(match, day, day)
        except ValueError:
            error = '日期不存在，请提供有效的完整起止日期。'
    for match in re.finditer(MONTH, text):
        try:
            year, month = map(int, match.groups())
            add(match, date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]))
        except ValueError:
            error = '月份不存在，请提供有效的完整起止日期。'
    names = '|'.join(sorted(MONTHS, key=len, reverse=True))
    for match in re.finditer(r'\b(' + names + r')\s+(20\d{2})\b', text):
        month, year = MONTHS[match.group(1)], int(match.group(2))
        add(match, date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]))
    for match in re.finditer(r'本月|这个月|当月|上个月|上月|上一月|\b(?:this month|current month|last month|previous month|prior month)\b', text):
        first = today.replace(day=1)
        if match.group() in {'上月', '上个月', '上一月', 'last month', 'previous month', 'prior month'}:
            end = first - timedelta(days=1)
            start = end.replace(day=1)
        else:
            start, end = first, today
        add(match, start, end)
    for match in re.finditer(r'(?:最近|近)(\d+)天|\blast\s+(\d+)\s+days?\b', text):
        days = int(next(value for value in match.groups() if value))
        if not 1 <= days <= 366:
            error = '日期范围需在1至366天内。'
        else:
            add(match, today - timedelta(days=days - 1), today)
    found.sort()
    # A negated date is not silently converted to the default window.
    for begin, _, _, _ in found:
        clause = re.split(r'[，,；;。]', text[:begin])[-1]
        if re.search(r'不是|并非|除(?:了|去)?|不要|不(?:核对|查询|比较|统计|计算|包括)|排除|除去|无需|\b(?:not|without|do not|don\x27t|excluding|except)\b', clause):
            error = '日期限定包含否定或排除，请明确本次实际读取的起止日期。'
    if found and re.search(r'自从|以来|之前|之后|以前|以后|截至|\b(?:since|before|after|until)\b', text):
        error = '开放日期范围暂不支持，请提供完整起止日期；不会改用默认窗口。'
    if len(found) == 2:
        middle = text[found[0][1]:found[1][0]].strip()
        if found[0][2] == found[0][3] and found[1][2] == found[1][3] and re.fullmatch(r'(?:至|到|—|–|~|to|through)', middle):
            found = [(found[0][0], found[1][1], found[0][2], found[1][3])]
        else:
            error = '本次包含多个日期窗口；请分别发起调查或明确一个完整起止范围，不合并读取间隔内记录。'
    elif len(found) > 2:
        error = '本次包含多个日期窗口；请分别发起调查或明确一个完整起止范围。'
    remainder = text
    for begin, stop, *_ in reversed(found):
        remainder = remainder[:begin] + ' ' + remainder[stop:]
    if has_unparsed_date_limit(remainder):
        error = '日期表达不完整或暂不支持，请提供YYYY-MM-DD至YYYY-MM-DD；不会改用默认窗口。'
    if not found and not error:
        start, end, status = today - timedelta(days=90), today, 'default'
    elif found and not error:
        start, end, status = found[0][2], found[0][3], 'explicit'
        if start > end or (end - start).days >= 366:
            error = '起止日期顺序无效或范围超过366天，请缩小并明确范围。'
    else:
        start = end = None
        status = 'needs_clarification'
    if error:
        status, start, end = 'needs_clarification', None, None
    return {'version': VERSION, 'status': status, 'timezone': timezone, 'anchor_date': str(today),
            'start': str(start) if start else None, 'end': str(end) if end else None,
            'requested_periods': [{'start': str(first), 'end': str(last)} for _, _, first, last in found],
            'reason': error or ('未限定日期：原文/事件使用最近90天，指标保留各自冻结两窗，互不替代。' if status == 'default' else '按店铺时区冻结显式日期；本月截止本次站点日期。'),
            'content_query': re.sub(r'\s+', ' ', ''.join(' ' if any(begin <= index < stop for begin, stop, *_ in found) else char for index, char in enumerate(query))).strip() if status == 'explicit' else query}


def validate_scope_state(state):
    frozen = state.get('input_versions', {}).get('request_scope')
    if not frozen and state.get('search_end') and resolve_scope(state.get('query', ''), today=state['search_end'])['status'] != 'default':
        raise RuntimeError('legacy_explicit_date_scope_requires_new_run')
    if frozen and state.get('scope_query') != frozen.get('resolution_query'):
        raise RuntimeError('investigation_date_scope_changed')
    if frozen and (state.get('request_scope') != frozen or state.get('search_start') != frozen['start'] or state.get('search_end') != frozen['end'] or state.get('content_query') != frozen['content_query']):
        raise RuntimeError('investigation_date_scope_changed')

import re


def fmt_elapsed(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f'{h:02d}:{m:02d}:{s:02d}'


def date_convert(ymd: str) -> str:
    """comUtil.java dateConvert() 동일 로직.
    입력: yyyyMMdd | yyyy.M.d. | yyyy.M.d | yyyyㆍMㆍd
    출력: yyyyMMdd | 미매칭 패턴 → 숫자만 추출한 문자열
    """
    if not ymd or not ymd.strip():
        return ''
    ymd = ymd.strip()
    if re.match(r'^\d{8}$', ymd):
        return ymd
    m = re.match(r'^(\d{4})[.·ㆍ](\d{1,2})[.·ㆍ](\d{1,2})[.·ㆍ]?$', ymd)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return re.sub(r'\D', '', ymd)

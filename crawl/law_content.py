import asyncio
import csv
import time
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from tqdm import tqdm

from common.csv_writer import CsvWriter
from common.http_client import fetch_xml, make_async_client, RETRYABLE
from common.logger import JobLogger
from common.utils import fmt_elapsed
from common.xml_parser import (
    convert_circled_number, get_tag, get_tag_attr_and_value,
    get_tag_value, make_jo_num, parse_xml_string,
)

# ── CSV 헤더 ────────────────────────────────────────────────────────────────
LAW_CON_HEADERS = [
    'DOCKEY', 'JOMUN_DVS_NM', 'CTXT', 'LAW_SRNO', 'CRNT_LAW_NM', 'LAW_ID',
    'PRMLGT_YMD', 'PRMLGT_NO', 'LAW_DVS_CD_NM', 'LAW_HAN_NM',
    'LAW_CHNCHR_NM', 'LAW_ABRVTD_NM', 'ENFC_YMD',
]
LAW_JO_CON_HEADERS = [
    'DOCKEY', 'JOMUN_NO', 'JO_NO', 'TITLE', 'CTXT', 'LAW_SRNO', 'CRNT_LAW_NM',
    'LAW_ID', 'PRMLGT_YMD', 'PRMLGT_NO', 'LAW_DVS_CD_NM', 'LAW_HAN_NM',
    'LAW_CHNCHR_NM', 'LAW_ABRVTD_NM', 'JOMUN_CHG_YN', 'ENFC_YMD',
]
LAW_HANG_CON_HEADERS = [
    'DOCKEY', 'JOMUN_NO', 'JO_NO', 'TITLE', 'HANG_NO', 'CTXT', 'LAW_SRNO',
    'CRNT_LAW_NM', 'LAW_ID', 'PRMLGT_YMD', 'PRMLGT_NO', 'LAW_DVS_CD_NM',
    'LAW_HAN_NM', 'LAW_CHNCHR_NM', 'LAW_ABRVTD_NM', 'JOMUN_CHG_YN', 'ENFC_YMD',
]


# ── law_list CSV → meta 목록 로드 ────────────────────────────────────────────
def load_metas(csv_dir: Path) -> list:
    files = sorted(csv_dir.glob('law_list_????_*.csv'))
    if not files:
        raise FileNotFoundError(f'law_list CSV 없음: {csv_dir}/law_list_????_*.csv')
    metas = []
    for f in files:
        with open(f, 'r', encoding='utf-8') as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                link = row.get('LINK', '').strip()
                if link:
                    metas.append({
                        'link':        link,
                        'law_srno':    row.get('LAW_SRNO', '').strip(),
                        'crnt_law_nm': row.get('CRNT_LAW_NM', '').strip(),
                    })
    return metas


# ── 공통 기본정보 추출 ────────────────────────────────────────────────────────
def _base(root, meta: dict) -> dict:
    law_type = get_tag_attr_and_value(root, '법종구분', '법종구분코드')
    return {
        'law_id':        get_tag_value(root, '법령ID'),
        'law_srno':      meta['law_srno'],
        'prmlgt_ymd':    get_tag_value(root, '공포일자'),
        'prmlgt_no':     get_tag_value(root, '공포번호'),
        'law_han_nm':    get_tag_value(root, '법령명_한글'),
        'law_chnchr_nm': get_tag_value(root, '법령명_한자'),
        'law_abrvtd_nm': get_tag_value(root, '법령명약칭'),
        'enfc_ymd':      get_tag_value(root, '시행일자'),
        'crnt_law_nm':   meta['crnt_law_nm'],
        'law_dvs_cd_nm': law_type['text'],
    }


def _build_hang_ho_mok(jomun_el) -> list:
    lines = []
    for hang in jomun_el.findall('.//항'):
        hang_con = get_tag(hang, '항내용')
        if hang_con:
            lines.append(hang_con.strip())
        for ho in hang.findall('.//호'):
            ho_con = get_tag(ho, '호내용')
            if ho_con:
                lines.append(ho_con.strip())
            for mok in ho.findall('.//목'):
                mok_con = get_tag(mok, '목내용')
                if mok_con:
                    lines.append(mok_con.strip())
    return lines


# ── law_con 파싱 (법령단위: URL 1개 → 행 1개) ───────────────────────────────
def parse_law_con(root, meta: dict):
    b = _base(root, meta)

    jomun_field = root.find('.//조문')
    if jomun_field is None:
        return None
    jomun_list = jomun_field.findall('조문단위')
    if not jomun_list:
        return None

    jb = []
    ctxt = ''
    jomun_dvs_nm = ''
    for jomun in jomun_list:
        jomun_con = get_tag(jomun, '조문내용')
        if not jomun_con:
            continue

        jomun_yn = get_tag(jomun, '조문여부')
        if not jomun_dvs_nm and jomun_yn:
            jomun_dvs_nm = jomun_yn

        if jomun_yn == '조문':
            jb.append(jomun_con)
            jb.extend(_build_hang_ho_mok(jomun))
            ctxt = '\n'.join(jb)
        else:
            if len(jomun_list) == 1:
                ctxt = jomun_con

    dockey = f"{b['law_srno']}_{b['enfc_ymd']}"
    return [
        dockey, jomun_dvs_nm, ctxt, b['law_srno'], b['crnt_law_nm'],
        b['law_id'], b['prmlgt_ymd'], b['prmlgt_no'], b['law_dvs_cd_nm'],
        b['law_han_nm'], b['law_chnchr_nm'], b['law_abrvtd_nm'], b['enfc_ymd'],
    ]


# ── law_jo_con 파싱 (조문단위: URL 1개 → 행 N개) ────────────────────────────
def parse_law_jo_con(root, meta: dict) -> list:
    b = _base(root, meta)

    jomun_field = root.find('.//조문')
    if jomun_field is None:
        return []
    jomun_list = jomun_field.findall('조문단위')
    if not jomun_list:
        return []

    result = {}
    for jomun in jomun_list:
        jomun_con = get_tag(jomun, '조문내용')
        if not jomun_con:
            continue

        jomun_no     = get_tag(jomun, '조문번호')
        jomun_ser_no = get_tag(jomun, '조문가지번호')
        jomun_yn     = get_tag(jomun, '조문여부')
        jomun_chg_yn = get_tag(jomun, '조문변경여부')

        jo_no = make_jo_num(jomun_no, jomun_ser_no)
        jo_con_no = (f'제{jomun_no}조의{jomun_ser_no}' if jomun_ser_no else f'제{jomun_no}조') if jomun_no and jomun_no != '0' else ''

        if jomun_yn == '조문':
            ctxt = '\n'.join([jomun_con] + _build_hang_ho_mok(jomun))
            dockey = f"{b['law_srno']}_{b['enfc_ymd']}_{jo_no}"
            result.setdefault(dockey, {
                'JOMUN_NO': jomun_no, 'JO_NO': jo_no,
                'TITLE': ' '.join(filter(None, [b['law_han_nm'], jo_con_no])),
                'CTXT': ctxt, 'JOMUN_CHG_YN': jomun_chg_yn,
            })
        elif len(jomun_list) == 1:
            dockey = f"{b['law_srno']}_{b['enfc_ymd']}_000000"
            result.setdefault(dockey, {
                'JOMUN_NO': '0', 'JO_NO': '000000',
                'TITLE': b['law_han_nm'],
                'CTXT': jomun_con, 'JOMUN_CHG_YN': jomun_chg_yn,
            })

    return [
        [
            dockey, rd['JOMUN_NO'], rd['JO_NO'], rd['TITLE'], rd['CTXT'],
            b['law_srno'], b['crnt_law_nm'], b['law_id'], b['prmlgt_ymd'], b['prmlgt_no'],
            b['law_dvs_cd_nm'], b['law_han_nm'], b['law_chnchr_nm'], b['law_abrvtd_nm'],
            rd['JOMUN_CHG_YN'], b['enfc_ymd'],
        ]
        for dockey, rd in result.items()
    ]


# ── law_hang_con 파싱 (항단위: URL 1개 → 행 N개) ────────────────────────────
def parse_law_hang_con(root, meta: dict) -> list:
    b = _base(root, meta)

    jomun_field = root.find('.//조문')
    if jomun_field is None:
        return []
    jomun_list = jomun_field.findall('조문단위')
    if not jomun_list:
        return []

    result = {}
    for jomun in jomun_list:
        jomun_no     = get_tag(jomun, '조문번호')
        jomun_ser_no = get_tag(jomun, '조문가지번호')
        jomun_con    = get_tag(jomun, '조문내용')
        jomun_yn     = get_tag(jomun, '조문여부')
        jomun_chg_yn = get_tag(jomun, '조문변경여부')

        jo_no = make_jo_num(jomun_no, jomun_ser_no)
        jo_con_no = (f'제{jomun_no}조의{jomun_ser_no}' if jomun_ser_no else f'제{jomun_no}조') if jomun_no and jomun_no != '0' else ''

        def _process_hangs(hang_list, jo_no_val, jo_con_no_val):
            for hang_el in hang_list:
                hang_no = convert_circled_number(get_tag(hang_el, '항번호')).strip()
                if not hang_no or not hang_no.isdigit():
                    continue
                hang_no_nm = f'제{hang_no}항' if hang_no != '0' else ''

                all_hang = []
                hang_con = get_tag(hang_el, '항내용').strip()
                if hang_con:
                    all_hang.append(hang_con)
                for ho in hang_el.findall('.//호'):
                    ho_con = get_tag(ho, '호내용')
                    if ho_con:
                        all_hang.append(ho_con.strip())
                    for mok in ho.findall('.//목'):
                        mok_con = get_tag(mok, '목내용')
                        if mok_con:
                            all_hang.append(mok_con.strip())

                ctxt = '\r\n'.join(filter(None, [jomun_con, '\n'.join(all_hang)]))
                dockey = f"{b['law_srno']}_{b['enfc_ymd']}_{jo_no_val}_{hang_no}"
                result.setdefault(dockey, {
                    'JOMUN_NO': jomun_no, 'JO_NO': jo_no_val,
                    'TITLE': ' '.join(filter(None, [b['law_han_nm'], jo_con_no_val, hang_no_nm])),
                    'HANG_NO': hang_no, 'CTXT': ctxt,
                    'JOMUN_CHG_YN': jomun_chg_yn,
                })

        hang_list = jomun.findall('.//항')
        if jomun_yn == '조문':
            _process_hangs(hang_list, jo_no, jo_con_no)
        elif len(jomun_list) == 1:
            _process_hangs(hang_list, '000000', '')

    return [
        [
            dockey, rd['JOMUN_NO'], rd['JO_NO'], rd['TITLE'], rd['HANG_NO'], rd['CTXT'],
            b['law_srno'], b['crnt_law_nm'], b['law_id'], b['prmlgt_ymd'], b['prmlgt_no'],
            b['law_dvs_cd_nm'], b['law_han_nm'], b['law_chnchr_nm'], b['law_abrvtd_nm'],
            rd['JOMUN_CHG_YN'], b['enfc_ymd'],
        ]
        for dockey, rd in result.items()
    ]


# ── 메인 실행 ────────────────────────────────────────────────────────────────
async def run(cfg: dict, ymd: str, logger: JobLogger, test_urls: list = None):
    csv_dir = Path(cfg['csv_path']) / ymd
    csv_dir.mkdir(parents=True, exist_ok=True)

    if test_urls:
        metas = [{'link': u, 'law_srno': '', 'crnt_law_nm': ''} for u in test_urls]
        logger.info(f'[law_content] 테스트 URL {len(metas)}건 지정')
    else:
        try:
            metas = load_metas(csv_dir)
        except Exception as exc:
            logger.error(f'[law_content] law_list CSV 로드 실패: {exc}')
            return
        logger.info(f'[law_content] 대상 URL: {len(metas)}건')
    total = len(metas)

    writers = {
        'law_con':      CsvWriter('law_con',      csv_dir, LAW_CON_HEADERS,      cfg['batch_size'], ymd, logger),
        'law_jo_con':   CsvWriter('law_jo_con',   csv_dir, LAW_JO_CON_HEADERS,   cfg['batch_size'], ymd, logger),
        'law_hang_con': CsvWriter('law_hang_con', csv_dir, LAW_HANG_CON_HEADERS, cfg['batch_size'], ymd, logger),
    }

    fail_list = []
    done_count = 0
    start_time = time.time()

    async with make_async_client(verify_ssl=(cfg['env'] != 'dev')) as client:

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type(RETRYABLE),
            reraise=True,
        )
        async def _fetch_and_parse(url: str):
            return parse_xml_string(await fetch_xml(client, url))

        q: asyncio.Queue = asyncio.Queue()
        for m in metas:
            q.put_nowait(m)

        logged_pct: set = set()
        with tqdm(total=total, desc='[law_content]', unit='건') as pbar:

            async def worker():
                nonlocal done_count
                while True:
                    try:
                        meta = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        root = await _fetch_and_parse(meta['link'])

                        con_row   = parse_law_con(root, meta)
                        jo_rows   = parse_law_jo_con(root, meta)
                        hang_rows = parse_law_hang_con(root, meta)

                        if con_row:
                            writers['law_con'].put(con_row)
                        for r in jo_rows:
                            writers['law_jo_con'].put(r)
                        for r in hang_rows:
                            writers['law_hang_con'].put(r)

                    except Exception as exc:
                        fail_list.append(meta)
                        logger.log_link_fail(meta['link'], str(exc), done_count, total)
                    finally:
                        done_count += 1
                        pbar.update(1)
                        pbar.set_postfix({'실패': len(fail_list)})
                        milestone = (done_count * 100 // total) // 10 * 10
                        if milestone and milestone not in logged_pct:
                            logged_pct.add(milestone)
                            logger.info(f'[law_content] 진행: {done_count:,}/{total:,}건 ({milestone}%) | 실패: {len(fail_list)}건')

            await asyncio.gather(*[worker() for _ in range(cfg['concurrency'])])

    for w in writers.values():
        w.close()

    elapsed = fmt_elapsed(time.time() - start_time)

    if fail_list:
        logger.save_final_fails(fail_list, elapsed)

    wc = writers['law_con']
    wj = writers['law_jo_con']
    wh = writers['law_hang_con']
    logger.info(
        f'[완료] law_content | 수집: {total - len(fail_list):,}/{total:,}건 | 실패: {len(fail_list):,}건 | 소요: {elapsed}'
        f' | law_con: {wc.total_rows:,}건/{wc.file_count}개 | law_jo_con: {wj.total_rows:,}건/{wj.file_count}개 | law_hang_con: {wh.total_rows:,}건/{wh.file_count}개'
    )

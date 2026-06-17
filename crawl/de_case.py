import asyncio
import re
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from tqdm import tqdm

from common.csv_writer import CsvWriter
from common.http_client import fetch_xml, make_async_client, RETRYABLE
from common.logger import JobLogger
from common.utils import date_convert, fmt_elapsed
from common.xml_parser import get_tag_value, parse_xml_string

HEADERS = ['INSTN_DCSNST_SRNO', 'DCSNST_SRNO', 'INSTN', 'CS_NO', 'CS_NM', 'DOC_KND', 'DCSN_YMD', 'CTXT', 'DATA_YMD']

# comDto.java dcaseTg / dcasNm 순서 그대로 대응
# DeCase.java switch-case에 없는 eiac, kcc는 default 태그 사용
DCASE_TAG_MAP = {
    'ppc':             {'srno': '결정문일련번호',            'linkkey': '결정문상세링크',         'cs_nm': '안건명',   'cs_no': '안건번호', 'dec_dt': '의결일자', 'data_dt': '의결일자',     'jm': '주문',    'eu': '이유'},
    'eiac':            {'srno': '결정문일련번호',            'linkkey': '결정문상세링크',         'cs_nm': '사건명',   'cs_no': '사건번호', 'dec_dt': '의결일자', 'data_dt': '의결일자',     'jm': '주문',    'eu': '이유'},
    'ftc':             {'srno': '결정문일련번호',            'linkkey': '결정문상세링크',         'cs_nm': '사건명',   'cs_no': '사건번호', 'dec_dt': '결정일자', 'data_dt': '결정일자',     'jm': '주문',    'eu': '이유'},
    'nhrck':           {'srno': '결정문일련번호',            'linkkey': '결정문상세링크',         'cs_nm': '사건명',   'cs_no': '사건번호', 'dec_dt': '의결일자', 'data_dt': '데이터기준일시', 'jm': '주문',   'eu': '이유'},
    'acr':             {'srno': '결정문일련번호',            'linkkey': '결정문상세링크',         'cs_nm': '결정구분', 'cs_no': '의안번호', 'dec_dt': '의결일',   'data_dt': '의결일자',     'jm': '주문',    'eu': '이유'},
    'fsc':             {'srno': '결정문일련번호',            'linkkey': '결정문상세링크',         'cs_nm': '안건명',   'cs_no': '의결번호', 'dec_dt': '의결일자', 'data_dt': '의결일자',     'jm': '조치내용', 'eu': '조치이유'},
    'kcc':             {'srno': '결정문일련번호',            'linkkey': '결정문상세링크',         'cs_nm': '사건명',   'cs_no': '사건번호', 'dec_dt': '의결일자', 'data_dt': '의결일자',     'jm': '주문',    'eu': '이유'},
    'iaciac':          {'srno': '결정문일련번호',            'linkkey': '결정문상세링크',         'cs_nm': '사건',     'cs_no': '사건번호', 'dec_dt': '의결일자', 'data_dt': '의결일자',     'jm': '주문',    'eu': '이유'},
    'ttSpecialDecc':   {'srno': '특별행정심판재결례일련번호', 'linkkey': '행정심판재결례상세링크', 'cs_nm': '사건명',   'cs_no': '사건번호', 'dec_dt': '의결일자', 'data_dt': '의결일자',     'jm': '주문',    'eu': '이유'},
    'ecc':             {'srno': '결정문일련번호',            'linkkey': '결정문상세링크',         'cs_nm': '사건명',   'cs_no': '의결번호', 'dec_dt': '의결일자', 'data_dt': '의결일자',     'jm': '주문',    'eu': '이유'},
    'sfc':             {'srno': '결정문일련번호',            'linkkey': '결정문상세링크',         'cs_nm': '안건명',   'cs_no': '의결번호', 'dec_dt': '의결일자', 'data_dt': '의결일자',     'jm': '조치내용', 'eu': '조치이유'},
    'kmstSpecialDecc': {'srno': '특별행정심판재결례일련번호', 'linkkey': '행정심판재결례상세링크', 'cs_nm': '사건명',   'cs_no': '사건번호', 'dec_dt': '의결일자', 'data_dt': '의결일자',     'jm': '주문',    'eu': '이유'},
    'decc':            {'srno': '행정심판례일련번호',        'linkkey': '행정심판례상세링크',     'cs_nm': '사건명',   'cs_no': '사건번호', 'dec_dt': '의결일자', 'data_dt': '의결일자',     'jm': '주문',    'eu': '이유'},
}

# comDto.java dcaseTg / dcasNm 1:1 매핑
DCASE_NAMES = {
    'ppc':             '개인정보보호위원회',
    'eiac':            '고용보험심사위원회',
    'ftc':             '공정거래위원회',
    'nhrck':           '국가인권위원회',
    'acr':             '국민권익위원회',
    'fsc':             '금융위원회',
    'kcc':             '방송미디어통신위원회',
    'iaciac':          '산업재해보상보험재심사위원회',
    'ttSpecialDecc':   '조세심판원 특별행정심판재결례',
    'ecc':             '중앙환경분쟁조정위원회',
    'sfc':             '증권선물위원회',
    'kmstSpecialDecc': '해양안전심판원 특별행정심판재결례',
    'decc':            '행정심판례',
}


def _make_list_url(api_list_url: str, target: str) -> str:
    base = re.sub(r'[?&]target=[^&]*', '', api_list_url)
    sep = '&' if '?' in base else '?'
    return f"{base}{sep}target={target}"


def _con_base(api_con_path: str) -> str:
    parsed = urlparse(api_con_path)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _adjust_link(link: str, con_base: str) -> str:
    link = link.replace('&type=HTML', '&type=XML')
    if 'type=' not in link:
        link += '&type=XML'
    return link.replace('/DRF/lawService.do', con_base)


def _is_target_date(date_str: str) -> bool:
    try:
        d = date_convert(date_str)
        if len(d) < 8:
            return False
        tg = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        today = date.today()
        return today - timedelta(days=1) <= tg <= today
    except (ValueError, TypeError):
        return False


async def _fetch_list(client, list_url: str, target: str, con_base: str, tags: dict, mode: str) -> list:
    xml = await fetch_xml(client, list_url)
    root = parse_xml_string(xml)

    cnt_el = root.find('.//totalCnt')
    if cnt_el is None:
        return []

    total_cnt = int(cnt_el.text.strip())
    if target == 'nhrck':
        page_count = total_cnt + 1
        disp = 1
    else:
        page_count = total_cnt // 100 + 1
        disp = 100

    linkkey = tags['linkkey']
    metas = []

    for page in range(1, page_count + 1):
        page_url = f'{list_url}&page={page}&display={disp}&sort=ddes'
        page_xml = await fetch_xml(client, page_url)
        page_root = parse_xml_string(page_xml)

        link_els = page_root.findall(f'.//{linkkey}')
        if not link_els:
            continue

        if mode == 'insert':
            day_els = page_root.findall('.//의결일자')
            if not day_els:
                return []
            for i, link_el in enumerate(link_els):
                day_el = day_els[i] if i < len(day_els) else None
                day_str = (day_el.text or '').strip() if day_el is not None else ''
                if not _is_target_date(day_str):
                    return []
                link = (link_el.text or '').strip()
                if link:
                    metas.append({'link': _adjust_link(link, con_base), 'target': target})
        else:
            for link_el in link_els:
                link = (link_el.text or '').strip()
                if link:
                    metas.append({'link': _adjust_link(link, con_base), 'target': target})

    return metas


def _parse_detail(root, target: str, tags: dict) -> list:
    dcsnst_srno = get_tag_value(root, tags['srno'])
    instn_dcsnst_srno = f"{target}_{dcsnst_srno}"
    instn = DCASE_NAMES.get(target, target)
    cs_no = get_tag_value(root, tags['cs_no'])
    cs_nm = get_tag_value(root, tags['cs_nm'])
    dcsn_ymd = date_convert(get_tag_value(root, tags['dec_dt']))

    ctxt = re.sub(
        r'\s+', ' ',
        f"{get_tag_value(root, tags['jm'])} {get_tag_value(root, tags['eu'])}"
    ).strip()

    data_ymd = date_convert(get_tag_value(root, tags['data_dt']))
    if not data_ymd:
        data_ymd = dcsn_ymd

    return [instn_dcsnst_srno, dcsnst_srno, instn, cs_no, cs_nm, target, dcsn_ymd, ctxt, data_ymd]


async def run(cfg: dict, ymd: str, logger: JobLogger, mode: str = 'all', test_urls: list = None, target: str = None):
    csv_dir = Path(cfg['csv_path']) / ymd
    csv_dir.mkdir(parents=True, exist_ok=True)

    verify_ssl = (cfg['env'] != 'dev')
    con_base = _con_base(cfg['api_con_path'])
    start_time = time.time()

    tag_map = {target: DCASE_TAG_MAP[target]} if (test_urls and target and target in DCASE_TAG_MAP) else DCASE_TAG_MAP

    for target, tags in tag_map.items():
        list_url = _make_list_url(cfg['api_list_url'], target)
        logger.info(f'[de_case/{target}] 목록 조회: {list_url}')

        total = 0
        fail_list = []
        done_count = 0
        writer = None

        try:
            if test_urls:
                metas = [{'link': u, 'target': target} for u in test_urls]
                total = len(metas)
                logger.info(f'[de_case/{target}] 테스트 URL {total}건 지정')
            else:
                async with make_async_client(verify_ssl=verify_ssl) as client:
                    metas = await _fetch_list(client, list_url, target, con_base, tags, mode)

                total = len(metas)
                logger.info(f'[de_case/{target}] 대상: {total}건')

            if not total:
                continue

            writer = CsvWriter(f'de_case_{target}', csv_dir, HEADERS, cfg['batch_size'], ymd, logger)

            async with make_async_client(verify_ssl=verify_ssl) as client:

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
                with tqdm(total=total, desc=f'[de_case/{target}]', unit='건') as pbar:

                    async def worker(tags=tags):
                        nonlocal done_count
                        while True:
                            try:
                                meta = q.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            try:
                                detail_root = await _fetch_and_parse(meta['link'])
                                row = _parse_detail(detail_root, meta['target'], tags)
                                writer.put(row)
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
                                    logger.info(f'[de_case/{target}] 진행: {done_count:,}/{total:,}건 ({milestone}%) | 실패: {len(fail_list)}건')

                    await asyncio.gather(*[worker() for _ in range(cfg['concurrency'])])

        except Exception as exc:
            logger.error(f'[de_case/{target}] 수집 중 오류: {exc}')
        finally:
            if writer is not None:
                writer.close()

        if not total or writer is None:
            continue

        tgt_elapsed = fmt_elapsed(time.time() - start_time)

        if fail_list:
            logger.save_final_fails(fail_list, tgt_elapsed)

        logger.info(
            f'[완료] de_case/{target} | 수집: {total - len(fail_list):,}/{total:,}건'
            f' | 실패: {len(fail_list):,}건'
            f' | de_case_{target}: {writer.total_rows:,}건/{writer.file_count}개'
        )

    logger.info(f'[de_case] 전체 완료 | 소요: {fmt_elapsed(time.time() - start_time)}')

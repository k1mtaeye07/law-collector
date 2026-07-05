import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from tqdm import tqdm

from common.csv_writer import CsvWriter
from common.http_client import fetch_xml, make_async_client, RETRYABLE
from common.logger import JobLogger
from common.utils import fmt_elapsed
from common.xml_parser import get_tag_value, parse_xml_string

# comDto.java authTg / authNm 그대로 대응
AUTHINT_TARGETS = [
    'moelCgmExpc',
    'molitCgmExpc',
    'mofCgmExpc',
    'moisCgmExpc',
    'meCgmExpc',
    'kcsCgmExpc',
    'molegCgmExpc',
]
AUTHINT_NAMES = {
    'moelCgmExpc':  '고용노동부',
    'molitCgmExpc': '국토교통부',
    'mofCgmExpc':   '해상수산부',
    'moisCgmExpc':  '행정안전부',
    'meCgmExpc':    '환경부',
    'kcsCgmExpc':   '관세청',
    'molegCgmExpc': '법제처',
}

HEADERS = ['SRNO', 'TITL', 'DOC_NO', 'DCSN_YMD', 'INSTN', 'CTXT', 'DOC_KND', 'DATA_YMD', 'RLTD_LAW']


def _make_list_url(api_list_url: str, target: str, ymd: str) -> str:
    base = re.sub(r'[?&]target=[^&]*', '', api_list_url)
    sep = '&' if '?' in base else '?'
    return f"{base}{sep}target={target}&explYd={ymd}"


def _con_base(api_con_path: str) -> str:
    parsed = urlparse(api_con_path)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _adjust_link(link: str, con_base: str) -> str:
    link = link.replace('&type=HTML', '&type=XML')
    if 'type=' not in link:
        link += '&type=XML'
    return link.replace('/DRF/lawService.do', con_base)


def _parse_detail(root, target: str):
    srno = get_tag_value(root, '법령해석일련번호')
    if not srno:
        return None

    qpt = get_tag_value(root, '질의요지')
    for marker in ('<table', '<html'):
        idx = qpt.lower().find(marker)
        if idx != -1:
            qpt = qpt[:idx]
    qpt = qpt.replace('<!DOCTYPE html>', '')

    ctxt = re.sub(
        r'\s+', ' ',
        f"{qpt} {get_tag_value(root, '화답')} {get_tag_value(root, '이유')}"
    ).strip()
    ctxt = ctxt.replace('<질의요지>', '')

    rltd_law = get_tag_value(root, '관련법령')
    rltd_law = rltd_law.replace('「', '').replace('」', '').replace('\n', '').replace('/', '')

    return [
        srno,
        get_tag_value(root, '안건명'),
        get_tag_value(root, '안건번호'),
        get_tag_value(root, '해석일자'),
        get_tag_value(root, '해석기관명'),
        ctxt,
        target,
        get_tag_value(root, '데이터기준일시'),
        rltd_law,
    ]


async def run(cfg: dict, ymd: str, logger: JobLogger, test_urls: list = None, target: str = None):
    csv_dir = Path(cfg['csv_path']) / ymd
    csv_dir.mkdir(parents=True, exist_ok=True)

    verify_ssl = (cfg['env'] != 'dev')
    con_base = _con_base(cfg['api_con_path'])
    start_time = time.time()

    targets = [target] if (test_urls and target) else (AUTHINT_TARGETS if not test_urls else [AUTHINT_TARGETS[0]])

    for target in targets:
        list_url = _make_list_url(cfg['api_list_url'], target, ymd)
        logger.info(f'[auth_int/{target}] 목록 조회: {list_url}')

        writer = CsvWriter(f'auth_int_{target}', csv_dir, HEADERS, cfg['batch_size'], ymd, logger)
        fail_list = []
        done_count = 0
        total = 0

        try:
            async with make_async_client(verify_ssl=verify_ssl) as client:

                if test_urls:
                    metas = [{'link': u, 'target': target} for u in test_urls]
                    total = len(metas)
                    logger.info(f'[auth_int/{target}] 테스트 URL {total}건 지정')
                else:
                    # 1. 목록 수집
                    first_xml = await fetch_xml(client, f'{list_url}&page=1&display=100')
                    first_root = parse_xml_string(first_xml)
                    cnt_el = first_root.find('.//totalCnt')

                    if cnt_el is None:
                        logger.info(f'[auth_int/{target}] totalCnt 없음 → 건너뜀')
                        writer.close()
                        continue

                    total_cnt = int(cnt_el.text.strip())
                    total_pages = total_cnt // 100 + 1
                    logger.info(f'[auth_int/{target}] totalCnt={total_cnt}, pages={total_pages}')

                    metas = []
                    for page in range(1, total_pages + 1):
                        page_xml = await fetch_xml(client, f'{list_url}&page={page}&display=100')
                        page_root = parse_xml_string(page_xml)
                        for el in page_root.findall('.//법령해석상세링크'):
                            link = (el.text or '').strip()
                            if link:
                                metas.append({'link': _adjust_link(link, con_base), 'target': target})

                    total = len(metas)
                    logger.info(f'[auth_int/{target}] 대상: {total}건')

                # 2. 상세 수집 (병렬)
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

                seen: set = set()
                logged_pct: set = set()
                with tqdm(total=total, desc=f'[auth_int/{target}]', unit='건') as pbar:

                    async def worker():
                        nonlocal done_count
                        while True:
                            try:
                                meta = q.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            try:
                                detail_root = await _fetch_and_parse(meta['link'])
                                row = _parse_detail(detail_root, meta['target'])
                                if row and row[0] not in seen:
                                    seen.add(row[0])
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
                                    logger.info(f'[auth_int/{target}] 진행: {done_count:,}/{total:,}건 ({milestone}%) | 실패: {len(fail_list)}건')

                    await asyncio.gather(*[worker() for _ in range(cfg['concurrency'])])

        except Exception as exc:
            logger.error(f'[auth_int/{target}] 수집 중 오류: {exc}')

        writer.close()
        tgt_elapsed = fmt_elapsed(time.time() - start_time)

        if fail_list:
            logger.save_final_fails(fail_list, tgt_elapsed)

        logger.info(
            f'[완료] auth_int/{target} | 수집: {total - len(fail_list):,}/{total:,}건'
            f' | 실패: {len(fail_list):,}건'
            f' | auth_int_{target}: {writer.total_rows:,}건/{writer.file_count}개'
        )

    logger.info(f'[auth_int] 전체 완료 | 소요: {fmt_elapsed(time.time() - start_time)}')

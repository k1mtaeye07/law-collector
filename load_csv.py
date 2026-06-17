"""
CSV → PostgreSQL 적재 (psycopg2 copy_expert, 단일 트랜잭션)

사용법:
  python load_csv.py <table> <env> <ymd> [--dir CSV경로]
  python load_csv.py law_hang_con law 20260608

동작:
  1. {csv_path}/{ymd}/{table}_????_*.csv 파일 수집
  2. 단일 트랜잭션 내:
     - 기존 행 있으면 TRUNCATE
     - 모든 CSV 파일 순차 COPY (integer/date 빈값 → NULL 자동 처리)
     - 전체 성공 시 COMMIT, 실패 시 ROLLBACK (테이블 원상 보존)
  3. 완료 요약 + 로그 파일 기록 (logs_path/{ymd}/load_*.log)
"""
import argparse
import glob
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg2

from common.config import load_config
from common.utils import fmt_elapsed

COPY_HEADERS = {
    'law_list':     'DOCKEY,LAW_SRNO,LAW_ID,ENTRVS_DVS_CD,ENTRVS_DVS_NM,PRMLGT_YMD,PRMLGT_NO,LAW_HAN_NM,ENFC_YMD,CRNT_LAW_NM,LINK',
    'law_con':      'DOCKEY,JOMUN_DVS_NM,CTXT,LAW_SRNO,CRNT_LAW_NM,LAW_ID,PRMLGT_YMD,PRMLGT_NO,LAW_DVS_CD_NM,LAW_HAN_NM,LAW_CHNCHR_NM,LAW_ABRVTD_NM,ENFC_YMD',
    'law_jo_con':   'DOCKEY,JOMUN_NO,JO_NO,TITLE,CTXT,LAW_SRNO,CRNT_LAW_NM,LAW_ID,PRMLGT_YMD,PRMLGT_NO,LAW_DVS_CD_NM,LAW_HAN_NM,LAW_CHNCHR_NM,LAW_ABRVTD_NM,JOMUN_CHG_YN,ENFC_YMD',
    'law_hang_con': 'DOCKEY,JOMUN_NO,JO_NO,TITLE,HANG_NO,CTXT,LAW_SRNO,CRNT_LAW_NM,LAW_ID,PRMLGT_YMD,PRMLGT_NO,LAW_DVS_CD_NM,LAW_HAN_NM,LAW_CHNCHR_NM,LAW_ABRVTD_NM,JOMUN_CHG_YN,ENFC_YMD',
    'auth_int':     'SRNO,TITL,DOC_NO,DCSN_YMD,INSTN,CTXT,DOC_KND,DATA_YMD',
    'de_case':      'INSTN_DCSNST_SRNO,DCSNST_SRNO,INSTN,CS_NO,CS_NM,DOC_KND,DCSN_YMD,CTXT,DATA_YMD',
}

# integer/date 데이터 타입 에러 방지 (CSV의 "" → NULL로 자동 변환)
FORCE_NULL_COLS = {
    'law_list':     'law_srno,law_id,entrvs_dvs_cd,prmlgt_ymd,prmlgt_no',
    'law_con':      'law_srno,law_id,prmlgt_ymd,prmlgt_no',
    'law_jo_con':   'law_srno,law_id,prmlgt_ymd,prmlgt_no',
    'law_hang_con': 'law_srno,law_id,prmlgt_ymd,prmlgt_no',
    'auth_int':     'dcsn_ymd,data_ymd',
    'de_case':      'dcsn_ymd,data_ymd',
}

# auth_int/de_case는 타겟별 job_name(auth_int_{target}, de_case_{target})으로 파일 생성
GLOB_PATTERNS = {
    'auth_int': 'auth_int_*_????_*.csv',
    'de_case':  'de_case_*_????_*.csv',
}


def setup_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    dt = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = log_dir / f'load_{dt}.log'

    logger = logging.getLogger(f'load.{dt}')
    fmt = logging.Formatter('[%(asctime)s] %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S')
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.setLevel(logging.INFO)
    return logger

# DB 연결 (psycopg2)
def _connect(db_cfg: dict):
    return psycopg2.connect(
        host=db_cfg.get('host', 'localhost'),
        port=int(db_cfg.get('port', 5432)),
        dbname=db_cfg['dbname'],
        user=db_cfg['user'],
        password=db_cfg.get('password', ''),
    )

"""
[run함수 전체 흐름]

파일 없음 → FileNotFoundError 즉시 발생
     ↓
TRUNCATE + COPY 시작 (트랜잭션 내)
     ↓
중간에 오류 → ROLLBACK → 테이블 이전 상태 유지
     ↓
전부 성공 → COMMIT → DB 반영
     ↓
finally: conn.close() (무조건 실행)
"""

def run(table: str, cfg: dict, ymd: str, csv_dir_override: str = None, logger=None):
    """단일 테이블 적재. main.py에서도 호출 가능."""
    db_cfg = cfg.get('db')
    if not db_cfg:
        raise RuntimeError('db 설정 없음')

    log_dir = Path(cfg['logs_path']) / ymd
    if logger is None:
        logger = setup_logger(log_dir)

    csv_dir = Path(csv_dir_override or cfg['csv_path']) / ymd
    glob_pat = GLOB_PATTERNS.get(table, f'{table}_????_*.csv')
    pattern = str(csv_dir / glob_pat)
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f'CSV 파일 없음: {pattern}')

    logger.info(f'[load_csv] table={table}, 파일={len(files)}개, 경로={csv_dir}')

    cols = COPY_HEADERS[table]
    force_null = FORCE_NULL_COLS.get(table, '')
    force_null_clause = f', FORCE_NULL({force_null})' if force_null else ''
    copy_sql = (
        f"COPY {table}({cols}) FROM STDIN WITH "
        f"(FORMAT csv, HEADER true, ENCODING 'UTF8'{force_null_clause})"
    )

    conn = _connect(db_cfg)
    start = time.time()
    total_rows = 0

    try:
        conn.autocommit = False
        cur = conn.cursor()

        cur.execute(f'SELECT COUNT(*) FROM {table}')
        existing = cur.fetchone()[0]
        logger.info(f'[load_csv] 기존 행 수: {existing:,}건')
        if existing > 0:
            logger.info(f'[load_csv] TRUNCATE {table} 실행...')
            cur.execute(f'TRUNCATE {table}')

        for i, fpath in enumerate(files, 1):
            fname = Path(fpath).name
            logger.info(f'[load_csv] ({i}/{len(files)}) {fname} COPY 중...')
            with open(fpath, 'rb') as f:
                cur.copy_expert(copy_sql, f)
            rows = cur.rowcount
            total_rows += rows
            logger.info(f'[load_csv]  → {rows:,}건 적재 (누계 {total_rows:,}건)')

        conn.commit()
        elapsed = fmt_elapsed(time.time() - start)
        logger.info(f'[완료] {table} | 적재: {total_rows:,}건 | 파일: {len(files)}개 | 소요: {elapsed}')

    except Exception as e:
        conn.rollback()
        elapsed = fmt_elapsed(time.time() - start)
        logger.error(f'[ROLLBACK] {table} 적재 실패 | 소요: {elapsed} | 원인: {e}')
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='CSV → PostgreSQL 적재')
    parser.add_argument('table', choices=sorted(COPY_HEADERS.keys()))
    parser.add_argument('env', choices=['dev', 'stg', 'law', 'prod'])
    parser.add_argument('ymd', help='날짜 서브디렉토리 (예: 20260608)')
    parser.add_argument('--dir', help='CSV 루트 디렉토리 (기본: config의 csv_path)')
    args = parser.parse_args()

    cfg = load_config(args.env)
    try:
        run(args.table, cfg, args.ymd, args.dir)
    except Exception as e:
        print(f'\n[오류] {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

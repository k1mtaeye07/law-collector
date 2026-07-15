"""
CSV → PostgreSQL 적재 (staging UPSERT + content_hash 기반 변경 감지)

사용법:
  python load_csv.py <table> <env> <ymd> [--dir CSV경로]
  python load_csv.py law_hang_con law 20260608

동작:
  1. {csv_path}/{ymd}/{table}_????_*.csv 파일 수집
  2. 단일 트랜잭션 내:
     - 임시 staging 테이블 생성 (LIKE {table})
     - 모든 CSV → staging COPY
     - staging → production UPSERT
       - 신규 행: INSERT (content_hash, modify_dt=NOW() 포함)
       - 기존 행: content_hash 변경 시 UPDATE + modify_dt=NOW()
                  content_hash 동일 시 modify_dt 유지 (HVM 대상 제외)
     - law_list: eff_end 재계산 (개정 추가 시 이전 버전 eff_end 갱신)
     - 전체 성공 시 COMMIT, 실패 시 ROLLBACK
  3. 완료 요약 + 로그 파일 기록
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
    'auth_int':     'SRNO,TITL,DOC_NO,DCSN_YMD,INSTN,CTXT,DOC_KND,DATA_YMD,RLTD_LAW',
    'de_case':      'INSTN_DCSNST_SRNO,DCSNST_SRNO,INSTN,CS_NO,CS_NM,DOC_KND,DCSN_YMD,CTXT,DATA_YMD',
}

FORCE_NULL_COLS = {
    'law_list':     'law_srno,law_id,entrvs_dvs_cd,prmlgt_ymd,prmlgt_no',
    'law_con':      'law_srno,law_id,prmlgt_ymd,prmlgt_no',
    'law_jo_con':   'law_srno,law_id,prmlgt_ymd,prmlgt_no',
    'law_hang_con': 'law_srno,law_id,prmlgt_ymd,prmlgt_no',
    'auth_int':     'dcsn_ymd,data_ymd',
    'de_case':      'dcsn_ymd,data_ymd',
}

GLOB_PATTERNS = {
    'auth_int': 'auth_int_*_????_*.csv',
    'de_case':  'de_case_*_????_*.csv',
}

# ON CONFLICT 대상 PK (migrate_v2.sql의 제약과 일치해야 함)
TABLE_PKS = {
    'law_list':     'dockey',
    'law_con':      'dockey',
    'law_jo_con':   'dockey',
    'law_hang_con': 'dockey',
    'auth_int':     'srno, doc_knd',
    'de_case':      'instn_dcsnst_srno',
}

# content_hash 계산 대상 컬럼 (변경 감지 기준)
HASH_EXPRS = {
    'law_list':     "coalesce(crnt_law_nm, '')",
    'law_con':      "coalesce(ctxt, '') || coalesce(jomun_dvs_nm, '')",
    'law_jo_con':   "coalesce(ctxt, '') || coalesce(title, '') || coalesce(jomun_chg_yn, '')",
    'law_hang_con': "coalesce(ctxt, '') || coalesce(title, '') || coalesce(jomun_chg_yn, '')",
    'auth_int':     "coalesce(ctxt, '') || coalesce(titl, '') || coalesce(rltd_law, '')",
    'de_case':      "coalesce(ctxt, '') || coalesce(cs_nm, '')",
}

# law_list: 개정 추가 시 이전 버전의 eff_end 및 modify_dt 갱신
_EFF_END_SQL = """
UPDATE law_list AS t
SET
    eff_end   = sub.new_eff_end,
    modify_dt = CASE
        WHEN t.eff_end IS DISTINCT FROM sub.new_eff_end THEN NOW()
        ELSE t.modify_dt
    END
FROM (
    SELECT
        dockey,
        to_char(
            CASE
                WHEN LEAD(enfc_ymd) OVER w IS NOT NULL
                     THEN LEAD(enfc_ymd) OVER w
                WHEN crnt_law_nm = '현행' THEN DATE '9999-12-31'
                ELSE enfc_ymd
            END, 'YYYYMMDD'
        ) AS new_eff_end
    FROM law_list
    WINDOW w AS (PARTITION BY law_id ORDER BY enfc_ymd, prmlgt_no, entrvs_dvs_cd)
) sub
WHERE t.dockey = sub.dockey
  AND t.eff_end IS DISTINCT FROM sub.new_eff_end
"""


def _build_upsert_sql(table: str) -> str:
    pk        = TABLE_PKS[table]
    pk_cols   = {c.strip() for c in pk.split(',')}
    cols      = [c.lower() for c in COPY_HEADERS[table].split(',')]
    non_pk    = [c for c in cols if c not in pk_cols]
    hash_expr = HASH_EXPRS[table]

    set_clause = ',\n    '.join(f'{c} = EXCLUDED.{c}' for c in non_pk)

    return f"""
INSERT INTO {table} ({', '.join(cols)}, content_hash, modify_dt)
SELECT {', '.join(cols)},
       md5({hash_expr}) AS content_hash,
       NOW()            AS modify_dt
FROM {table}_stg
ON CONFLICT ({pk}) DO UPDATE SET
    {set_clause},
    content_hash = EXCLUDED.content_hash,
    modify_dt    = CASE
        WHEN {table}.content_hash IS DISTINCT FROM EXCLUDED.content_hash
        THEN NOW()
        ELSE {table}.modify_dt
    END
"""


def setup_logger(log_dir: Path, task: str = '') -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    dt = datetime.now().strftime('%Y%m%d_%H%M%S')
    prefix = f'{task}_' if task else ''
    log_file = log_dir / f'load_{prefix}{dt}.log'

    logger_name = f'load.{task}.{dt}' if task else f'load.{dt}'
    logger = logging.getLogger(logger_name)
    fmt = logging.Formatter('[%(asctime)s] %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S')
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.setLevel(logging.INFO)
    return logger


def _connect(db_cfg: dict):
    return psycopg2.connect(
        host=db_cfg.get('host', 'localhost'),
        port=int(db_cfg.get('port', 5432)),
        dbname=db_cfg['dbname'],
        user=db_cfg['user'],
        password=db_cfg.get('password', ''),
    )


def run(table: str, cfg: dict, ymd: str, csv_dir_override: str = None, logger=None):
    """단일 테이블 적재. main.py에서도 호출 가능."""
    db_cfg = cfg.get('db')
    if not db_cfg:
        raise RuntimeError('db 설정 없음')

    log_dir = Path(cfg['logs_path']) / ymd
    if logger is None:
        logger = setup_logger(log_dir, task=table)

    csv_dir  = Path(csv_dir_override or cfg['csv_path']) / ymd
    glob_pat = GLOB_PATTERNS.get(table, f'{table}_????_*.csv')
    pattern  = str(csv_dir / glob_pat)
    files    = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f'CSV 파일 없음: {pattern}')

    logger.info(f'[load_csv] table={table}, 파일={len(files)}개, 경로={csv_dir}')

    cols          = COPY_HEADERS[table]
    force_null    = FORCE_NULL_COLS.get(table, '')
    fn_clause     = f', FORCE_NULL({force_null})' if force_null else ''
    copy_stg_sql  = (
        f"COPY {table}_stg({cols}) FROM STDIN WITH "
        f"(FORMAT csv, HEADER true, ENCODING 'UTF8'{fn_clause})"
    )
    upsert_sql = _build_upsert_sql(table)

    conn = _connect(db_cfg)
    start = time.time()
    stg_rows = 0

    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '30min'")

        cur.execute(f'SELECT COUNT(*) FROM {table}')
        existing = cur.fetchone()[0]
        logger.info(f'[load_csv] 기존 행 수: {existing:,}건')

        # 1. 임시 staging 테이블 생성 (COMMIT 시 자동 삭제)
        cur.execute(
            f"CREATE TEMP TABLE {table}_stg "
            f"(LIKE {table} EXCLUDING CONSTRAINTS EXCLUDING INDEXES) ON COMMIT DROP"
        )

        # 2. CSV → staging COPY
        for i, fpath in enumerate(files, 1):
            fname = Path(fpath).name
            logger.info(f'[load_csv] ({i}/{len(files)}) {fname} staging 중...')
            with open(fpath, 'rb') as f:
                cur.copy_expert(copy_stg_sql, f)
            rows = cur.rowcount
            stg_rows += rows
            logger.info(f'[load_csv]  → {rows:,}건 (누계 {stg_rows:,}건)')

        logger.info(f'[load_csv] staging 완료: {stg_rows:,}건 → UPSERT 시작')

        # 3. staging → production UPSERT
        cur.execute(upsert_sql)
        upserted = cur.rowcount
        logger.info(f'[load_csv] UPSERT 완료: {upserted:,}건 (신규+변경)')

        # 4. law_list: eff_end 갱신 (개정 추가 시 이전 버전 eff_end·modify_dt 업데이트)
        if table == 'law_list':
            cur.execute(_EFF_END_SQL)
            eff_rows = cur.rowcount
            logger.info(f'[load_csv] eff_end 갱신: {eff_rows:,}건')

        conn.commit()
        elapsed = fmt_elapsed(time.time() - start)
        logger.info(f'[완료] {table} | UPSERT: {upserted:,}건 | 파일: {len(files)}개 | 소요: {elapsed}')

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
    parser.add_argument('env',   choices=['dev', 'stg', 'law', 'prod'])
    parser.add_argument('ymd',   help='날짜 서브디렉토리 (예: 20260608)')
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

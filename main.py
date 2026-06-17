"""
법령 OpenAPI 수집기

사용법:
  python main.py law_list    <env>  [--concurrency N]
  python main.py law_content <env>  [--concurrency N]
  python main.py load        <env>
  python main.py all         <env>  [--concurrency N]

env: dev | stg | law | prod
"""
import argparse
import asyncio
import sys
import time
from datetime import date

from common.config import load_config
from common.logger import JobLogger
from common.utils import fmt_elapsed


def cmd_law_list(args):
    cfg = load_config(args.env)
    if args.concurrency:
        cfg['concurrency'] = args.concurrency

    ymd = date.today().strftime('%Y%m%d')
    logger = JobLogger(cfg['logs_path'], ymd)

    from crawl.law_list import run
    start = time.time()
    run(cfg, ymd, logger)
    logger.info(f'[law_list] 총 소요: {fmt_elapsed(time.time() - start)}')


def cmd_auth_int(args):
    cfg = load_config(args.env)
    if args.concurrency:
        cfg['concurrency'] = args.concurrency

    ymd = date.today().strftime('%Y%m%d')
    logger = JobLogger(cfg['logs_path'], ymd)

    from crawl.auth_int import run
    asyncio.run(run(cfg, ymd, logger,
                    test_urls=getattr(args, 'test_urls', None),
                    target=getattr(args, 'target', None)))


def cmd_de_case(args):
    cfg = load_config(args.env)
    if args.concurrency:
        cfg['concurrency'] = args.concurrency

    ymd = date.today().strftime('%Y%m%d')
    logger = JobLogger(cfg['logs_path'], ymd)

    from crawl.de_case import run
    asyncio.run(run(cfg, ymd, logger, mode=args.mode,
                    test_urls=getattr(args, 'test_urls', None),
                    target=getattr(args, 'target', None)))


def cmd_load(args):
    cfg = load_config(args.env)
    ymd = date.today().strftime('%Y%m%d')

    from pathlib import Path
    from load_csv import run, COPY_HEADERS, setup_logger
    logger = setup_logger(Path(cfg['logs_path']) / ymd)

    for table in COPY_HEADERS.keys():
        try:
            run(table, cfg, ymd, logger=logger)
        except FileNotFoundError as e:
            logger.warning(f'[load] {table} CSV 없음, 건너뜀: {e}')


def cmd_all(args):
    from pathlib import Path
    from load_csv import run as load_run, setup_logger
    import crawl.law_list as m_law_list
    import crawl.law_content as m_law_content
    import crawl.auth_int as m_auth_int
    import crawl.de_case as m_de_case

    cfg = load_config(args.env)
    if args.concurrency:
        cfg['concurrency'] = args.concurrency

    ymd = date.today().strftime('%Y%m%d')
    logger = JobLogger(cfg['logs_path'], ymd)
    load_logger = setup_logger(Path(cfg['logs_path']) / ymd)

    def _load(*tables):
        for table in tables:
            try:
                load_run(table, cfg, ymd, logger=load_logger)
            except FileNotFoundError as e:
                load_logger.warning(f'[load] {table} CSV 없음, 건너뜀: {e}')
            except Exception as e:
                load_logger.error(f'[load] {table} 적재 실패: {e}')

    m_law_list.run(cfg, ymd, logger)
    _load('law_list')

    asyncio.run(m_law_content.run(cfg, ymd, logger))
    _load('law_con', 'law_jo_con', 'law_hang_con')

    asyncio.run(m_auth_int.run(cfg, ymd, logger))
    _load('auth_int')

    asyncio.run(m_de_case.run(cfg, ymd, logger))
    _load('de_case')


def cmd_law_content(args):
    cfg = load_config(args.env)
    if args.concurrency:
        cfg['concurrency'] = args.concurrency

    ymd = date.today().strftime('%Y%m%d')
    logger = JobLogger(cfg['logs_path'], ymd)

    from crawl.law_content import run
    asyncio.run(run(
        cfg, ymd, logger,
        test_urls=getattr(args, 'test_urls', None),
    ))


def main():
    parser = argparse.ArgumentParser(description='법령 OpenAPI 수집기')
    sub = parser.add_subparsers(dest='command', required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('env', choices=['dev', 'stg', 'law', 'prod'])
    common.add_argument('--concurrency', type=int, help='동시연결 수 (기본: config.yml)')

    sub.add_parser('law_list', parents=[common], help='법령 목록 수집')

    p_content = sub.add_parser('law_content', parents=[common], help='법령 본문 수집 (law_con/law_jo_con/law_hang_con)')
    p_content.add_argument('--test-urls', nargs='+', metavar='URL', dest='test_urls',
                           help='특정 URL 지정 수집 (테스트용)')

    p_authint = sub.add_parser('auth_int', parents=[common], help='유권해석 수집 (auth_int)')
    p_authint.add_argument('--test-urls', nargs='+', metavar='URL', dest='test_urls',
                           help='특정 URL 지정 수집 (테스트용)')
    p_authint.add_argument('--target', metavar='TARGET', dest='target',
                           help='테스트 시 사용할 target 코드 (기본: moelCgmExpc)')

    p_decase = sub.add_parser('de_case', parents=[common], help='결정례 수집 (de_case_*)')
    p_decase.add_argument('--mode', choices=['all', 'insert'], default='all',
                          help='all: 전체 수집 | insert: 전일/당일 신규 수집 (기본: all)')
    p_decase.add_argument('--test-urls', nargs='+', metavar='URL', dest='test_urls',
                          help='특정 URL 지정 수집 (테스트용)')
    p_decase.add_argument('--target', metavar='TARGET', dest='target',
                          help='테스트 시 사용할 target 코드 (예: ppc, ftc, decc 등)')

    sub.add_parser('load', parents=[common], help='CSV → DB 전체 적재 (6개 테이블)')
    sub.add_parser('all',  parents=[common], help='각 크롤러 완료 즉시 load (law_list→law_content→auth_int→de_case)')

    args = parser.parse_args()

    if args.command == 'law_list':
        cmd_law_list(args)
    elif args.command == 'law_content':
        cmd_law_content(args)
    elif args.command == 'auth_int':
        cmd_auth_int(args)
    elif args.command == 'de_case':
        cmd_de_case(args)
    elif args.command == 'load':
        cmd_load(args)
    elif args.command == 'all':
        cmd_all(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()

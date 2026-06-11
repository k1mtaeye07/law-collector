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


def cmd_load(args):
    cfg = load_config(args.env)
    ymd = date.today().strftime('%Y%m%d')

    from pathlib import Path
    from load_csv import run, COPY_HEADERS, setup_logger
    logger = setup_logger(Path(cfg['logs_path']) / ymd)

    for table in COPY_HEADERS.keys():
        run(table, cfg, ymd, logger=logger)


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
    sub.add_parser('load', parents=[common], help='CSV → DB 전체 적재 (4개 테이블)')
    sub.add_parser('all',  parents=[common], help='law_list + law_content + load 순차 실행')

    args = parser.parse_args()

    if args.command == 'law_list':
        cmd_law_list(args)
    elif args.command == 'law_content':
        cmd_law_content(args)
    elif args.command == 'load':
        cmd_load(args)
    elif args.command == 'all':
        cmd_law_list(args)
        cmd_law_content(args)
        cmd_load(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()

"""
requirements_check.py - 서버 실행 전 환경 자가 점검

사용법:
  python requirements_check.py [env]
  python3 requirements_check.py          # 패키지만 확인
  python3 requirements_check.py dev      # dev 환경 전체 점검
  python3 requirements_check.py prod     # prod 환경 + DB 연결 테스트 포함

env 미지정 시 패키지/Python 버전만 확인 (config 경로 점검 생략)
"""
import importlib
import platform
import sys


# ── 점검 결과 출력 헬퍼 ──────────────────────────────────────────────────────
_OK   = '[  OK  ]'
_FAIL = '[ FAIL ]'
_WARN = '[ WARN ]'
_fail_count = 0
_warn_count = 0


def ok(msg):
    print(f'{_OK}  {msg}')


def fail(msg):
    global _fail_count
    _fail_count += 1
    print(f'{_FAIL}  {msg}')


def warn(msg):
    global _warn_count
    _warn_count += 1
    print(f'{_WARN}  {msg}')


def check_import(pkg_name, display_name=None, required=True):
    label = display_name or pkg_name
    try:
        mod = importlib.import_module(pkg_name)
        ver = getattr(mod, '__version__', '버전미확인')
        ok(f'{label} ({ver})')
        return True
    except ImportError:
        (fail if required else warn)(
            f'{label} 미설치'
            + ('' if required else ' (선택사항 — 없어도 동작함)')
        )
        return False


# ── 1. Python 버전 ──────────────────────────────────────────────────────────
def check_python():
    print('\n=== Python 버전 ===')
    vi = sys.version_info
    ver_str = f'{vi.major}.{vi.minor}.{vi.micro}'
    if vi.major == 3 and vi.minor >= 9:
        ok(f'Python {ver_str}  ({platform.python_implementation()})')
    else:
        fail(f'Python {ver_str} — 3.9 이상 필요')


# ── 2. 반입 패키지 (수동 설치 필수) ─────────────────────────────────────────
def check_required_packages():
    print('\n=== 반입 패키지 (수동 설치 필수) ===')
    check_import('httpx',    'httpx')
    check_import('httpcore', 'httpcore')
    check_import('h11',      'h11')
    check_import('sniffio',  'sniffio')
    check_import('anyio',    'anyio')
    check_import('tenacity', 'tenacity')
    check_import('tqdm',     'tqdm')


# ── 3. 기본 제공 패키지 ──────────────────────────────────────────────────────
def check_stdlib_packages():
    print('\n=== 기본 제공 패키지 ===')
    for pkg in ['yaml', 'requests', 'urllib3', 'asyncio', 'csv',
                'queue', 'threading', 'pathlib', 'xml.etree.ElementTree']:
        check_import(pkg, required=True)


# ── 4. 선택 패키지 ───────────────────────────────────────────────────────────
def check_optional_packages():
    print('\n=== 선택 패키지 ===')
    check_import('lxml',     'lxml     (없으면 xml.etree 대체)', required=False)
    check_import('psycopg2', 'psycopg2 (없으면 load_csv.py 불가)', required=False)


# ── 5. config.yml 및 경로 점검 ───────────────────────────────────────────────
def check_config(env: str):
    print(f'\n=== config.yml 및 경로 점검 (env={env}) ===')
    from pathlib import Path
    import os

    config_path = Path(__file__).parent / 'config.yml'
    if not config_path.exists():
        fail(f'config.yml 없음: {config_path}')
        return

    try:
        import yaml
        with open(config_path, encoding='utf-8') as f:
            full = yaml.safe_load(f)
        ok(f'config.yml 로드 성공')
    except Exception as e:
        fail(f'config.yml 파싱 오류: {e}')
        return

    env_cfg = full.get(env)
    if not env_cfg:
        fail(f"환경 '{env}' 없음 (사용 가능: {list(full.keys())})")
        return
    ok(f"환경 '{env}' 설정 확인")

    # 필수 키 확인
    for key in ('api_list_url', 'api_con_path', 'csv_path', 'logs_path'):
        val = env_cfg.get(key)
        if not val:
            fail(f'{key} 미설정')
        else:
            ok(f'{key} = {val}')

    # 디렉토리 쓰기 권한 확인
    for dir_key in ('csv_path', 'logs_path'):
        dir_path = Path(env_cfg.get(dir_key, ''))
        if not dir_path.exists():
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
                ok(f'{dir_key} 디렉토리 생성됨: {dir_path}')
            except Exception as e:
                fail(f'{dir_key} 디렉토리 생성 실패: {dir_path} → {e}')
        elif os.access(dir_path, os.W_OK):
            ok(f'{dir_key} 쓰기 가능: {dir_path}')
        else:
            fail(f'{dir_key} 쓰기 권한 없음: {dir_path}')

    # DB 설정 확인 (prod 환경)
    db_cfg = env_cfg.get('db')
    if db_cfg:
        print(f'\n=== DB 설정 점검 (env={env}) ===')
        for key in ('host', 'port', 'dbname', 'user', 'password'):
            val = db_cfg.get(key)
            if not val:
                fail(f'db.{key} 미설정')
            elif key == 'password' and val == 'CHANGEME':
                warn('db.password 가 기본값(CHANGEME) — 실제 비밀번호로 변경 필요')
            else:
                masked = '****' if key == 'password' else val
                ok(f'db.{key} = {masked}')

        # DB 연결 테스트
        try:
            import psycopg2
            conn = psycopg2.connect(
                host=db_cfg['host'], port=db_cfg.get('port', 5432),
                dbname=db_cfg['dbname'], user=db_cfg['user'],
                password=db_cfg['password'], connect_timeout=5,
            )
            conn.close()
            ok('PostgreSQL 연결 성공')
        except ImportError:
            warn('psycopg2 미설치 → DB 연결 테스트 생략')
        except Exception as e:
            fail(f'PostgreSQL 연결 실패: {e}')


# ── 6. API 연결 테스트 (선택적) ──────────────────────────────────────────────
def check_api(env: str):
    print(f'\n=== API 연결 테스트 (env={env}) ===')
    from pathlib import Path
    try:
        import yaml, requests, urllib3
        urllib3.disable_warnings()
        config_path = Path(__file__).parent / 'config.yml'
        with open(config_path, encoding='utf-8') as f:
            full = yaml.safe_load(f)
        env_cfg = full.get(env, {})
        list_url = env_cfg.get('api_list_url', '')
        if not list_url:
            warn('api_list_url 미설정 → API 테스트 생략')
            return

        # 목록 API 1건 호출
        test_url = f'{list_url}eflaw&nw=1,3&page=1&display=1'
        resp = requests.get(test_url, verify=False, timeout=10)
        if resp.status_code == 200 and not resp.text.lstrip().startswith('<html'):
            ok(f'목록 API 응답 정상 (HTTP {resp.status_code})')
        else:
            fail(f'목록 API 응답 이상 (HTTP {resp.status_code})')
    except Exception as e:
        fail(f'API 연결 실패: {e}')


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    env = sys.argv[1] if len(sys.argv) > 1 else None

    print('=' * 55)
    print(' 법령 수집기 환경 점검')
    if env:
        print(f' 대상 환경: {env}')
    print('=' * 55)

    check_python()
    check_required_packages()
    check_stdlib_packages()
    check_optional_packages()

    if env:
        check_config(env)
        check_api(env)

    print('\n' + '=' * 55)
    if _fail_count == 0 and _warn_count == 0:
        print(f'  결과: 전체 통과')
    elif _fail_count == 0:
        print(f'  결과: 통과 (경고 {_warn_count}건 — 동작에는 문제 없음)')
    else:
        print(f'  결과: 실패 {_fail_count}건 / 경고 {_warn_count}건')
        print(f'  → FAIL 항목 해결 후 재실행하세요.')
    print('=' * 55)

    sys.exit(1 if _fail_count > 0 else 0)


if __name__ == '__main__':
    main()

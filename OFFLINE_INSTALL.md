# law-collector 폐쇄망 수동 설치 가이드

> 대법원 서버는 인터넷 차단 환경입니다.  
> 외부 PC에서 패키지를 미리 다운로드한 후, 승인된 반입 절차를 통해 설치합니다.

---

## 1. 서버별 설치 대상 파악

법령 수집기가 필요로 하는 외부 패키지 현황입니다.

| 패키지 | 역할 | 개발(3.10) | 검증(3.9) | 운영(3.9) |
|---|---|:---:|:---:|:---:|
| httpx | 비동기 HTTP 클라이언트 | ❌ 필요 | ❌ 필요 | ❌ 필요 |
| httpcore | httpx 내부 의존성 | ❌ 필요 | ❌ 필요 | ❌ 필요 |
| h11 | HTTP/1.1 구현체 | ❌ 필요 | ❌ 필요 | ❌ 필요 |
| sniffio | async 라이브러리 감지 | ❌ 필요 | ❌ 필요 | ❌ 필요 |
| anyio | 비동기 백엔드 추상화 | ❌ 필요 | ❌ 필요 | ❌ 필요 |
| exceptiongroup | anyio 의존성 (Python<3.11) | ❌ 필요 | ❌ 필요 | ❌ 필요 |
| tenacity | 재시도 로직 | ❌ 필요 | ❌ 필요 | ❌ 필요 |
| tqdm | 진행률 표시 | ❌ 필요 | ❌ 필요 | ❌ 필요 |
| lxml | 고속 XML 파서 (선택) | ❌ 필요 | ✅ 기설치 | ✅ 기설치 |
| psycopg2-binary | PostgreSQL 어댑터 | ❌ 필요 | ❌ 필요 | ✅ 기설치 |

> **선택 패키지(lxml, psycopg2):**  
> - `lxml` 없으면 xml.etree로 자동 대체됩니다 (동작은 하지만 느림)  
> - `psycopg2` 없으면 `load_csv.py` 실행 불가 (적재 단계에서 필수)

---

## 2. 인터넷 환경 PC에서 파일 다운로드

> **전제:** 인터넷이 되는 PC에 Python과 pip이 설치되어 있어야 합니다.  
> OS는 Windows, macOS, Linux 무관합니다.

### 2-1. 다운로드 디렉토리 준비

> **주의:** 아래 모든 명령은 `offline_packages/` 디렉토리를 만든 위치(상위 디렉토리)에서 실행합니다.

```bash
mkdir -p offline_packages/py310   # 개발서버용 (Python 3.10)
mkdir -p offline_packages/py39    # 검증/운영서버용 (Python 3.9)
cd offline_packages               # 이후 명령은 여기서 실행
```

### 2-2. 개발서버 pip 부트스트랩 파일 다운로드

개발서버(Ubuntu)는 `ensurepip`이 없습니다. pip 설치 여부를 확인 후, 없으면 `get-pip.py`도 함께 반입합니다.

```bash
# offline_packages/ 안에서 실행
curl -O https://bootstrap.pypa.io/get-pip.py
```

> `get-pip.py`에는 pip이 내장되어 있어 인터넷 없이 실행 가능합니다.

---

### 2-3. 개발서버용 패키지 다운로드 (Python 3.10 / Linux x86_64)

```bash
pip download \
  httpx httpcore h11 sniffio anyio exceptiongroup tenacity tqdm lxml psycopg2-binary \
  --dest ./offline_packages/py310 \
  --python-version 3.10 \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --abi cp310 \
  --only-binary :all:
```

### 2-4. 검증/운영서버용 패키지 다운로드 (Python 3.9 / Linux x86_64)

```bash
pip download \
  httpx httpcore h11 sniffio anyio exceptiongroup tenacity tqdm lxml psycopg2-binary \
  --dest ./offline_packages/py39 \
  --python-version 3.9 \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --abi cp39 \
  --only-binary :all:
```

### 2-5. 다운로드 결과 확인

```bash
ls offline_packages/py310/
ls offline_packages/py39/
```

각 디렉토리에 아래와 유사한 `.whl` 파일이 생성되면 정상입니다.

```
anyio-4.x.x-py3-none-any.whl
exceptiongroup-1.x.x-py3-none-any.whl
h11-0.x.x-py3-none-any.whl
httpcore-1.x.x-py3-none-any.whl
httpx-0.x.x-py3-none-any.whl
lxml-5.x.x-cp310-cp310-manylinux2014_x86_64.whl   ← 버전명은 달라도 됨
psycopg2_binary-2.x.x-cp310-cp310-manylinux2014_x86_64.whl
sniffio-1.x.x-py3-none-any.whl
tenacity-x.x.x-py3-none-any.whl
tqdm-4.x.x-py3-none-any.whl
```

> **주의:** `.tar.gz` 파일이 섞여 있으면 해당 패키지는 바이너리 배포가 없는 것입니다.  
> 이 경우 `--no-binary :none:` 없이 재시도하거나 다른 버전을 지정하세요.

### 2-6. 압축

```bash
# offline_packages/ 의 상위 디렉토리에서 실행
tar czf offline_packages.tar.gz offline_packages/
```

---

## 3. 반입 파일

| 단계 | 내용 |
|---|---|
| ① 패키지 파일 준비 | `offline_packages.tar.gz` 생성 (위 2-6 단계) |
| ② 전체 코드 파일 준비 | `law-collector.tar.gz` 생성 |

---

## 4. 서버별 설치

### 공통 준비

```bash
# 서버에서 압축 해제 (홈 디렉토리 기준)
tar xzf offline_packages.tar.gz -C ~/
```

---

### 4-1. 개발서버 (Python 3.10, Ubuntu)

**pip 확인 먼저:**

```bash
python3 -m pip --version
```

없으면 get-pip.py로 설치:

```bash
python3 ~/offline_packages/get-pip.py
```

**패키지 설치:**

```bash
pip install --no-index --find-links ~/offline_packages/py310 \
  httpx httpcore h11 sniffio anyio exceptiongroup tenacity tqdm lxml psycopg2-binary
```

---

### 4-2. 검증서버 (Python 3.9, RHEL)

`ensurepip` 내장 → pip 없을 경우:

```bash
python3 -m ensurepip --upgrade
```

설치 대상: httpx, httpcore, h11, sniffio, anyio, exceptiongroup, tenacity, tqdm, psycopg2-binary  
(lxml은 기설치 → 목록에서 제외)

```bash
pip install --no-index --find-links ~/offline_packages/py39 \
  httpx httpcore h11 sniffio anyio exceptiongroup tenacity tqdm psycopg2-binary
```

---

### 4-3. 운영서버 (Python 3.9, RHEL)

`ensurepip` 내장 → pip 없을 경우:

```bash
python3 -m ensurepip --upgrade
```

설치 대상: httpx, httpcore, h11, sniffio, anyio, exceptiongroup, tenacity, tqdm  
(lxml, psycopg2는 기설치 → 목록에서 제외)

```bash
pip install --no-index --find-links ~/offline_packages/py39 \
  httpx httpcore h11 sniffio anyio exceptiongroup tenacity tqdm
```

---

## 5. 설치 검증

프로젝트에 포함된 `requirements_check.py`로 환경을 자가 점검합니다.

```bash
cd /path/to/law-collector

# 패키지만 확인 (env 미지정)
python3 requirements_check.py

# 환경 설정 + DB 연결까지 전체 점검
python3 requirements_check.py dev    # 개발서버
python3 requirements_check.py stg    # 검증서버
python3 requirements_check.py law    # 운영서버
```

### 정상 출력 예시

```
=======================================================
 법령 수집기 환경 점검
=======================================================

=== Python 버전 ===
[  OK  ]  Python 3.10.12  (CPython)

=== 반입 패키지 (수동 설치 필수) ===
[  OK  ]  httpx (0.27.x)
[  OK  ]  httpcore (1.x.x)
[  OK  ]  h11 (0.x.x)
[  OK  ]  sniffio (1.x.x)
[  OK  ]  anyio (4.x.x)
[  OK  ]  tenacity (9.x.x)
[  OK  ]  tqdm (4.x.x)

=== 기본 제공 패키지 ===
[  OK  ]  yaml (6.x.x)
...

=======================================================
  결과: 전체 통과
=======================================================
```

`[ FAIL ]` 항목이 있으면 해당 패키지 `.whl` 파일을 재확인한 후 재설치합니다.

---

## 6. 문제 해결

### 플랫폼 불일치 오류

```
ERROR: ... is not a supported wheel on this platform
```

→ 다운로드 시 `--platform` 태그가 서버 환경과 다를 때 발생합니다.  
서버에서 아래 명령으로 지원 태그를 확인 후 다시 다운로드하세요.

```bash
python3 -c "import pip; from pip._internal.utils.compatibility_tags import get_supported; [print(t) for t in get_supported()[:10]]"
```

### pip 버전 오류

```
ERROR: --python-version ... requires --only-binary
```

→ 인터넷 PC의 pip을 업그레이드한 후 재시도합니다.

```bash
pip install --upgrade pip
```

### 개별 패키지 재설치

특정 패키지만 설치 실패한 경우:

```bash
pip install --no-index --find-links ~/offline_packages/py39 httpx
```

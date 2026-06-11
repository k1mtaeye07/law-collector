# law-collector 아키텍처 및 코드 흐름

## 목차
1. [프로그램 목적](#1-프로그램-목적)
2. [디렉토리 구조](#2-디렉토리-구조)
3. [전체 파이프라인 흐름](#3-전체-파이프라인-흐름)
4. [단계별 상세 흐름](#4-단계별-상세-흐름)
   - [STEP 1 — 법령 목록 수집 (law_list)](#step-1--법령-목록-수집-law_list)
   - [STEP 2 — 법령 본문 수집 (law_content)](#step-2--법령-본문-수집-law_content)
   - [STEP 3 — DB 적재 (load)](#step-3--db-적재-load)
5. [공통 모듈 설명](#5-공통-모듈-설명)
6. [설정 파일 구조](#6-설정-파일-구조)
7. [로그 파일 구조](#7-로그-파일-구조)
8. [CSV 파일 구조](#8-csv-파일-구조)
9. [실행 방법](#9-실행-방법)
10. [주요 설계 결정](#10-주요-설계-결정)

---

## 1. 프로그램 목적

법령정보원 Open API(DRF)에서 법령 데이터를 수집하여 PostgreSQL에 적재합니다.

```
법령정보원 API  →  CSV 파일  →  PostgreSQL DB
```

수집 대상은 약 **166,000건** 이상의 법령이며, 목록 수집 → 본문 수집 → DB 적재 순으로 실행합니다.

---

## 2. 디렉토리 구조

```
law-collector/
│
├── main.py                  # CLI 진입점 (모든 명령어 처리)
├── load_csv.py              # CSV → PostgreSQL 적재
├── config.yml               # 환경별 설정 (dev/stg/law/prod)
├── requirements_check.py    # 서버 환경 점검 스크립트
│
├── crawl/
│   ├── law_list.py          # STEP 1: 법령 목록 수집
│   └── law_content.py       # STEP 2: 법령 본문 수집 (비동기)
│
└── common/
    ├── config.py            # config.yml 로드
    ├── logger.py            # 로그 파일 관리
    ├── http_client.py       # HTTP 요청 + 재시도 정책
    ├── xml_parser.py        # XML 파싱 유틸
    ├── csv_writer.py        # CSV 파일 기록 (백그라운드 스레드)
    └── utils.py             # 공통 유틸 (시간 포맷 등)
```

### 실행 시 생성되는 디렉토리

```
csv/
└── 20260610/                # --vol 값 (기본: 오늘 날짜)
    ├── law_list_0001_20260610.csv
    ├── law_con_0001_20260610.csv
    ├── law_jo_con_0001_20260610.csv
    └── law_hang_con_0001_20260610.csv

logs/
└── 20260610/
    ├── crawl_20260610_120000.log    # 진행 상황, 오류 요약
    ├── link_20260610_120000.log     # URL별 실패 상세
    └── final_fail_20260610_120000.log  # 최종 실패 URL 목록
```

---

## 3. 전체 파이프라인 흐름

```
┌─────────────────────────────────────────────────────────────────┐
│                         main.py                                 │
│  python main.py all law                                         │
└───────────┬─────────────────────────────────────────────────────┘
            │
            ▼
┌───────────────────────┐
│  STEP 1: law_list     │  법령 목록 API 호출 (동기, requests)
│  crawl/law_list.py    │  → 166,267건의 URL 목록 수집
└───────────┬───────────┘
            │  law_list_0001_20260610.csv 생성
            ▼
┌───────────────────────┐
│  STEP 2: law_content  │  URL별 XML 수집 (비동기, httpx × 50 worker)
│  crawl/law_content.py │  → 법령 본문 파싱 (3개 테이블로 분리)
└───────────┬───────────┘
            │  law_con / law_jo_con / law_hang_con CSV 생성
            ▼
┌───────────────────────┐
│  STEP 3: load         │  CSV → PostgreSQL COPY (단일 트랜잭션)
│  load_csv.py          │  → 4개 테이블 순차 적재
└───────────────────────┘
```

---

## 4. 단계별 상세 흐름

### STEP 1 — 법령 목록 수집 (law_list)

**실행:** `python main.py law_list law`

**목적:** API에서 전체 법령 목록(URL, 일련번호, 시행일자 등)을 수집해 CSV로 저장합니다.

```
api_list_url?page=1&display=100
        │
        ▼
   totalCnt 조회 → 총 페이지 수 계산
        │
        ▼ (페이지 반복)
   page_doc = XML 응답 파싱
        │
        ▼ (법령 항목 반복)
   법령상세링크(상대경로)  →  _adjust_url()  →  절대 URL로 변환
        │
        ▼
   writer.put([row])  →  CsvWriter 백그라운드 스레드가 CSV에 기록
        │
        ▼
   law_list_0001_20260610.csv
```

**핵심 코드 흐름 (`crawl/law_list.py`):**

```python
# 1. API base URL 한 번만 계산 (166K번 반복 방지)
parsed = urlparse(api_con_path)
con_base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

# 2. 총 건수 조회
doc = _call_api(f'{base_url}&page=1&display=100')
total_cnt = int(doc.find('.//totalCnt').text)

# 3. 페이지 순회하며 행 적재
for page in range(1, total_pages + 1):
    for law_el in page_doc.findall('.//law'):
        detail_url = _adjust_url(detail_url, con_base_url)  # 상대→절대 URL
        writer.put([dockey, law_srno, ...])
```

**출력 CSV 컬럼:**
| 컬럼 | 설명 |
|---|---|
| DOCKEY | `{LAW_SRNO}_{ENFC_YMD}` 복합키 |
| LAW_SRNO | 법령 일련번호 |
| LAW_ID | 법령 ID |
| LINK | 본문 수집에 사용할 XML API URL |

---

### STEP 2 — 법령 본문 수집 (law_content)

**실행:** `python main.py law_content law`

**목적:** STEP 1에서 수집한 URL을 비동기로 병렬 호출하여 법령 본문을 3개 테이블 구조로 파싱합니다.

#### 비동기 처리 구조

```
asyncio 이벤트 루프 (단일 스레드)
│
├── asyncio.Queue  ← 166,267개 URL이 모두 적재됨
│
├── worker-01  →  fetch_xml() → parse_xml_string() → 파싱 → CsvWriter.put()
├── worker-02  →  fetch_xml() → parse_xml_string() → 파싱 → CsvWriter.put()
│   ...             (HTTP I/O 대기 중엔 다른 worker 실행)
└── worker-50  →  fetch_xml() → parse_xml_string() → 파싱 → CsvWriter.put()
                                                              │
                                                              ▼
                                              별도 스레드 (CsvWriter × 3개)
                                              law_con_0001.csv
                                              law_jo_con_0001.csv
                                              law_hang_con_0001.csv
```

> **왜 Queue + worker 패턴?**
> `asyncio.gather(*[166K tasks])` 로 만들면 코루틴 객체 166K개가 메모리에 한꺼번에 올라갑니다.
> Queue 방식은 worker N개만 상주하면서 항목을 꺼내 처리하므로 메모리가 고정됩니다.

#### 재시도 정책

```python
@retry(
    stop=stop_after_attempt(3),           # 최대 3회 시도
    wait=wait_exponential(min=2, max=10), # 2초 → 4초 → 10초 대기
    retry=retry_if_exception_type(RETRYABLE),  # HTTP 오류 + XML 파싱 오류
)
async def _fetch_and_parse(url: str):
    return parse_xml_string(await fetch_xml(client, url))
```

`RETRYABLE` = `(RequestError, HTTPStatusError, ParseError, XMLSyntaxError)`

> **왜 XML 파싱 오류도 재시도?**
> 서버에 50개 동시 요청이 몰리면 서버가 일시적으로 응답을 잘라서(truncate) HTTP 200으로 반환합니다.
> bytes는 받지만 XML 구조가 깨져 파싱 오류가 발생합니다. 재시도하면 정상 응답을 받습니다.

#### 파싱 결과 → 3개 테이블

법령 XML 1개를 파싱하면 3개 테이블에 동시 기록됩니다:

```
XML 1개
 │
 ├── parse_law_con()      → law_con      (법령 전체 1행)
 ├── parse_law_jo_con()   → law_jo_con   (조문 단위 N행)
 └── parse_law_hang_con() → law_hang_con (항 단위 N행)
```

| 테이블 | 단위 | 설명 |
|---|---|---|
| `law_con` | 법령 1개 → 1행 | 법령 전체 본문 |
| `law_jo_con` | 법령 1개 → 조문 수만큼 N행 | 조문(제1조, 제2조...) 단위 |
| `law_hang_con` | 법령 1개 → 항 수만큼 N행 | 항(①②③...) 단위 |

---

### STEP 3 — DB 적재 (load)

**실행:** `python main.py load law`

**목적:** CSV 파일을 PostgreSQL에 COPY 명령으로 적재합니다. 4개 테이블을 순차적으로 처리합니다.

```
CSV 파일 목록 수집 (law_list_????_*.csv, law_con_????_*.csv, ...)
        │
        ▼
연결: psycopg2 → autocommit = False (트랜잭션 시작)
        │
        ▼
SELECT COUNT(*) → 기존 행 있으면 TRUNCATE (덮어쓰기 방식)
        │
        ▼ (파일 반복)
COPY {table}(컬럼...) FROM STDIN WITH (FORMAT csv, HEADER true, ENCODING 'UTF8',
                                        FORCE_NULL(integer/date 컬럼...))
        │
        ├── 전체 성공 → COMMIT
        └── 중간 실패 → ROLLBACK (테이블 이전 상태 보존)
```

> **`FORCE_NULL`이란?**
> CSV에서 빈 문자열(`""`)을 integer/date 컬럼에 넣으면 오류가 납니다.
> `FORCE_NULL`을 지정하면 빈 문자열을 `NULL`로 자동 변환합니다.

---

## 5. 공통 모듈 설명

### `common/config.py`

`config.yml`을 읽어 실행 환경(dev/stg/law/prod)에 맞는 설정 딕셔너리를 반환합니다.

```python
cfg = load_config('law')
# {
#   'env': 'law',
#   'concurrency': 50,
#   'batch_size': 1000000,
#   'api_list_url': '...',
#   'csv_path': '/home/.../csv',
#   'db': {'host': 'localhost', 'dbname': 'lawdb', ...}
# }
```

---

### `common/logger.py` — `JobLogger`

두 가지 로그를 관리합니다:

| 로그 파일 | 내용 | 기록 방식 |
|---|---|---|
| `crawl_*.log` | 진행 상황, 완료 요약, 경고 | Python `logging` 모듈 |
| `link_*.log` | URL별 실패 상세 (`FAIL ... \| 오류메시지`) | 파일 직접 기록 (상시 open) |
| `final_fail_*.log` | 수집 완료 후 전체 실패 URL 목록 | 수집 종료 시 1회 기록 |

---

### `common/http_client.py`

```python
async def fetch_xml(client, url) -> bytes:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content  # bytes 그대로 반환 (인코딩 변환 없음)
```

> **왜 `resp.text`가 아닌 `resp.content`?**
> httpx는 `Content-Type` 헤더의 `charset`으로 디코딩합니다.
> 서버가 `charset=euc-kr`을 헤더에 잘못 기재하면 UTF-8 바이트를 EUC-KR로 오디코딩합니다.
> `resp.content`(raw bytes)를 사용하면 XML 파서가 `<?xml encoding="utf-8">` 선언을 직접 읽어 올바르게 처리합니다.

---

### `common/xml_parser.py`

lxml이 설치되어 있으면 lxml을 사용하고, 없으면 표준 라이브러리 `xml.etree.ElementTree`로 자동 대체됩니다.

```python
try:
    from lxml import etree
    def parse_xml_string(content: bytes):
        return etree.fromstring(content)
except ImportError:
    def parse_xml_string(content: bytes):
        return ET.fromstring(content)
```

| 함수 | 설명 |
|---|---|
| `parse_xml_string(bytes)` | XML bytes → Element 객체 |
| `get_tag_value(root, tag)` | 하위 태그의 텍스트 반환 |
| `get_tag(el, tag)` | 공백 정규화 포함 태그 텍스트 반환 |
| `get_list_tag_value(el, tag)` | 직접 텍스트 노드만 반환 (하위 태그 제외) |
| `get_tag_attr_and_value(root, tag, attr)` | 속성값 + 텍스트 동시 반환 |
| `make_jo_num(jo_no, jo_ser_no)` | 조번호 6자리 생성 (`0010 + 02` → `001002`) |
| `convert_circled_number(s)` | ① ~ ㊿ → 1 ~ 50 변환 |

---

### `common/csv_writer.py` — `CsvWriter`

asyncio(단일 스레드)와 파일 I/O(블로킹)를 분리하기 위해 백그라운드 스레드를 사용합니다.

```
asyncio worker (메인 스레드)          CsvWriter (백그라운드 스레드)
        │                                       │
  writer.put(row)  →  Queue.put()     Queue.get()  →  csv.writer.writerow()
        │                                       │
  (즉시 반환,                          (행이 들어올 때만 깨어남,
   블로킹 없음)                         배치 크기 초과 시 새 파일 생성)
```

**파일 분할:** `batch_size`(기본 1,000,000행) 초과 시 자동으로 다음 파일로 분할합니다.
- `law_con_0001_20260610.csv` → `law_con_0002_20260610.csv` → ...

**과거 파일 정리:** `CsvWriter` 생성 시 오늘 이전 날짜의 CSV 디렉토리를 자동 삭제합니다 (디스크 용량 관리).

---

## 6. 설정 파일 구조

**`config.yml`**

```yaml
batch_size: 1000000    # CSV 파일당 최대 행 수
concurrency: 50        # 동시 HTTP 요청 수 (worker 수)

law:                   # 인터넷망 허용환경
  api_list_url: "http://www.law.go.kr/DRF/lawSearch.do?OC=...&type=XML"
  api_con_path: "http://www.law.go.kr/DRF/lawService.do?OC=...&type=XML"
  csv_path: "/path/to/csv"
  logs_path: "/path/to/logs"
  db:
    host: "localhost"
    port: 5432
    dbname: "lawdb"
    user: "konan"
    password: "..."

dev:   # 개발 환경 (SSL 검증 비활성화됨)
stg:   # 검증 환경
prod:  # 운영 환경 (별도 도메인)
```

---

## 7. 로그 파일 구조

### `crawl_*.log` — 진행 상황

```
[2026-06-10 12:48:10] INFO [law_content] 대상 URL: 166267건
[2026-06-10 13:41:33] INFO [완료] law_content | 수집: 166258/166267건 | 실패: 9건 | 소요: 00:53:22 | ...
```

### `link_*.log` — URL별 실패 내역

```
[2026-06-10 13:12:33] FAIL    ( 48147/166267,  29.0%) http://...MST=223113... | unclosed CDATA section: line 13788
```

### `final_fail_*.log` — 실패 URL 재수집용 목록

```
# 총 실패: 9건 | 소요시간: 00:53:22
http://www.law.go.kr/DRF/lawService.do?...MST=223113...
http://www.law.go.kr/DRF/lawService.do?...MST=190863...
```

---

## 8. CSV 파일 구조

### `law_list` — 법령 목록

| DOCKEY | LAW_SRNO | LAW_ID | ENTRVS_DVS_CD | ENTRVS_DVS_NM | PRMLGT_YMD | ... | LINK |
|---|---|---|---|---|---|---|---|
| 191415_20170126 | 191415 | 123456 | 300202 | 일부개정 | 20170110 | ... | http://... |

### `law_con` — 법령 본문 (법령 단위)

| DOCKEY | TITLE | JOMUN_DVS_NM | CTXT | LAW_SRNO | ... |
|---|---|---|---|---|---|
| 191415_20170126 | 민법 | 조문 | 제1조 본문... | 191415 | ... |

### `law_jo_con` — 조문 단위

| DOCKEY | JOMUN_NO | JO_NO | TITLE | CTXT | ... |
|---|---|---|---|---|---|
| 191415_20170126_000100 | 1 | 000100 | 민법 제1조 | 제1조 내용... | ... |

### `law_hang_con` — 항 단위

| DOCKEY | JOMUN_NO | JO_NO | HANG_NO | TITLE | CTXT | ... |
|---|---|---|---|---|---|---|
| 191415_20170126_000100_1 | 1 | 000100 | 1 | 민법 제1조 제1항 | 항 내용... | ... |

---

## 9. 실행 방법

### 전체 파이프라인 한 번에 실행

```bash
python main.py all law
```

### 단계별 실행

```bash
# 수집 + 적재 전체
python3 main.py all law

# 1단계: 목록만 수집
python main.py law_list law

# 2단계: 본문만 수집 (law_list가 이미 생성한 csv파일을 기준으로 목록파싱)
python main.py law_content law

# 3단계: DB 적재 (csv 경로에 있는 파일들을 읽어 postgreSQL에 COPY명령 수행)
python main.py load law
```

### 옵션

```bash
# 동시 요청 수 조정 (기본값: config.yml의 concurrency)
python main.py law_content law --concurrency 20

# 특정 URL만 테스트 수집 (디버깅용)
python3 main.py law_content law --test-urls \
    "http://www.law.go.kr/DRF/lawService.do?OC=itplan&target=eflaw&MST=198305&type=XML&efYd=20171201" \
    "http://www.law.go.kr/DRF/lawService.do?OC=itplan&target=eflaw&MST=202344&type=XML&efYd=20180327" \
	"http://www.law.go.kr/DRF/lawService.do?OC=itplan&target=eflaw&MST=264647&type=XML&efYd=20240731"
```

### 환경 점검

```bash
# 패키지 설치 여부 + config + DB 연결 확인을 한번에 체크
python requirements_check.py {env}
python requirements_check.py dev
```

---

## 10. 주요 설계 결정

### 왜 CSV를 거쳐 DB에 넣는가?

직접 INSERT하지 않고 CSV → `COPY` 경로를 쓰는 이유:
- `COPY`는 `INSERT`보다 수십 배 빠릅니다 (166K건 기준 수 분 → 수십 초)
- 수집 실패 시 CSV는 남아있으므로 수집 재실행 없이 적재만 재시도할 수 있습니다
- TRUNCATE + COPY + COMMIT 단일 트랜잭션으로 부분 적재 상태가 발생하지 않습니다

### 왜 asyncio와 threading을 함께 쓰는가?

```
asyncio (HTTP I/O) ──put()──▶ Queue ──▶ threading (파일 I/O)
```

- asyncio는 I/O 대기 중 다른 코루틴을 실행하는 협력형 멀티태스킹입니다
- 파일 쓰기(`csv.writer`)는 블로킹 작업이라 asyncio 루프 안에서 직접 쓰면 전체가 멈춥니다
- Queue를 통해 asyncio ↔ thread 경계를 분리하면 HTTP 수집과 파일 기록이 서로 방해하지 않습니다

### 왜 worker 수 = concurrency인가?

`asyncio.Semaphore`(기존 방식) 대신 worker N개로 concurrency를 제어합니다:
- 기존: 166K 코루틴 생성 후 세마포어로 50개 제한 → 166K 객체가 메모리 상주
- 현재: worker 50개만 생성, 각자 Queue에서 꺼내 처리 → 메모리 고정
- 처리량은 동일하지만 메모리 효율이 크게 개선됩니다

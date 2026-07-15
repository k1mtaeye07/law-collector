-- ============================================================
-- migrate_v2.sql  :  UPSERT 방식 전환을 위한 1회성 DDL
-- 실행 순서: 1→2→3→4→5→6
-- ============================================================

-- 1. 신규 컬럼 추가
ALTER TABLE law_list     ADD COLUMN IF NOT EXISTS eff_end      VARCHAR(8);
ALTER TABLE law_list     ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32);
ALTER TABLE law_list     ADD COLUMN IF NOT EXISTS modify_dt    TIMESTAMP;

ALTER TABLE law_con      ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32);
ALTER TABLE law_con      ADD COLUMN IF NOT EXISTS modify_dt    TIMESTAMP;

ALTER TABLE law_jo_con   ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32);
ALTER TABLE law_jo_con   ADD COLUMN IF NOT EXISTS modify_dt    TIMESTAMP;

ALTER TABLE law_hang_con ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32);
ALTER TABLE law_hang_con ADD COLUMN IF NOT EXISTS modify_dt    TIMESTAMP;

ALTER TABLE auth_int     ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32);
ALTER TABLE auth_int     ADD COLUMN IF NOT EXISTS modify_dt    TIMESTAMP;

ALTER TABLE de_case      ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32);
ALTER TABLE de_case      ADD COLUMN IF NOT EXISTS modify_dt    TIMESTAMP;


-- 2. PK 제약 추가 (ON CONFLICT 대상 / 기존 중복 시 실패할 수 있음)
--    중복 행 있으면 먼저 제거 후 실행:
--      DELETE FROM law_list a USING law_list b WHERE a.ctid < b.ctid AND a.dockey = b.dockey;
ALTER TABLE law_list     ADD CONSTRAINT pk_law_list     PRIMARY KEY (dockey);
ALTER TABLE law_con      ADD CONSTRAINT pk_law_con      PRIMARY KEY (dockey);
ALTER TABLE law_jo_con   ADD CONSTRAINT pk_law_jo_con   PRIMARY KEY (dockey);
ALTER TABLE law_hang_con ADD CONSTRAINT pk_law_hang_con PRIMARY KEY (dockey);
ALTER TABLE auth_int     ADD CONSTRAINT pk_auth_int     PRIMARY KEY (srno, doc_knd);
ALTER TABLE de_case      ADD CONSTRAINT pk_de_case      PRIMARY KEY (instn_dcsnst_srno);


-- 3. 초기 content_hash 계산
UPDATE law_list     SET content_hash = md5(coalesce(crnt_law_nm, ''));
UPDATE law_con      SET content_hash = md5(coalesce(ctxt, '') || coalesce(jomun_dvs_nm, ''));
UPDATE law_jo_con   SET content_hash = md5(coalesce(ctxt, '') || coalesce(title, '') || coalesce(jomun_chg_yn, ''));
UPDATE law_hang_con SET content_hash = md5(coalesce(ctxt, '') || coalesce(title, '') || coalesce(jomun_chg_yn, ''));
UPDATE auth_int     SET content_hash = md5(coalesce(ctxt, '') || coalesce(titl, '') || coalesce(rltd_law, ''));
UPDATE de_case      SET content_hash = md5(coalesce(ctxt, '') || coalesce(cs_nm, ''));


-- 4. 초기 eff_end 계산 (law_list 전용)
--    개정이 추가될 때마다 load_csv.py가 자동 갱신
UPDATE law_list AS t
SET eff_end = sub.new_eff_end
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
WHERE t.dockey = sub.dockey;


-- 5. 초기 modify_dt 설정 (현재 시점으로 일괄 설정 후 NOT NULL 적용)
UPDATE law_list     SET modify_dt = NOW() WHERE modify_dt IS NULL;
UPDATE law_con      SET modify_dt = NOW() WHERE modify_dt IS NULL;
UPDATE law_jo_con   SET modify_dt = NOW() WHERE modify_dt IS NULL;
UPDATE law_hang_con SET modify_dt = NOW() WHERE modify_dt IS NULL;
UPDATE auth_int     SET modify_dt = NOW() WHERE modify_dt IS NULL;
UPDATE de_case      SET modify_dt = NOW() WHERE modify_dt IS NULL;

ALTER TABLE law_list     ALTER COLUMN modify_dt SET NOT NULL;
ALTER TABLE law_con      ALTER COLUMN modify_dt SET NOT NULL;
ALTER TABLE law_jo_con   ALTER COLUMN modify_dt SET NOT NULL;
ALTER TABLE law_hang_con ALTER COLUMN modify_dt SET NOT NULL;
ALTER TABLE auth_int     ALTER COLUMN modify_dt SET NOT NULL;
ALTER TABLE de_case      ALTER COLUMN modify_dt SET NOT NULL;


-- 6. HVM 조회용 인덱스 (검색엔진 증분 배치 성능)
CREATE INDEX IF NOT EXISTS idx_law_list_modify_dt     ON law_list     (modify_dt);
CREATE INDEX IF NOT EXISTS idx_law_con_modify_dt      ON law_con      (modify_dt);
CREATE INDEX IF NOT EXISTS idx_law_jo_con_modify_dt   ON law_jo_con   (modify_dt);
CREATE INDEX IF NOT EXISTS idx_law_hang_con_modify_dt ON law_hang_con (modify_dt);
CREATE INDEX IF NOT EXISTS idx_auth_int_modify_dt     ON auth_int     (modify_dt);
CREATE INDEX IF NOT EXISTS idx_de_case_modify_dt      ON de_case      (modify_dt);

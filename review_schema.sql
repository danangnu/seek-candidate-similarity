-- Execute in the intended database; no USE, DROP or candidate-table ALTER/UPDATE.
-- init-db loads these same statements. All application timestamps use UTC.
-- MariaDB 10.1: LONGTEXT JSON is validated by the applications.
-- JSON/CHECK clauses below execute only on MariaDB 10.2.6 and later.
CREATE TABLE IF NOT EXISTS seek_uuid_match_review (
 review_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 source_table VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 seekid_detail BIGINT NOT NULL,
 proposed_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 numeric_profile_url VARCHAR(512) NOT NULL,
 uuid_profile_url VARCHAR(512) NOT NULL,
 numeric_profile_snapshot LONGTEXT NOT NULL,
 uuid_profile_snapshot LONGTEXT NOT NULL,
 comparison_evidence LONGTEXT NOT NULL,
 exact_content_match TINYINT(1) NOT NULL,
 comparison_version VARCHAR(50) NOT NULL,
 submission_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 source_fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 status VARCHAR(20) NOT NULL DEFAULT 'pending',
 assigned_reviewer VARCHAR(100) NULL,
 reviewed_by VARCHAR(100) NULL,
 reviewed_at DATETIME NULL,
 review_reason TEXT NULL,
 created_by VARCHAR(100) NOT NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 applied_at DATETIME NULL,
 row_version INT UNSIGNED NOT NULL DEFAULT 1,
 UNIQUE KEY uq_seek_review_submission (submission_hash),
 KEY ix_seek_review_queue (status, assigned_reviewer, created_at),
 KEY ix_seek_review_pair (seekid_detail, proposed_uuid),
 KEY ix_seek_review_source (source_table, seekid_detail, status)
 /*M!100206 ,
 CONSTRAINT ck_seek_review_source CHECK (source_table IN ('seek_scrap_detail','seek_scrap')),
 CONSTRAINT ck_seek_review_id CHECK (seekid_detail > 0),
 CONSTRAINT ck_seek_review_exact CHECK (exact_content_match IN (0,1)),
 CONSTRAINT ck_seek_review_status CHECK (status IN ('pending','approved','rejected','applied')),
 CONSTRAINT ck_seek_review_version CHECK (row_version >= 1),
 CONSTRAINT ck_seek_review_old_json CHECK (JSON_VALID(numeric_profile_snapshot)),
 CONSTRAINT ck_seek_review_new_json CHECK (JSON_VALID(uuid_profile_snapshot)),
 CONSTRAINT ck_seek_review_evidence_json CHECK (JSON_VALID(comparison_evidence)),
 CONSTRAINT ck_seek_review_decision CHECK (
   (status='pending' AND reviewed_by IS NULL AND reviewed_at IS NULL AND review_reason IS NULL)
   OR (status IN ('approved','rejected','applied') AND reviewed_by IS NOT NULL
       AND CHAR_LENGTH(TRIM(reviewed_by)) > 0 AND reviewed_at IS NOT NULL
       AND review_reason IS NOT NULL AND CHAR_LENGTH(TRIM(review_reason)) > 0)),
 CONSTRAINT ck_seek_review_applied CHECK (
   (status='applied' AND applied_at IS NOT NULL) OR (status<>'applied' AND applied_at IS NULL))
 */
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS seek_uuid_match_review_history (
 history_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 review_id BIGINT UNSIGNED NOT NULL,
 action VARCHAR(30) NOT NULL,
 performed_by VARCHAR(100) NOT NULL,
 performed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 reason TEXT NULL,
 change_details LONGTEXT NOT NULL,
 KEY ix_seek_review_history (review_id, history_id),
 CONSTRAINT fk_seek_review_history FOREIGN KEY (review_id)
   REFERENCES seek_uuid_match_review (review_id) ON DELETE RESTRICT
 /*M!100206 ,
 CONSTRAINT ck_seek_review_history_json CHECK (JSON_VALID(change_details)),
 CONSTRAINT ck_seek_review_history_action CHECK (action IN ('submitted','assigned','approved','rejected','applied'))
 */
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- Operational leases only. No candidate columns or review decisions are changed.
CREATE TABLE IF NOT EXISTS seek_uuid_work_claim (
 source_table VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 seekid_detail BIGINT NOT NULL,
 claim_token CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 worker_id VARCHAR(160) NOT NULL,
 claimed_by VARCHAR(100) NOT NULL,
 state VARCHAR(20) NOT NULL,
 claimed_at DATETIME NOT NULL,
 heartbeat_at DATETIME NOT NULL,
 expires_at DATETIME NOT NULL,
 last_result VARCHAR(64) NULL,
 PRIMARY KEY (source_table, seekid_detail),
 KEY ix_seek_claim_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

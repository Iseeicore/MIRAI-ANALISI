-- Batch ingestion state store and job queue (MySQL 8, InnoDB, utf8mb4).
-- Applied idempotently at worker startup. A study is a StudyInstanceUID (decision 0003), stored only
-- as study_uid_hash = sha256(StudyInstanceUID). Folder paths relative to the source root are kept
-- (study.folder, study_image.source_path, skipped_file.folder) so failures can be located: access
-- to this database must be restricted.

CREATE TABLE IF NOT EXISTS batch (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_root     VARCHAR(1024) NOT NULL,
    status          ENUM('PENDING', 'CRAWLING', 'CRAWLED') NOT NULL DEFAULT 'PENDING',
    total_studies   INT UNSIGNED NOT NULL DEFAULT 0,
    total_images    INT UNSIGNED NOT NULL DEFAULT 0,
    total_skipped   INT UNSIGNED NOT NULL DEFAULT 0,
    locked_by       VARCHAR(128) NULL,
    lease_until     DATETIME(6) NULL,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_batch_status_lease (status, lease_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS study (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    batch_id        BIGINT UNSIGNED NOT NULL,
    study_uid_hash  CHAR(64) NOT NULL,
    folder          VARCHAR(1024) NOT NULL DEFAULT '.',
    anon_id         VARCHAR(64) NULL,
    status          ENUM('COLLECTING', 'DISCOVERED', 'FETCHING', 'READY', 'SCORING', 'DONE', 'INCOMPLETE',
                         'FAILED') NOT NULL DEFAULT 'COLLECTING',
    attempts        INT UNSIGNED NOT NULL DEFAULT 0,
    locked_by       VARCHAR(128) NULL,
    lease_until     DATETIME(6) NULL,
    next_attempt_at DATETIME(6) NULL,
    error           TEXT NULL,
    score_year_1    DOUBLE NULL,
    score_year_2    DOUBLE NULL,
    score_year_3    DOUBLE NULL,
    score_year_4    DOUBLE NULL,
    score_year_5    DOUBLE NULL,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_study_batch_uid (batch_id, study_uid_hash),
    KEY ix_study_status_lease (status, lease_until),
    CONSTRAINT fk_study_batch FOREIGN KEY (batch_id) REFERENCES batch (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS study_image (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    study_id        BIGINT UNSIGNED NOT NULL,
    batch_id        BIGINT UNSIGNED NOT NULL,
    source_path     VARCHAR(2048) NOT NULL,
    path_hash       CHAR(64) NOT NULL,
    size            BIGINT UNSIGNED NOT NULL DEFAULT 0,
    view_type       ENUM('L_CC', 'L_MLO', 'R_CC', 'R_MLO') NULL,
    staged_path     VARCHAR(512) NULL,
    selected        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (id),
    UNIQUE KEY uq_study_image_batch_path (batch_id, path_hash),
    KEY ix_study_image_study (study_id),
    CONSTRAINT fk_study_image_study FOREIGN KEY (study_id) REFERENCES study (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS skipped_file (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    batch_id        BIGINT UNSIGNED NOT NULL,
    path_hash       CHAR(64) NOT NULL,
    folder          VARCHAR(1024) NOT NULL,
    file_name       VARCHAR(512) NOT NULL,
    reason          VARCHAR(255) NOT NULL,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_skipped_file_batch_path (batch_id, path_hash),
    CONSTRAINT fk_skipped_file_batch FOREIGN KEY (batch_id) REFERENCES batch (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

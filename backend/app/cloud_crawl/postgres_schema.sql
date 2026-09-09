CREATE TABLE IF NOT EXISTS crawl_cloud_job (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    crawl_date DATE NOT NULL,
    job_type TEXT NOT NULL,
    task_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    leased_count INTEGER NOT NULL DEFAULT 0,
    succeeded_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    dead_letter_count INTEGER NOT NULL DEFAULT 0,
    config_json TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_crawl_cloud_job_status
ON crawl_cloud_job(status, crawl_date, id);

CREATE TABLE IF NOT EXISTS crawl_cloud_station_od_task (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES crawl_cloud_job(id),
    provider TEXT NOT NULL,
    crawl_date DATE NOT NULL,
    from_station TEXT NOT NULL,
    to_station TEXT NOT NULL,
    from_station_code TEXT,
    to_station_code TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_run_at TIMESTAMPTZ,
    leased_by TEXT,
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    response_row_count INTEGER NOT NULL DEFAULT 0,
    matched_train_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    last_error_type TEXT,
    last_error TEXT,
    last_http_status INTEGER,
    last_response_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    UNIQUE(job_id, crawl_date, from_station, to_station)
);

CREATE INDEX IF NOT EXISTS idx_crawl_cloud_station_od_pick
ON crawl_cloud_station_od_task(status, next_run_at, priority, id);

CREATE INDEX IF NOT EXISTS idx_crawl_cloud_station_od_lease
ON crawl_cloud_station_od_task(status, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_crawl_cloud_station_od_key
ON crawl_cloud_station_od_task(provider, crawl_date, from_station, to_station);

CREATE INDEX IF NOT EXISTS idx_crawl_cloud_station_od_job_status
ON crawl_cloud_station_od_task(job_id, status, id);

CREATE TABLE IF NOT EXISTS crawl_cloud_station_od_response (
    job_id BIGINT NOT NULL,
    task_id BIGINT NOT NULL,
    provider TEXT NOT NULL,
    crawl_date DATE NOT NULL,
    from_station TEXT NOT NULL,
    to_station TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    row_count INTEGER NOT NULL DEFAULT 0,
    response_hash TEXT NOT NULL,
    object_uri TEXT,
    response_excerpt TEXT,
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(job_id, task_id, response_hash)
);

CREATE INDEX IF NOT EXISTS idx_crawl_cloud_response_key
ON crawl_cloud_station_od_response(provider, crawl_date, from_station, to_station);

CREATE TABLE IF NOT EXISTS crawl_cloud_dead_letter (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    task_id BIGINT NOT NULL,
    provider TEXT NOT NULL,
    crawl_date DATE NOT NULL,
    from_station TEXT NOT NULL,
    to_station TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    http_status INTEGER,
    response_excerpt TEXT,
    worker_id TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_crawl_cloud_dead_letter_job
ON crawl_cloud_dead_letter(job_id, created_at, id);

CREATE INDEX IF NOT EXISTS idx_crawl_cloud_dead_letter_key
ON crawl_cloud_dead_letter(provider, crawl_date, from_station, to_station);
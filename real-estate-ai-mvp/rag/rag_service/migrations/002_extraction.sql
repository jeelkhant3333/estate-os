-- Structured facts read from a document version (projects and listings with their prices), for the
-- CRM to turn into projects and inventory. Computed by an EXTRACT job; one result per version.
ALTER TABLE kb_documents ADD COLUMN extraction jsonb;
ALTER TABLE kb_documents ADD COLUMN extraction_status text NOT NULL DEFAULT 'NONE'
  CHECK (extraction_status IN ('NONE','QUEUED','DONE','FAILED'));
ALTER TABLE kb_documents ADD COLUMN extraction_error text;

ALTER TABLE kb_jobs DROP CONSTRAINT kb_jobs_kind_check;
ALTER TABLE kb_jobs ADD CONSTRAINT kb_jobs_kind_check CHECK (kind IN ('INGEST','EXTRACT'));

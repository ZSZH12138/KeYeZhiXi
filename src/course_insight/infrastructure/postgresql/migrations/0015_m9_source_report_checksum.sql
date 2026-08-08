ALTER TABLE m9_model_invocation_audits
    ADD COLUMN source_report_checksum CHAR(64);

UPDATE m9_model_invocation_audits
SET source_report_checksum = payload ->> 'source_report_checksum';

DO $m9_source_report_binding$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM m9_model_invocation_audits AS audit
        LEFT JOIN m9_teacher_analytics AS report
            ON report.report_id = audit.source_report_id
        WHERE report.report_id IS NULL
           OR audit.payload ->> 'source_report_id'
                IS DISTINCT FROM audit.source_report_id
           OR audit.source_report_checksum
                IS DISTINCT FROM report.payload_checksum
    ) THEN
        RAISE EXCEPTION
            'M9 model audit source report checksum mismatch'
            USING ERRCODE = '23514';
    END IF;
END
$m9_source_report_binding$;

ALTER TABLE m9_model_invocation_audits
    ALTER COLUMN source_report_checksum SET NOT NULL,
    ADD CONSTRAINT m9_model_audit_source_report_checksum_format
        CHECK (source_report_checksum ~ '^[0-9a-f]{64}$');

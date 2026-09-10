"""Single-source DDL shared by init-db and deployment SQL."""
from pathlib import Path

REVIEW_COLUMNS = '''review_id source_table seekid_detail proposed_uuid numeric_profile_url uuid_profile_url
numeric_profile_snapshot uuid_profile_snapshot comparison_evidence exact_content_match comparison_version
submission_hash source_fingerprint status assigned_reviewer reviewed_by reviewed_at review_reason created_by
created_at applied_at row_version'''.split()
HISTORY_COLUMNS = '''history_id review_id action performed_by performed_at reason change_details'''.split()

CLAIM_COLUMNS = 'source_table seekid_detail claim_token worker_id claimed_by state claimed_at heartbeat_at expires_at last_result'.split()


def schema_statements():
    sql = Path(__file__).with_name('review_schema.sql').read_text(encoding='utf-8')
    sql = '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
    return [s.strip() for s in sql.split(';') if s.strip()]

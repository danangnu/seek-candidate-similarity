"""Proposal-only repository. Candidate tables are strictly read-only."""
import hashlib
import json
import re
from contextlib import contextmanager
from profiles import canonical_uuid
from network_config import database_options

TARGETS = {
    'seek_scrap': {'pk': 'id_pk', 'numeric': 'id', 'uuid': None},
    'seek_scrap_detail': {'pk': 'id_detail', 'numeric': 'seekid_detail', 'uuid': 'uuid'},
}


def target_fields(table):
    t = TARGETS[table]
    metadata = ('name, file, scrap_date, seek_scrap_id AS source_link' if table == 'seek_scrap'
                else 'NULL AS name, NULL AS file, NULL AS scrap_date')
    uuid_column = t['uuid'] or 'NULL'  # seek_scrap has an integer link, not a UUID column.
    return f"{t['pk']} AS id_pk, {t['numeric']} AS id, {uuid_column} AS uuid, {metadata}"

from review_schema import REVIEW_COLUMNS, HISTORY_COLUMNS, schema_statements
from profiles import compare_evidence, normalized_profile_content

COMPARISON_VERSION = 'profile_content_v2_review_v1'


def staff_id(value, field, optional=False):
    if optional and (value is None or value == ''):
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 100 or any(ord(c) < 32 for c in value):
        raise ValueError(field+' must be a staff ID of 1 to 100 characters')
    return value.strip()


def review_actors(config):
    # Legacy reviewer identified the operator; it never implies an approval.
    creator = staff_id(config.get('created_by') or config.get('reviewer'), 'created_by')
    review = config.get('review', {})
    if not isinstance(review, dict):
        raise ValueError('review must be an object')
    assignee = staff_id(review.get('assigned_reviewer'), 'review.assigned_reviewer', optional=True)
    return creator, assignee


def blank(value):
    return value is None or not str(value).strip()


def fingerprint(rows):
    return hashlib.sha256(json.dumps(sorted(rows, key=lambda r: r['id_pk']), sort_keys=True,
                                     default=str, ensure_ascii=False).encode()).hexdigest()


def check_mapping(rows, other_rows, numeric_id, uid):
    if not rows or any(r['id'] != numeric_id for r in rows):
        raise ValueError('Numeric SEEK ID missing or inconsistent')
    if any(not blank(r['uuid']) and canonical_uuid(r['uuid']) != uid for r in rows):
        raise ValueError('This numeric SEEK ID already has a different UUID')
    if any(r['id'] is not None and r['id'] != numeric_id for r in other_rows):
        raise ValueError('This UUID already belongs to another numeric SEEK ID')
    # UUID-only rows (id NULL) are valid snapshots from the new importer.


class Repository:
    def __init__(self, config, password):
        import pymysql
        options = database_options(config, password)
        self.require_tls = 'ssl' in options
        self.database = config['database']
        self.target_table = config.get('target_table', 'seek_scrap_detail')
        if self.target_table not in TARGETS:
            raise ValueError('target_table must be seek_scrap_detail or seek_scrap')
        self.connection = pymysql.connect(**options, cursorclass=pymysql.cursors.DictCursor)
        try:
            self.verify_connection()
            self.preflight([self.target_table], check_type=False)
        except Exception:
            self.connection.close()
            raise

    def verify_connection(self):
        with self.connection.cursor() as cur:
            cur.execute("SET time_zone = '+00:00'")
            cur.execute('SELECT DATABASE() AS db, @@hostname AS hostname, @@port AS port')
            self.server = cur.fetchone()
            if not self.server or self.server['db'] != self.database:
                raise ValueError('Connected database differs from the configured database')
            if self.require_tls:
                cur.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
                tls = cur.fetchone()
                if not tls or not tls.get('Value'):
                    raise ValueError('MariaDB TLS was requested but the server did not establish an encrypted session')

    def scrap_idle_settings(self, settings_id=1):
        from runtime_breaks import BreakSettingsError
        if type(settings_id) is not int or settings_id < 1:
            raise BreakSettingsError('breaks.settings_id must be a positive integer.')
        # Called outside candidate write transactions; long pauses can expire a connection.
        self.connection.ping(reconnect=True)
        self.verify_connection()
        with self.connection.cursor() as cur:
            cur.execute('SELECT idle_less_than, idle_less_than2, idle_more_than, idle_more_than2 '
                        'FROM seek_scrap_settings WHERE id=%s', (settings_id,))
            row = cur.fetchone()
        if row is None:
            raise BreakSettingsError('No seek_scrap_settings row with id='+str(settings_id)+
                                     '. Check the selected database and breaks.settings_id. The bundled setup SQL is only for the local test copy.')
        return row

    def preflight(self, tables, check_type=False, target_table=None):
        # Candidate tables are sources only. Their old numeric relationship columns
        # are not changed or required to be UUID text columns.
        target = target_table or self.target_table
        if target not in TARGETS:
            raise ValueError('Unknown source table')
        allowed = set(TARGETS) | {'seek_uuid_match_review', 'seek_uuid_match_review_history'}
        if any(table not in allowed for table in tables):
            raise ValueError('Unknown review/source table')
        with self.connection.cursor() as cur:
            for table in set(tables + [target]):
                cur.execute('SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s',
                            (self.database, table))
                row = cur.fetchone()
                if not row or not row['ENGINE'] or row['ENGINE'].lower() != 'innodb':
                    raise ValueError(table+' must exist and use InnoDB. Run init-db for missing review tables.')
                if table in ('seek_uuid_match_review', 'seek_uuid_match_review_history'):
                    columns = REVIEW_COLUMNS if table == 'seek_uuid_match_review' else HISTORY_COLUMNS
                    cur.execute('SELECT '+', '.join(columns)+' FROM '+table+' LIMIT 0')
            cur.execute('SELECT '+target_fields(target)+' FROM '+target+' LIMIT 0')
            if 'seek_scrap' in tables:
                cur.execute('SELECT id, date_updated FROM seek_scrap LIMIT 0')

    def initialize(self):
        self.preflight([self.target_table])
        with self.connection.cursor() as cur:
            for statement in schema_statements():
                cur.execute(statement)
        self.preflight(['seek_uuid_match_review', 'seek_uuid_match_review_history'])

    def ids(self):
        self.preflight(['seek_uuid_match_review', 'seek_uuid_match_review_history', 'seek_scrap'])
        t=TARGETS[self.target_table]
        # A numeric person can have multiple historical snapshots. Rank the person
        # by their newest profile date, not a row's insertion/scrape date or parent ID.
        history_join = (' LEFT JOIN seek_scrap history ON history.id=s.seekid_detail'
                        if self.target_table == 'seek_scrap_detail' else '')
        profile_date = 'history.date_updated' if history_join else 's.date_updated'
        with self.connection.cursor() as cur:
            cur.execute(f"SELECT s.{t['numeric']} AS id, MAX({profile_date}) AS latest_profile_updated "
                        f"FROM {self.target_table} s{history_join} "
                        f"WHERE s.{t['numeric']} > 0 AND NOT EXISTS (SELECT 1 FROM seek_uuid_match_review r "
                        f"WHERE r.source_table=%s AND r.seekid_detail=s.{t['numeric']}) "
                        f"GROUP BY s.{t['numeric']} "
                        "ORDER BY latest_profile_updated IS NULL ASC, latest_profile_updated DESC, id ASC",
                        (self.target_table,))
            return [r['id'] for r in cur.fetchall()]

    def rows(self, numeric_id):
        t=TARGETS[self.target_table]
        order = 'scrap_date DESC, id_pk DESC' if self.target_table == 'seek_scrap' else 'id_detail DESC'
        with self.connection.cursor() as cur:
            cur.execute('SELECT '+target_fields(self.target_table)+f" FROM {self.target_table} WHERE {t['numeric']}=%s ORDER BY "+order, (numeric_id,))
            return cur.fetchall()

    @contextmanager
    def transaction(self):
        c = self.connection
        lock = 'seek_review:'+hashlib.sha256(self.database.encode()).hexdigest()[:40]
        with c.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s, 10) AS acquired', (lock,))
            if cur.fetchone()['acquired'] != 1:
                raise ValueError('Another proposal submission holds the database lock')
        try:
            with c.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL SERIALIZABLE')
            c.begin()
            with c.cursor() as cur:
                yield cur
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            with c.cursor() as cur:
                cur.execute('SELECT RELEASE_LOCK(%s)', (lock,))

    def submit_proposal(self, numeric_id, uid, expected_fingerprint, evidence, created_by, assigned_reviewer=None):
        """Insert a pending proposal and submitted audit event; NEVER mutate candidates."""
        if type(numeric_id) is not int or numeric_id <= 0:
            raise ValueError('Numeric SEEK ID must be a positive integer')
        uid = canonical_uuid(uid)
        created_by = staff_id(created_by, 'created_by')
        assigned_reviewer = staff_id(assigned_reviewer, 'assigned_reviewer', optional=True)
        old = evidence['old_profile']
        selected = evidence['selected']
        if canonical_uuid(selected['uuid']) != uid:
            raise ValueError('Selected UUID differs from proposal UUID')
        new = selected['profile']
        comparison = compare_evidence(old, new)  # Recompute, never trust caller's flag.
        if not comparison['profile_content_equal'] and not comparison['eligible_for_review']:
            raise ValueError('Insufficient profile evidence to submit a proposal; name alone is not enough')
        if any(p.get('profile_content_scope') != 'profile_tab' or not p.get('profile_content') for p in (old,new)):
            raise ValueError('Both complete Profile snapshots are required for review')
        encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)
        digest = hashlib.sha256(encode({
            'source_table': self.target_table, 'numeric_id': numeric_id, 'uuid': uid,
            'old': normalized_profile_content(old), 'new': normalized_profile_content(new),
            'version': COMPARISON_VERSION}).encode('utf-8')).hexdigest()
        # Browsing/inference may outlast DB idle timeout. Reconnect BEFORE transaction.
        self.connection.ping(reconnect=True)
        self.verify_connection()
        self.preflight(['seek_uuid_match_review', 'seek_uuid_match_review_history'])
        with self.transaction() as cur:
            cur.execute('SELECT review_id, status, assigned_reviewer FROM seek_uuid_match_review WHERE submission_hash=%s FOR UPDATE', (digest,))
            existing = cur.fetchone()
            if existing:
                return {'review_id': existing['review_id'], 'status': existing['status'], 'created': False,
                        'assigned_reviewer': existing['assigned_reviewer']}
            cur.execute("SELECT review_id FROM seek_uuid_match_review WHERE source_table=%s AND seekid_detail=%s "
                        "AND status IN ('pending','approved','applied') FOR UPDATE", (self.target_table, numeric_id))
            if cur.fetchone():
                raise ValueError('This numeric ID already has an active or applied proposal. Review it in the separate app.')
            rows = self.rows(numeric_id)
            if not rows or fingerprint(rows) != expected_fingerprint:
                raise ValueError('Source rows changed since comparison. Rerun this numeric ID.')
            evidence = dict(evidence, comparison=comparison, source_table=self.target_table,
                            source_rows=rows, source_fingerprint=expected_fingerprint,
                            numeric_key=TARGETS[self.target_table]['numeric'])
            cur.execute('INSERT INTO seek_uuid_match_review '
                        '(source_table,seekid_detail,proposed_uuid,numeric_profile_url,uuid_profile_url,'
                        'numeric_profile_snapshot,uuid_profile_snapshot,comparison_evidence,exact_content_match,'
                        'comparison_version,submission_hash,source_fingerprint,status,assigned_reviewer,created_by) '
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)",
                        (self.target_table, numeric_id, uid,
                         'https://au.employer.seek.com/talentsearch/profiles/'+str(numeric_id),
                         'https://au.employer.seek.com/talentsearch/profiles/'+uid,
                         encode(old), encode(new), encode(evidence), int(comparison['profile_content_equal']),
                         COMPARISON_VERSION, digest, expected_fingerprint, assigned_reviewer, created_by))
            review_id = cur.lastrowid
            cur.execute('INSERT INTO seek_uuid_match_review_history '
                        '(review_id,action,performed_by,reason,change_details) VALUES (%s,%s,%s,%s,%s)',
                        (review_id, 'submitted', created_by, 'Profile comparison submitted for human review',
                         encode({'from_status': None, 'to_status': 'pending', 'assigned_reviewer': assigned_reviewer,
                                 'row_version': 1, 'submission_hash': digest})))
        return {'review_id': review_id, 'status': 'pending', 'created': True, 'assigned_reviewer': assigned_reviewer}

    def close(self):
        self.connection.close()

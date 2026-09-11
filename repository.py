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

from review_schema import REVIEW_COLUMNS, HISTORY_COLUMNS, CLAIM_COLUMNS, schema_statements
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
        self._connection_options = options
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

    def run_schedule(self):
        # Tiny read-only table; database clock is shared by all worker machines.
        self.connection.ping(reconnect=True)
        self.verify_connection()
        with self.connection.cursor() as cur:
            cur.execute('SELECT UTC_TIMESTAMP() AS server_utc')
            now = cur.fetchone()['server_utc']
            cur.execute('SELECT day, time_from, time_to FROM seek_run_times ORDER BY day, id')
            rows = cur.fetchall()
        return {'server_utc': now, 'rows': rows}

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
        allowed = set(TARGETS) | {'seek_uuid_match_review', 'seek_uuid_match_review_history', 'seek_uuid_work_claim'}
        if any(table not in allowed for table in tables):
            raise ValueError('Unknown review/source table')
        with self.connection.cursor() as cur:
            for table in set(tables + [target]):
                cur.execute('SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s',
                            (self.database, table))
                row = cur.fetchone()
                if not row or not row['ENGINE'] or row['ENGINE'].lower() != 'innodb':
                    raise ValueError(table+' must exist and use InnoDB. Run init-db for missing review tables.')
                if table in ('seek_uuid_match_review', 'seek_uuid_match_review_history', 'seek_uuid_work_claim'):
                    columns = {'seek_uuid_match_review': REVIEW_COLUMNS,
                               'seek_uuid_match_review_history': HISTORY_COLUMNS,
                               'seek_uuid_work_claim': CLAIM_COLUMNS}[table]
                    cur.execute('SELECT '+', '.join(columns)+' FROM '+table+' LIMIT 0')
            cur.execute('SELECT '+target_fields(target)+' FROM '+target+' LIMIT 0')
            if 'seek_scrap' in tables:
                cur.execute('SELECT id, seek_scrap_id, date_updated FROM seek_scrap LIMIT 0')

    def initialize(self):
        self.preflight([self.target_table])
        with self.connection.cursor() as cur:
            for statement in schema_statements():
                cur.execute(statement)
        self.preflight(['seek_uuid_match_review', 'seek_uuid_match_review_history', 'seek_uuid_work_claim'])

    QUEUE_PAGE_SIZE = 500

    def _queue_read(self, sql, params=()):
        # No cursor/transaction is kept open while Chrome works or the scraper idles.
        self.connection.ping(reconnect=True)
        self.verify_connection()
        with self.connection.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _queue_eligible(self, ids, exclude_claimed=False):
        if not ids:
            return set()
        t = TARGETS[self.target_table]
        placeholders = ','.join(['%s'] * len(ids))
        linked = (' AND EXISTS (SELECT 1 FROM seek_scrap h WHERE h.seek_scrap_id=s.id_detail)'
                  if self.target_table == 'seek_scrap_detail' else '')
        rows = self._queue_read(
            f"SELECT DISTINCT s.{t['numeric']} AS id FROM {self.target_table} s "
            f"WHERE s.{t['numeric']} IN ({placeholders}){linked} AND NOT EXISTS "
            "(SELECT 1 FROM seek_uuid_match_review r WHERE r.source_table=%s "
            f"AND r.seekid_detail=s.{t['numeric']})"+self._claim_filter(exclude_claimed, t), tuple(ids) + (self.target_table,))
        return {row['id'] for row in rows}

    def _claim_filter(self, enabled, target):
        if not enabled:
            return ''
        return (" AND NOT EXISTS (SELECT 1 FROM seek_uuid_work_claim wc WHERE "
                "wc.source_table='"+self.target_table+"' AND wc.seekid_detail=s."+target['numeric']+
                " AND wc.expires_at > UTC_TIMESTAMP())")

    def _queue_latest_date(self, before=None):
        predicate = 'date_updated IS NOT NULL' if before is None else 'date_updated < %s'
        rows = self._queue_read(
            'SELECT date_updated FROM seek_scrap FORCE INDEX (date_updated) WHERE '+predicate+
            ' ORDER BY date_updated DESC LIMIT 1', () if before is None else (before,))
        return rows[0]['date_updated'] if rows else None

    def _queue_history_sql(self, before_pk=None):
        cursor_filter = '' if before_pk is None else ' AND id_pk < %s'
        if self.target_table == 'seek_scrap_detail':
            # Limit raw history FIRST so a page of orphaned links cannot end the
            # scan early. LEFT JOIN keeps its cursor rows; NULL IDs are never yielded.
            return ('SELECT p.id_pk, d.seekid_detail AS id, p.date_updated '
                    'FROM (SELECT id_pk, seek_scrap_id, date_updated '
                    'FROM seek_scrap FORCE INDEX (date_updated) '
                    'WHERE date_updated=%s'+cursor_filter+' ORDER BY id_pk DESC LIMIT %s) p '
                    'LEFT JOIN seek_scrap_detail d ON d.id_detail=p.seek_scrap_id '
                    'ORDER BY p.id_pk DESC')
        return ('SELECT id_pk, id, date_updated FROM seek_scrap FORCE INDEX (date_updated) '
                'WHERE date_updated=%s'+cursor_filter+' ORDER BY id_pk DESC LIMIT %s')

    def _queue_history_page(self, profile_date, before_pk=None):
        params = (profile_date,) if before_pk is None else (profile_date, before_pk)
        return self._queue_read(self._queue_history_sql(before_pk), params + (self.QUEUE_PAGE_SIZE,))

    def _queue_undated_page(self, after_id=0, exclude_claimed=False):
        t = TARGETS[self.target_table]
        relation = ('h.seek_scrap_id=s.id_detail' if self.target_table == 'seek_scrap_detail'
                    else f"h.id=s.{t['numeric']}")
        linked = (' AND EXISTS (SELECT 1 FROM seek_scrap h WHERE '+relation+')'
                  if self.target_table == 'seek_scrap_detail' else '')
        # Undated people must have linked history, matching the requested INNER JOIN.
        return self._queue_read(
            f"SELECT DISTINCT s.{t['numeric']} AS id FROM {self.target_table} s "
            f"WHERE s.{t['numeric']} > %s{linked} AND NOT EXISTS (SELECT 1 FROM seek_scrap h "
            f"WHERE {relation} AND h.date_updated IS NOT NULL) "
            "AND NOT EXISTS (SELECT 1 FROM seek_uuid_match_review r WHERE r.source_table=%s "
            f"AND r.seekid_detail=s.{t['numeric']})"+self._claim_filter(exclude_claimed, t)+
            f" ORDER BY s.{t['numeric']} ASC LIMIT %s",
            (after_id, self.target_table, self.QUEUE_PAGE_SIZE))

    def _queue_csv_ranked(self, candidate_ids, exclude_claimed=False):
        # For an explicit CSV, aggregate only selected numeric IDs via the source/link indexes.
        dates = {}
        for offset in range(0, len(candidate_ids), self.QUEUE_PAGE_SIZE):
            chunk = candidate_ids[offset:offset+self.QUEUE_PAGE_SIZE]
            eligible = self._queue_eligible(chunk, exclude_claimed)
            if not eligible:
                continue
            placeholders = ','.join(['%s'] * len(eligible))
            if self.target_table == 'seek_scrap_detail':
                sql = ('SELECT d.seekid_detail AS id, MAX(h.date_updated) AS latest_profile_updated '
                       'FROM seek_scrap_detail d JOIN seek_scrap h ON h.seek_scrap_id=d.id_detail '
                       'WHERE d.seekid_detail IN ('+placeholders+') GROUP BY d.seekid_detail')
            else:
                sql = ('SELECT id, MAX(date_updated) AS latest_profile_updated '
                       'FROM seek_scrap FORCE INDEX (id) WHERE id IN ('+placeholders+') GROUP BY id')
            rows = self._queue_read(sql, tuple(sorted(eligible)))
            latest = {row['id']: row['latest_profile_updated'] for row in rows}
            dates.update({numeric_id: latest.get(numeric_id) for numeric_id in eligible})
        # Stable numeric ties for the small explicitly selected CSV set.
        ordered = sorted(dates)
        ordered.sort(key=lambda numeric_id: str(dates[numeric_id] or ''), reverse=True)
        return ordered

    def iter_ids(self, limit=None, candidate_ids=None, exclude_claimed=False):
        """Lazily yield newest-profile people; no whole-table GROUP BY/derived sort."""
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError('Queue limit must be a positive integer')
        self.preflight(['seek_uuid_match_review', 'seek_uuid_match_review_history', 'seek_scrap'] +
                       (['seek_uuid_work_claim'] if exclude_claimed else []))
        if candidate_ids is not None:
            selected = sorted(set(candidate_ids))
            if any(type(n) is not int or n < 1 for n in selected):
                raise ValueError('CSV numeric IDs must be positive integers')
            for index, numeric_id in enumerate(self._queue_csv_ranked(selected, exclude_claimed)):
                if limit is not None and index >= limit:
                    break
                yield numeric_id
            return
        seen = set()
        emitted = 0
        scanned = 0
        profile_date = self._queue_latest_date()
        while profile_date is not None:
            before_pk = None
            while True:
                page = self._queue_history_page(profile_date, before_pk)
                if not page:
                    break
                before_pk = page[-1]['id_pk']
                scanned += len(page)
                fresh = []
                for row in page:
                    numeric_id = row['id']
                    if numeric_id is not None and numeric_id > 0 and numeric_id not in seen:
                        seen.add(numeric_id)
                        fresh.append(numeric_id)
                eligible = self._queue_eligible(fresh, exclude_claimed)
                for numeric_id in fresh:
                    if numeric_id in eligible:
                        emitted += 1
                        yield numeric_id
                        if limit is not None and emitted >= limit:
                            return
                if scanned and scanned % (self.QUEUE_PAGE_SIZE * 20) == 0:
                    print('Queue progress:', scanned, 'history rows scanned;', emitted, 'people yielded.')
                if len(page) < self.QUEUE_PAGE_SIZE:
                    break
            profile_date = self._queue_latest_date(before=profile_date)
        after_id = 0
        while True:
            page = self._queue_undated_page(after_id, exclude_claimed)
            if not page:
                return
            after_id = page[-1]['id']
            for row in page:
                if row['id'] not in seen:
                    seen.add(row['id'])
                    emitted += 1
                    yield row['id']
                    if limit is not None and emitted >= limit:
                        return
            if len(page) < self.QUEUE_PAGE_SIZE:
                return

    def ids(self, limit=None, candidate_ids=None):
        # Compatibility helper; normal scraping uses iter_ids instead of materializing all IDs.
        return list(self.iter_ids(limit=limit, candidate_ids=candidate_ids))

    def explain_queue(self):
        self.preflight([self.target_table, 'seek_scrap', 'seek_uuid_match_review'])
        queries = [('Latest date lookup',
                    'SELECT date_updated FROM seek_scrap FORCE INDEX (date_updated) '
                    'WHERE date_updated IS NOT NULL ORDER BY date_updated DESC LIMIT 1', ())]
        latest = self._queue_latest_date()
        if latest is not None:
            queries.append(('History page within latest date',
                            self._queue_history_sql(), (latest,self.QUEUE_PAGE_SIZE)))
        for label, sql, params in queries:
            print(label+':')
            print(json.dumps(self._queue_read('EXPLAIN '+sql,params),default=str,indent=2))

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

    def submit_proposal(self, numeric_id, uid, expected_fingerprint, evidence, created_by, assigned_reviewer=None, *, claim_token=None):
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
            if claim_token is not None:
                self._assert_claim(cur, numeric_id, claim_token)
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
                            numeric_key=TARGETS[self.target_table]['numeric'],
                            history_relationship=('seek_scrap.seek_scrap_id = seek_scrap_detail.id_detail'
                                                  if self.target_table == 'seek_scrap_detail' else 'legacy seek_scrap.id'))
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

    def claim_connection(self):
        # Dedicated heartbeat connection: never share the browser thread's PyMySQL connection.
        import pymysql
        other = Repository.__new__(Repository)
        other.database = self.database
        other.target_table = self.target_table
        other.require_tls = self.require_tls
        other._connection_options = dict(self._connection_options)
        # Bound heartbeat IO independently of long foreground query timeouts.
        for name in ('connect_timeout', 'read_timeout', 'write_timeout'):
            other._connection_options[name] = min(other._connection_options[name], 10)
        other.connection = pymysql.connect(**other._connection_options, cursorclass=pymysql.cursors.DictCursor)
        try:
            other.verify_connection()
        except BaseException:
            other.close()
            raise
        return other

    def claim_candidate(self, numeric_id, worker_id, created_by, lease_seconds, allow_rejected=False):
        from uuid import uuid4
        from work_claims import validate_duration
        validate_duration(lease_seconds, 'claims.lease_seconds', 120, 86400)
        if type(numeric_id) is not int or numeric_id <= 0:
            raise ValueError('Claim numeric ID must be positive')
        created_by = staff_id(created_by, 'created_by')
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 160:
            raise ValueError('Claim worker ID must contain 1 to 160 characters')
        self.connection.ping(reconnect=True)
        self.verify_connection()
        token = uuid4().hex
        with self.transaction() as cur:
            review_filter = " AND status IN ('pending','approved','applied')" if allow_rejected else ''
            cur.execute('SELECT review_id FROM seek_uuid_match_review WHERE source_table=%s '
                        'AND seekid_detail=%s'+review_filter+' LIMIT 1 FOR UPDATE', (self.target_table, numeric_id))
            if cur.fetchone():
                return None
            cur.execute('SELECT claim_token, expires_at > UTC_TIMESTAMP() AS live FROM seek_uuid_work_claim '
                        'WHERE source_table=%s AND seekid_detail=%s FOR UPDATE', (self.target_table, numeric_id))
            existing = cur.fetchone()
            if existing and existing['live']:
                return None
            if existing:
                cur.execute("UPDATE seek_uuid_work_claim SET claim_token=%s, worker_id=%s, claimed_by=%s, "
                            "state='active', claimed_at=UTC_TIMESTAMP(), heartbeat_at=UTC_TIMESTAMP(), "
                            "expires_at=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP()), last_result=NULL "
                            "WHERE source_table=%s AND seekid_detail=%s",
                            (token, worker_id, created_by, lease_seconds, self.target_table, numeric_id))
            else:
                cur.execute('INSERT INTO seek_uuid_work_claim '
                            '(source_table,seekid_detail,claim_token,worker_id,claimed_by,state,claimed_at,heartbeat_at,expires_at) '
                            "VALUES (%s,%s,%s,%s,%s,'active',UTC_TIMESTAMP(),UTC_TIMESTAMP(),TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP()))",
                            (self.target_table, numeric_id, token, worker_id, created_by, lease_seconds))
        return token

    def _assert_claim(self, cur, numeric_id, token):
        from work_claims import ClaimLost
        cur.execute("SELECT claim_token FROM seek_uuid_work_claim WHERE source_table=%s AND seekid_detail=%s "
                    "AND claim_token=%s AND state='active' AND expires_at > UTC_TIMESTAMP() FOR UPDATE",
                    (self.target_table, numeric_id, token))
        if not cur.fetchone():
            raise ClaimLost('Candidate claim expired or changed owner; no proposal was submitted.')

    def renew_claim(self, numeric_id, token, lease_seconds):
        self.connection.ping(reconnect=True)
        self.verify_connection()
        with self.connection.cursor() as cur:
            cur.execute('UPDATE seek_uuid_work_claim SET heartbeat_at=UTC_TIMESTAMP(), '
                        'expires_at=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP()) '
                        "WHERE source_table=%s AND seekid_detail=%s AND claim_token=%s AND state='active' "
                        'AND expires_at > UTC_TIMESTAMP()', (lease_seconds, self.target_table, numeric_id, token))
            # A same-second renewal can have rowcount=0 even though ownership is valid.
            if cur.rowcount == 0:
                self._assert_claim(cur, numeric_id, token)

    def finish_claim(self, numeric_id, token, retry_seconds, outcome):
        self.connection.ping(reconnect=True)
        self.verify_connection()
        with self.connection.cursor() as cur:
            cur.execute("UPDATE seek_uuid_work_claim SET state='finished', last_result=%s, "
                        'expires_at=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP()) '
                        "WHERE source_table=%s AND seekid_detail=%s AND claim_token=%s AND state='active' "
                        'AND expires_at > UTC_TIMESTAMP()',
                        (outcome[:64], retry_seconds, self.target_table, numeric_id, token))

    def start_claim_lease(self, numeric_id, worker_id, created_by, settings, allow_rejected=False):
        from work_claims import ClaimLease
        import time
        started = time.monotonic()
        token = self.claim_candidate(numeric_id, worker_id, created_by, settings['lease_seconds'], allow_rejected)
        if token is None:
            return None
        # Expiry recovers a claim even if opening the heartbeat connection fails.
        heartbeat_repo = self.claim_connection()
        try:
            return ClaimLease(heartbeat_repo, numeric_id, token, settings, started)
        except BaseException:
            heartbeat_repo.close()
            raise

    def close(self):
        self.connection.close()

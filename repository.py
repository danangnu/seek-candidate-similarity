"""Local-only test repository. Candidate IDs and all other candidate fields stay intact."""
import hashlib
import json
import re
from contextlib import contextmanager
from profiles import canonical_uuid

TARGETS = {
    'seek_scrap': {'pk': 'id_pk', 'numeric': 'id', 'uuid': 'uuid'},
    'seek_scrap_detail': {'pk': 'id_detail', 'numeric': 'seekid_detail', 'uuid': 'seek_scrap_id'},
}


def target_fields(table):
    t = TARGETS[table]
    metadata = 'name, file, scrap_date' if table == 'seek_scrap' else 'NULL AS name, NULL AS file, NULL AS scrap_date'
    return f"{t['pk']} AS id_pk, {t['numeric']} AS id, {t['uuid']} AS uuid, {metadata}"

SCHEMA = [
'''CREATE TABLE IF NOT EXISTS seek_candidate_identity_map (
 numeric_seek_id BIGINT NOT NULL PRIMARY KEY,
 profile_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
 change_id CHAR(36) CHARACTER SET ascii NOT NULL,
 reviewed_by VARCHAR(100) NOT NULL,
 reviewed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB''',
'''CREATE TABLE IF NOT EXISTS seek_uuid_backfill_audit (
 change_id CHAR(36) CHARACTER SET ascii NOT NULL PRIMARY KEY,
 numeric_seek_id BIGINT NOT NULL,
 profile_uuid CHAR(36) CHARACTER SET ascii NOT NULL,
 before_rows LONGTEXT NOT NULL,
 evidence LONGTEXT NOT NULL,
 reviewed_by VARCHAR(100) NOT NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 reverted_at DATETIME NULL
) ENGINE=InnoDB''']


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
        if config['host'] not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('Local test build: database host must be loopback')
        if not re.fullmatch(r'seek_uuid_test_[A-Za-z0-9_]+', config['database']):
            raise ValueError('Local test build: database must start with seek_uuid_test_')
        self.database = config['database']
        self.target_table = config.get('target_table', 'seek_scrap_detail')
        if self.target_table not in TARGETS:
            raise ValueError('target_table must be seek_scrap_detail or seek_scrap')
        self.connection = pymysql.connect(host=config['host'], port=int(config.get('port', 3306)),
            user=config['user'], password=password, database=self.database, charset='utf8mb4',
            autocommit=True, cursorclass=pymysql.cursors.DictCursor, connect_timeout=10,
            read_timeout=60, write_timeout=60)
        with self.connection.cursor() as cur:
            cur.execute('SELECT DATABASE() AS db, @@hostname AS hostname, @@port AS port')
            self.server = cur.fetchone()
        self.preflight([self.target_table], check_type=False)

    def scrap_idle_settings(self, settings_id=1):
        from runtime_breaks import BreakSettingsError
        if type(settings_id) is not int or settings_id < 1:
            raise BreakSettingsError('breaks.settings_id must be a positive integer.')
        # Called outside candidate write transactions; long pauses can expire a connection.
        self.connection.ping(reconnect=True)
        with self.connection.cursor() as cur:
            cur.execute('SELECT idle_less_than, idle_less_than2, idle_more_than, idle_more_than2 '
                        'FROM seek_scrap_settings WHERE id=%s', (settings_id,))
            row = cur.fetchone()
        if row is None:
            raise BreakSettingsError('No local seek_scrap_settings row with id='+str(settings_id)+
                                     '. Run setup_local_scrap_settings.sql or set breaks.settings_id.')
        return row

    def preflight(self, tables, check_type=True, target_table=None):
        target = target_table or self.target_table
        with self.connection.cursor() as cur:
            for table in set(tables + [target]):
                cur.execute('SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s',
                            (self.database, table))
                row = cur.fetchone()
                if not row or row['ENGINE'].lower() != 'innodb':
                    raise ValueError(table+' must exist and use InnoDB')
            cur.execute('SELECT '+target_fields(target)+' FROM '+target+' LIMIT 0')
            if check_type:
                cur.execute('SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS '
                            'WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s',
                            (self.database, target, TARGETS[target]['uuid']))
                column = cur.fetchone()
                if not column or column['DATA_TYPE'].lower() not in ('char','varchar') or column['CHARACTER_MAXIMUM_LENGTH'] < 36:
                    raise ValueError(target+'.'+TARGETS[target]['uuid']+' must be a text column of at least 36 characters. '
                                     'For the uploaded detail schema run: python compare.py prepare-detail')

    def prepare_detail(self):
        """Explicit local-only preparation; DDL is separate from mapping transactions."""
        if self.target_table != 'seek_scrap_detail':
            raise ValueError('prepare-detail requires database.target_table = seek_scrap_detail')
        self.preflight(['seek_scrap_detail', 'seek_scrap'], check_type=False)
        with self.connection.cursor() as cur:
            # Refuse to alter a declared parent/child relationship.
            cur.execute('SELECT COUNT(*) AS n FROM information_schema.KEY_COLUMN_USAGE WHERE '
                        '(TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL) OR '
                        '(REFERENCED_TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME=%s AND REFERENCED_COLUMN_NAME=%s)',
                        (self.database,'seek_scrap_detail','seek_scrap_id',self.database,'seek_scrap_detail','seek_scrap_id'))
            if cur.fetchone()['n']:
                raise ValueError('seek_scrap_id participates in a foreign key; schema needs explicit redesign before UUID storage')
            cur.execute('SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS '
                        'WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s',
                        (self.database,'seek_scrap_detail','seek_scrap_id'))
            column=cur.fetchone()
            integer_types=('tinyint','smallint','mediumint','int','bigint')
            if column and (column['DATA_TYPE'].lower() in integer_types or
                           (column['DATA_TYPE'].lower() in ('char','varchar') and column['CHARACTER_MAXIMUM_LENGTH'] < 36)):
                cur.execute('ALTER TABLE seek_scrap_detail MODIFY COLUMN seek_scrap_id VARCHAR(255) NULL DEFAULT NULL')
                print('Changed local seek_scrap_detail.seek_scrap_id to VARCHAR(255). Existing values were preserved.')
            elif not column or column['DATA_TYPE'].lower() not in ('char','varchar'):
                raise ValueError('Unexpected seek_scrap_id data type; preparation stopped')
            # The uploaded table has a single-column unique key on seekid_detail.
            cur.execute('SELECT INDEX_NAME, NON_UNIQUE, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns_list '
                        'FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s '
                        'GROUP BY INDEX_NAME, NON_UNIQUE', (self.database,'seek_scrap_detail'))
            if not any(r['NON_UNIQUE']==0 and r['columns_list']=='seekid_detail' for r in cur.fetchall()):
                raise ValueError('A unique single-column index on seekid_detail is required; existing rows were not changed')
        self.preflight(['seek_scrap_detail'])
        with self.transaction() as cur:
            cur.execute('INSERT INTO seek_scrap_detail (seekid_detail) SELECT DISTINCT s.id FROM seek_scrap s '
                        'WHERE s.id > 0 AND NOT EXISTS (SELECT 1 FROM seek_scrap_detail d WHERE d.seekid_detail=s.id)')
            added=cur.rowcount
            cur.execute("SELECT COUNT(*) AS n FROM seek_scrap_detail WHERE seek_scrap_id IS NOT NULL AND TRIM(seek_scrap_id)<>''")
            preserved=cur.fetchone()['n']
        print('Prepared',added,'missing detail rows from local seek_scrap IDs; preserved',preserved,'nonblank target values.')
        print('Preparation does not copy UUIDs from seek_scrap or modify seek_scrap rows.')

    def initialize(self):
        self.preflight([self.target_table])
        with self.connection.cursor() as cur:
            for statement in SCHEMA:
                cur.execute(statement)
        self.preflight(['seek_candidate_identity_map', 'seek_uuid_backfill_audit'])

    def ids(self):
        self.preflight([self.target_table])
        t=TARGETS[self.target_table]
        with self.connection.cursor() as cur:
            cur.execute(f"SELECT DISTINCT {t['numeric']} AS id FROM {self.target_table} WHERE {t['numeric']} > 0 "
                        f"AND ({t['uuid']} IS NULL OR TRIM({t['uuid']})='') ORDER BY {t['numeric']}")
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
        lock = self.database+':uuid_backfill'
        with c.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s, 10) AS acquired', (lock,))
            if cur.fetchone()['acquired'] != 1:
                raise ValueError('Another backfill process holds the database lock')
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

    def apply(self, numeric_id, uid, expected_fingerprint, evidence, reviewer):
        from uuid import uuid4
        uid = canonical_uuid(uid)
        self.preflight(['seek_candidate_identity_map', 'seek_uuid_backfill_audit'])
        change_id = str(uuid4())
        table=self.target_table
        t=TARGETS[table]
        with self.transaction() as cur:
            cur.execute('SELECT '+target_fields(table)+f" FROM {table} WHERE {t['numeric']}=%s ORDER BY {t['pk']} FOR UPDATE", (numeric_id,))
            rows = cur.fetchall()
            if fingerprint(rows) != expected_fingerprint:
                raise ValueError('Candidate rows changed since comparison. Rerun this numeric ID.')
            cur.execute(f"SELECT {t['pk']} AS id_pk, {t['numeric']} AS id, {t['uuid']} AS uuid FROM {table} WHERE LOWER(TRIM({t['uuid']}))=%s FOR UPDATE", (uid,))
            check_mapping(rows, cur.fetchall(), numeric_id, uid)
            cur.execute('SELECT * FROM seek_candidate_identity_map WHERE numeric_seek_id=%s OR profile_uuid=%s FOR UPDATE',
                        (numeric_id, uid))
            mappings = cur.fetchall()
            if any(r['numeric_seek_id'] != numeric_id or r['profile_uuid'] != uid for r in mappings):
                raise ValueError('Conflicting approved identity mapping')
            changes = [r for r in rows if blank(r['uuid'])]
            if not changes:
                return None, 0
            cur.execute('INSERT INTO seek_uuid_backfill_audit '
                        '(change_id,numeric_seek_id,profile_uuid,before_rows,evidence,reviewed_by) VALUES (%s,%s,%s,%s,%s,%s)',
                        (change_id, numeric_id, uid, json.dumps({'target_table': table, 'rows': [{'id_pk': r['id_pk'], 'uuid': r['uuid']} for r in changes]}),
                         json.dumps(dict(evidence, storage_target=table+'.'+t['uuid'], numeric_key=t['numeric']), ensure_ascii=False), reviewer))
            if not mappings:
                cur.execute('INSERT INTO seek_candidate_identity_map (numeric_seek_id,profile_uuid,change_id,reviewed_by) VALUES (%s,%s,%s,%s)',
                            (numeric_id, uid, change_id, reviewer))
            for row in changes:
                cur.execute(f"UPDATE {table} SET {t['uuid']}=%s WHERE {t['pk']}=%s AND {t['numeric']}=%s AND ({t['uuid']} IS NULL OR TRIM({t['uuid']})='')",
                            (uid, row['id_pk'], numeric_id))
                if cur.rowcount != 1:
                    raise ValueError('Concurrent candidate update; all changes rolled back')
        return change_id, len(changes)

    def rollback(self, change_id):
        change_id = canonical_uuid(change_id)
        self.preflight(['seek_candidate_identity_map', 'seek_uuid_backfill_audit'], check_type=False)
        with self.transaction() as cur:
            cur.execute('SELECT * FROM seek_uuid_backfill_audit WHERE change_id=%s FOR UPDATE', (change_id,))
            audit = cur.fetchone()
            if not audit:
                raise ValueError('Change ID not found')
            if audit['reverted_at']:
                return 0
            payload = json.loads(audit['before_rows'])
            # Earlier releases stored a plain list and always wrote seek_scrap.uuid.
            table = 'seek_scrap' if isinstance(payload,list) else payload.get('target_table')
            if table not in TARGETS:
                raise ValueError('Unknown audit target; rollback refused')
            self.preflight([], target_table=table)
            t=TARGETS[table]
            before = payload if isinstance(payload,list) else payload['rows']
            for row in before:
                cur.execute(f"SELECT {t['numeric']} AS id, {t['uuid']} AS uuid FROM {table} WHERE {t['pk']}=%s FOR UPDATE", (row['id_pk'],))
                current = cur.fetchone()
                if not current or current['id'] != audit['numeric_seek_id'] or current['uuid'] != audit['profile_uuid']:
                    raise ValueError('A mapped row changed after backfill; rollback refused')
            # Keep the identity map if another active backfill depends on it.
            cur.execute('SELECT change_id FROM seek_uuid_backfill_audit WHERE numeric_seek_id=%s AND change_id<>%s '
                        'AND reverted_at IS NULL FOR UPDATE', (audit['numeric_seek_id'], change_id))
            other = cur.fetchall()
            for row in before:
                cur.execute(f"UPDATE {table} SET {t['uuid']}=%s WHERE {t['pk']}=%s", (row['uuid'], row['id_pk']))
            cur.execute('UPDATE seek_uuid_backfill_audit SET reverted_at=CURRENT_TIMESTAMP WHERE change_id=%s', (change_id,))
            if not other:
                cur.execute('DELETE FROM seek_candidate_identity_map WHERE numeric_seek_id=%s AND profile_uuid=%s AND change_id=%s',
                            (audit['numeric_seek_id'], audit['profile_uuid'], change_id))
        return len(before)

    def close(self):
        self.connection.close()

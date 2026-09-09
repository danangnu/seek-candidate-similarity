"""Execute repository SQL using a SQLite adapter. MariaDB locks/DDL need live testing."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from repository import Repository, fingerprint

UID='35011ba6-373e-8818-5731-45449246b18e'

class Cursor:
    def __init__(self, connection): self.cur=connection.db.cursor();self.synthetic=None
    def __enter__(self): return self
    def __exit__(self,*args): self.cur.close()
    def execute(self,sql,params=()):
        self.synthetic=None
        if 'GET_LOCK' in sql:self.synthetic=[{'acquired':1}];return
        if 'RELEASE_LOCK' in sql or sql.startswith('SET TRANSACTION'):self.synthetic=[];return
        self.cur.execute(sql.replace('%s','?').replace(' FOR UPDATE',''),params)
    def fetchone(self):
        if self.synthetic is not None:return self.synthetic[0] if self.synthetic else None
        row=self.cur.fetchone();return dict(row) if row else None
    def fetchall(self):
        return self.synthetic if self.synthetic is not None else [dict(r) for r in self.cur.fetchall()]
    @property
    def rowcount(self):return self.cur.rowcount

class Connection:
    def __init__(self):
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
    def cursor(self):return Cursor(self)
    def begin(self):self.db.execute('BEGIN')
    def commit(self):self.db.commit()
    def rollback(self):self.db.rollback()
    def close(self):self.db.close()

class DetailRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.repo=Repository.__new__(Repository)
        self.repo.database='seek_uuid_test_unit';self.repo.target_table='seek_scrap_detail'
        self.repo.connection=Connection();self.repo.preflight=lambda *a,**k:None
        self.db=self.repo.connection.db
        self.db.executescript('''
        CREATE TABLE seek_scrap (id_pk INTEGER PRIMARY KEY, id INTEGER, uuid TEXT, name TEXT, file TEXT, scrap_date TEXT);
        CREATE TABLE seek_scrap_detail (id_detail INTEGER PRIMARY KEY, seekid_detail INTEGER UNIQUE, uuid TEXT, match_path_found TEXT);
        CREATE TABLE seek_candidate_identity_map (numeric_seek_id INTEGER PRIMARY KEY, profile_uuid TEXT UNIQUE, change_id TEXT, reviewed_by TEXT);
        CREATE TABLE seek_uuid_backfill_audit (change_id TEXT PRIMARY KEY, numeric_seek_id INTEGER, profile_uuid TEXT, before_rows TEXT, evidence TEXT, reviewed_by TEXT, reverted_at TEXT);
        INSERT INTO seek_scrap VALUES (1,42,'existing-main-value','Alex Example','',NULL);
        INSERT INTO seek_scrap_detail VALUES (100,42,NULL,'preserve matching metadata');
        ''')
    def tearDown(self):self.repo.close()
    def apply(self):
        return self.repo.apply(42,UID,fingerprint(self.repo.rows(42)),{'review_mode':'automatic'},'unit-test')
    def test_pending_and_write_use_detail_columns_only(self):
        self.assertEqual(self.repo.ids(),[42])
        change,count=self.apply();self.assertEqual(count,1)
        self.assertEqual(self.db.execute('SELECT uuid FROM seek_scrap_detail').fetchone()[0],UID)
        self.assertEqual(self.db.execute('SELECT uuid FROM seek_scrap').fetchone()[0],'existing-main-value')
        self.assertEqual(self.db.execute('SELECT match_path_found FROM seek_scrap_detail').fetchone()[0],'preserve matching metadata')
        audit=self.db.execute('SELECT before_rows,evidence FROM seek_uuid_backfill_audit').fetchone()
        self.assertEqual(json.loads(audit[0])['target_table'],'seek_scrap_detail')
        self.assertEqual(json.loads(audit[1])['numeric_key'],'seekid_detail')
        self.assertEqual(self.repo.ids(),[])
    def test_detail_rollback_restores_only_detail_uuid(self):
        change,_=self.apply();self.assertEqual(self.repo.rollback(change),1)
        self.assertIsNone(self.db.execute('SELECT uuid FROM seek_scrap_detail').fetchone()[0])
        self.assertEqual(self.db.execute('SELECT uuid FROM seek_scrap').fetchone()[0],'existing-main-value')
        self.assertEqual(self.repo.rollback(change),0)
    def test_nonblank_old_integer_link_not_overwritten(self):
        self.db.execute("UPDATE seek_scrap_detail SET uuid='12345'")
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual(self.db.execute('SELECT uuid FROM seek_scrap_detail').fetchone()[0],'12345')
    def test_other_numeric_id_uuid_conflict(self):
        self.db.execute('INSERT INTO seek_scrap_detail (id_detail,seekid_detail,uuid) VALUES (101,43,?)',(UID,))
        with self.assertRaises(ValueError):self.apply()
        self.assertIsNone(self.db.execute('SELECT uuid FROM seek_scrap_detail WHERE seekid_detail=42').fetchone()[0])
    def test_failed_detail_update_rolls_back_mapping_and_audit(self):
        self.db.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON seek_scrap_detail BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.apply()
        for table in ['seek_candidate_identity_map','seek_uuid_backfill_audit']:
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.assertIsNone(self.db.execute('SELECT uuid FROM seek_scrap_detail').fetchone()[0])
    def test_previous_plain_list_audit_rolls_back_old_main_table(self):
        old_change='11111111-1111-1111-1111-111111111111'
        self.db.execute('UPDATE seek_scrap SET uuid=?',(UID,))
        self.db.execute('INSERT INTO seek_uuid_backfill_audit VALUES (?,?,?,?,?,?,NULL)',
            (old_change,42,UID,json.dumps([{'id_pk':1,'uuid':None}]),'{}','unit-test'))
        self.assertEqual(self.repo.rollback(old_change),1)
        self.assertIsNone(self.db.execute('SELECT uuid FROM seek_scrap').fetchone()[0])
        self.assertIsNone(self.db.execute('SELECT uuid FROM seek_scrap_detail').fetchone()[0])
    def test_intervening_change_blocks_rollback(self):
        change,_=self.apply()
        self.db.execute("UPDATE seek_scrap_detail SET uuid='changed-after-save'")
        with self.assertRaises(ValueError):self.repo.rollback(change)
        self.assertEqual(self.db.execute('SELECT uuid FROM seek_scrap_detail').fetchone()[0],'changed-after-save')

if __name__=='__main__':unittest.main()

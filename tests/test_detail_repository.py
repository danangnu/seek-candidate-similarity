"""Execute proposal SQL using SQLite; MariaDB-specific locking needs live validation."""
import copy
import json
from pathlib import Path
import re
import sqlite3
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from repository import Repository, fingerprint, review_actors
from review_schema import schema_statements
from profiles import extract_candidate_profile
from test_backfill import FIXTURE

UID='35011ba6-373e-8818-5731-45449246b18e'


def sqlite_ddl(sql):
    sql=re.sub(r'BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY','INTEGER PRIMARY KEY AUTOINCREMENT',sql)
    sql=re.sub(r' CHARACTER SET ascii COLLATE ascii_bin','',sql)
    sql=re.sub(r'UNIQUE KEY \w+ \(([^)]+)\)',r'UNIQUE (\1)',sql)
    sql=re.sub(r'^ KEY .*,\n','',sql,flags=re.M)
    return re.sub(r' ENGINE=InnoDB.*$','',sql,flags=re.S)


class Cursor:
    def __init__(self, connection): self.connection=connection;self.cur=connection.db.cursor();self.synthetic=None
    def __enter__(self): return self
    def __exit__(self,*args): self.cur.close()
    def execute(self,sql,params=()):
        self.connection.statements.append(sql)
        self.synthetic=None
        if 'GET_LOCK' in sql:self.synthetic=[{'acquired':1}];return
        if 'RELEASE_LOCK' in sql or sql.startswith('SET TRANSACTION'):self.synthetic=[];return
        if sql.startswith('CREATE TABLE'):sql=sqlite_ddl(sql)
        self.cur.execute(sql.replace('%s','?').replace(' FOR UPDATE',''),params)
    def fetchone(self):
        if self.synthetic is not None:return self.synthetic[0] if self.synthetic else None
        row=self.cur.fetchone();return dict(row) if row else None
    def fetchall(self):
        return self.synthetic if self.synthetic is not None else [dict(r) for r in self.cur.fetchall()]
    @property
    def rowcount(self):return self.cur.rowcount
    @property
    def lastrowid(self):return self.cur.lastrowid


class Connection:
    def __init__(self):
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.db.create_function('CHAR_LENGTH',1,len)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.statements=[]
    def cursor(self):return Cursor(self)
    def begin(self):self.db.execute('BEGIN')
    def commit(self):self.db.commit()
    def rollback(self):self.db.rollback()
    def close(self):self.db.close()
    def ping(self,**kwargs):pass


class ReviewRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.repo=Repository.__new__(Repository)
        self.repo.database='seek_uuid_test_unit';self.repo.target_table='seek_scrap_detail'
        self.repo.connection=Connection();self.repo.preflight=Mock();self.repo.verify_connection=Mock()
        self.db=self.repo.connection.db
        self.db.executescript('''
        CREATE TABLE seek_scrap (id_pk INTEGER PRIMARY KEY, id INTEGER, uuid TEXT, name TEXT, file TEXT, scrap_date TEXT, date_updated TEXT);
        CREATE TABLE seek_scrap_detail (id_detail INTEGER PRIMARY KEY, seekid_detail INTEGER UNIQUE, seek_scrap_id INTEGER, match_path_found TEXT);
        INSERT INTO seek_scrap VALUES (1,42,'existing-main-value','Alex Example','',NULL,'2026-01-01');
        INSERT INTO seek_scrap_detail VALUES (100,42,12345,'preserve matching metadata');
        ''')
        self.repo.initialize()
        self.profile=extract_candidate_profile(FIXTURE)
        self.evidence={'old_profile': self.profile, 'selected': {'uuid':UID,'profile':copy.deepcopy(self.profile)}}
    def tearDown(self):self.repo.close()
    def submit(self, evidence=None, uid=UID, numeric=42):
        return self.repo.submit_proposal(numeric,uid,fingerprint(self.repo.rows(numeric)),evidence or self.evidence,'collector','reviewer-one')
    def proposal(self):return dict(self.db.execute('SELECT * FROM seek_uuid_match_review').fetchone())
    def test_detail_queue_latest_profile_date_first_deduplicated_nulls_last(self):
        self.db.executemany('INSERT INTO seek_scrap_detail VALUES (?,?,?,NULL)',
                            [(101,43,100),(102,44,101),(103,45,102),(104,46,103)])
        self.db.executemany('INSERT INTO seek_scrap (id_pk,id,date_updated) VALUES (?,?,?)',
                            [(2,43,'2026-09-10'),(3,43,'2020-01-01'),(4,44,'2026-09-09'),
                             (5,45,None),(6,42,'2026-09-09')])
        # 46 has no seek_scrap row; keep it after dated people. Ties use numeric ID.
        self.assertEqual(self.repo.ids(),[43,42,44,45,46])
        self.submit(numeric=43)
        self.assertEqual(self.repo.ids(),[42,44,45,46])

    def test_alternate_main_source_uses_latest_snapshot_date(self):
        self.repo.target_table='seek_scrap'
        self.db.executemany('INSERT INTO seek_scrap (id_pk,id,date_updated) VALUES (?,?,?)',
                            [(2,43,'2026-09-10'),(3,43,'2020-01-01'),(4,44,None)])
        self.assertEqual(self.repo.ids(),[43,42,44])

    def test_pending_proposal_has_snapshots_assignment_and_no_approval(self):
        self.assertEqual(self.repo.ids(),[42])
        result=self.submit();row=self.proposal()
        self.assertTrue(result['created']);self.assertEqual(row['status'],'pending')
        self.assertEqual(row['created_by'],'collector');self.assertEqual(row['assigned_reviewer'],'reviewer-one')
        for key in ('reviewed_by','reviewed_at','review_reason','applied_at'):self.assertIsNone(row[key])
        self.assertEqual(json.loads(row['numeric_profile_snapshot']),self.profile)
        self.assertEqual(json.loads(row['uuid_profile_snapshot']),self.profile)
        self.assertTrue(json.loads(row['comparison_evidence'])['comparison']['profile_content_equal'])
        self.assertEqual(row['row_version'],1)
        self.assertEqual(self.db.execute('SELECT seek_scrap_id FROM seek_scrap_detail').fetchone()[0],12345)
        self.assertEqual(self.db.execute('SELECT uuid FROM seek_scrap').fetchone()[0],'existing-main-value')
        self.assertEqual(self.repo.ids(),[])
        event=dict(self.db.execute('SELECT * FROM seek_uuid_match_review_history').fetchone())
        self.assertEqual(event['action'],'submitted');self.assertEqual(event['performed_by'],'collector')
    def test_only_review_tables_ever_receive_writes(self):
        def guard(action,table,*rest):
            if action in (sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_INSERT,sqlite3.SQLITE_DELETE) and table in ('seek_scrap','seek_scrap_detail'):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.db.set_authorizer(guard)
        self.submit()
        writes=[s for s in self.repo.connection.statements if s.startswith(('INSERT','UPDATE','DELETE','ALTER'))]
        self.assertEqual(len(writes),2)
        self.assertTrue(all(s.startswith('INSERT INTO seek_uuid_match_review') for s in writes))
        self.assertFalse(hasattr(self.repo,'apply'));self.assertFalse(hasattr(self.repo,'rollback'));self.assertFalse(hasattr(self.repo,'prepare_detail'))
    def test_repeated_submission_keeps_assignment_and_one_history_event(self):
        first=self.submit()
        second=self.repo.submit_proposal(42,UID,fingerprint(self.repo.rows(42)),self.evidence,'another','different-reviewer')
        self.assertEqual(second['review_id'],first['review_id']);self.assertFalse(second['created'])
        self.assertEqual(second['assigned_reviewer'],'reviewer-one')
        self.assertEqual(self.proposal()['assigned_reviewer'],'reviewer-one')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM seek_uuid_match_review_history').fetchone()[0],1)
    def reject(self):
        self.db.execute("UPDATE seek_uuid_match_review SET status='rejected',reviewed_by='human',reviewed_at=CURRENT_TIMESTAMP,review_reason='Different candidate',row_version=2")
    def test_replay_does_not_reset_rejected_review(self):
        self.submit();self.reject();result=self.submit()
        self.assertFalse(result['created']);self.assertEqual(result['status'],'rejected')
        self.assertEqual(self.proposal()['reviewed_by'],'human');self.assertEqual(self.repo.ids(),[])
    def test_active_numeric_proposal_prevents_different_proposal(self):
        self.submit();changed=copy.deepcopy(self.evidence)
        uid='11111111-1111-1111-1111-111111111111';changed['selected']['uuid']=uid
        with self.assertRaisesRegex(ValueError,'already has'):self.submit(changed,uid)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM seek_uuid_match_review').fetchone()[0],1)
    def test_rejected_changed_evidence_allows_explicit_resubmission(self):
        self.submit();self.reject();changed=copy.deepcopy(self.evidence)
        for profile in (changed['old_profile'],changed['selected']['profile']):profile['profile_content']+=' New licence'
        self.assertTrue(self.submit(changed)['created'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM seek_uuid_match_review').fetchone()[0],2)
    def test_shared_uuid_can_be_proposed_for_review_without_assigning_identity(self):
        self.submit();self.db.execute('INSERT INTO seek_scrap_detail VALUES (101,43,6789,NULL)')
        self.assertTrue(self.submit(numeric=43)['created'])
        self.assertEqual(self.db.execute('SELECT seek_scrap_id FROM seek_scrap_detail WHERE seekid_detail=43').fetchone()[0],6789)
    def test_history_failure_rolls_back_proposal(self):
        self.db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON seek_uuid_match_review_history BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.submit()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM seek_uuid_match_review').fetchone()[0],0)
    def test_source_change_blocks_stale_submission(self):
        old=fingerprint(self.repo.rows(42));self.db.execute("UPDATE seek_scrap_detail SET seek_scrap_id=999")
        with self.assertRaisesRegex(ValueError,'changed'):
            self.repo.submit_proposal(42,UID,old,self.evidence,'collector')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM seek_uuid_match_review').fetchone()[0],0)
    def test_missing_source_blocked(self):
        with self.assertRaises(ValueError):self.submit(numeric=999)
    def test_name_only_and_forged_exact_flag_blocked(self):
        bad=copy.deepcopy(self.evidence);bad['selected']['profile']['work_history']=[]
        bad['selected']['profile']['education']=[];bad['selected']['evidence']={'profile_content_equal':True}
        with self.assertRaises(ValueError):self.submit(bad)
    def test_missing_full_snapshot_blocked(self):
        bad=copy.deepcopy(self.evidence);bad['old_profile']['profile_content_scope']='sections_only'
        with self.assertRaises(ValueError):self.submit(bad)
    def test_database_constraints_require_real_decision_metadata(self):
        self.submit()
        for sql in ["UPDATE seek_uuid_match_review SET status='approved'",
                    "UPDATE seek_uuid_match_review SET reviewed_by='collector'",
                    "UPDATE seek_uuid_match_review SET status='invented'",
                    "UPDATE seek_uuid_match_review SET numeric_profile_snapshot='invalid json'"]:
            with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError):self.db.execute(sql)
    def test_schema_init_creates_only_two_review_tables(self):
        self.assertEqual(len(schema_statements()),2)
        self.repo.initialize()
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('seek_candidate_identity_map',tables);self.assertNotIn('seek_uuid_backfill_audit',tables)
    def test_legacy_operator_does_not_become_reviewer(self):
        self.assertEqual(review_actors({'reviewer':'collector'}),('collector',None))
        self.assertEqual(review_actors({'created_by':'collector','reviewer':'old','review':{'assigned_reviewer':'human'}}),('collector','human'))
        for config in ({'created_by':''},{'created_by':'x','review':[]},{'created_by':'x','review':{'assigned_reviewer':'\n'}}):
            with self.assertRaises(ValueError):review_actors(config)

if __name__=='__main__':unittest.main()

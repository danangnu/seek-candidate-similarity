import argparse
import contextlib
import io
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from network_config import database_options, validate_endpoint, ollama_json, check_ollama
from profiles import compare_profiles_with_ollama, extract_candidate_profile
from repository import Repository
from compare import check_connections, run
from test_backfill import FIXTURE, UID


class RemoteDatabaseTest(unittest.TestCase):
    def config(self):
        return dict(host='db.internal.example', port=3307, user='seek_backfill', database='trackitlive')

    def test_remote_host_database_port_and_password_are_passed_through(self):
        options=database_options(self.config(),'test-secret')
        self.assertEqual(options['host'],'db.internal.example')
        self.assertEqual(options['port'],3307)
        self.assertEqual(options['database'],'trackitlive')
        self.assertEqual(options['password'],'test-secret')
        self.assertNotIn('ssl',options)

    def test_tls_context_verifies_certificate_and_hostname(self):
        config=self.config();config['tls']={'enabled':True}
        context=database_options(config,'x')['ssl']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)

    def test_tls_not_silently_disabled_when_certificates_configured(self):
        config=self.config();config['tls']={'ca_file':'server-ca.pem'}
        with self.assertRaises(ValueError):database_options(config,'x')

    def test_invalid_host_port_and_timeout_rejected(self):
        for key,value in [('host','https://db.example'),('port',0),('port',True),
                          ('connect_timeout',float('nan')),('database','db; DROP TABLE x')]:
            config=self.config();config[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):database_options(config,'x')

    def test_wrong_database_or_missing_required_tls_is_rejected(self):
        for name,cipher in [('other_db','TLS_AES_256_GCM_SHA384'),('trackitlive','')]:
            repo=Repository.__new__(Repository);repo.database='trackitlive';repo.require_tls=True
            cur=Mock();cur.fetchone.side_effect=[dict(db=name,hostname='server-a',port=3306),{'Value':cipher}]
            ctx=Mock();ctx.__enter__=Mock(return_value=cur);ctx.__exit__=Mock(return_value=False)
            repo.connection=Mock();repo.connection.cursor.return_value=ctx
            with self.subTest(name=name,cipher=cipher),self.assertRaises(ValueError):repo.verify_connection()


class OllamaHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.server.requests.append(('GET',self.path,None))
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'models':[{'name':'llama3.1:8b'}]}).encode())
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.server.requests.append(('POST',self.path,data))
                if self.server.redirect:
                    self.send_response(307);self.send_header('Location','/other-service');self.end_headers();return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                verdict=dict(is_same_person=True,confidence=.8,reason='Synthetic fixture comparison')
                self.wfile.write(json.dumps({'message':{'content':json.dumps(verdict)}}).encode())
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.server.requests=[];cls.server.redirect=False
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.endpoint='http://127.0.0.1:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=2)

    def setUp(self):
        self.server.requests.clear();self.server.redirect=False

    def test_model_check_sends_get_without_candidate_data(self):
        result=check_ollama(endpoint=self.endpoint,model='llama3.1:8b')
        self.assertTrue(result['model_available'])
        self.assertEqual(self.server.requests,[('GET','/api/tags',None)])

    def test_missing_model_rejected(self):
        with self.assertRaises(ValueError):check_ollama(endpoint=self.endpoint,model='missing-model')

    def test_profile_post_and_model_verdict_roundtrip(self):
        profile={'candidate_name':'Synthetic Example'}
        result=compare_profiles_with_ollama(profile,profile,endpoint=self.endpoint)
        self.assertTrue(result['is_same_person'])
        method,path,payload=self.server.requests[0]
        self.assertEqual((method,path),('POST','/api/chat'))
        self.assertEqual(json.loads(payload['messages'][1]['content'])['old_profile'],profile)
        self.assertFalse(payload['stream'])

    def test_redirect_does_not_forward_profile_payload(self):
        self.server.redirect=True
        with self.assertRaises(ValueError):ollama_json(self.endpoint,'/api/chat',{'synthetic':'record'})
        self.assertEqual(len(self.server.requests),1)

    def test_remote_url_and_ipv6_allowed_but_embedded_secrets_rejected(self):
        for url in ('http://ollama.internal.example:11434','https://ai.internal.example/ollama','http://[::1]:11434'):
            self.assertEqual(validate_endpoint(url),url)
        for url in ('http://user:secret@ai.example','file:///tmp/x','http://ai.example?key=secret','http://ai.example#x'):
            with self.assertRaises(ValueError):validate_endpoint(url)

    def test_optional_gateway_key_is_header_only(self):
        response=Mock();response.read.return_value=b'{}'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        with patch.dict('os.environ',{'TEST_OLLAMA_TOKEN':'unit-test-token'}),patch('network_config.build_opener',return_value=opener):
            ollama_json('https://ai.internal.example','/api/chat',{'a':1},api_key_env='TEST_OLLAMA_TOKEN')
        request=opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'),'Bearer unit-test-token')
        self.assertNotIn('unit-test-token',request.full_url)
        self.assertNotIn(b'unit-test-token',request.data)

    def test_gateway_key_requires_https(self):
        with patch.dict('os.environ',{'TEST_OLLAMA_TOKEN':'test-token'}),self.assertRaises(ValueError):
            ollama_json(self.endpoint,'/api/chat',{},api_key_env='TEST_OLLAMA_TOKEN')
        self.assertEqual(self.server.requests,[])


class RemoteWorkflowTest(unittest.TestCase):
    def test_explicit_ollama_advice_does_not_override_exact_content_save(self):
        repo=Mock();repo.database='trackitlive';repo.target_table='seek_scrap_detail'
        repo.ids.return_value=[42]
        repo.rows.return_value=[dict(id_pk=1,id=42,uuid=None,name=None,file=None,scrap_date=None)]
        repo.scrap_idle_settings.return_value=dict(idle_less_than=0,idle_less_than2=0,idle_more_than=0,idle_more_than2=0)
        repo.apply.return_value=('change-test',1)
        profile=extract_candidate_profile(FIXTURE)
        browser=Mock();browser.numeric_profile.return_value=(profile,{'mode':'live_numeric'})
        browser.search.return_value=({UID:'https://au.employer.seek.com/talentsearch/profiles/'+UID},True,'')
        browser.profile.return_value=profile
        args=argparse.Namespace(apply=True,auto_save=True,with_ollama=True,no_ollama=False,id=42,csv=None,limit=1,uuid=None)
        with tempfile.TemporaryDirectory() as folder:
            config={'reviewer':'test','report_dir':folder,'ollama':{'endpoint':'http://ai.internal.example:11434','model':'llama3.1:8b'}}
            with patch('seek_browser.SeekBrowser',return_value=browser),patch('compare.compare_profiles_with_ollama',return_value={'is_same_person':False,'confidence':.8,'reason':'Advisory'}) as model,contextlib.redirect_stdout(io.StringIO()):
                run(args,config,repo)
            model.assert_called_once_with(profile,profile,endpoint='http://ai.internal.example:11434',model='llama3.1:8b')
            self.assertEqual(json.loads(next(Path(folder).rglob('42.json')).read_text())['status'],'saved')
        repo.apply.assert_called_once()

    def test_check_connections_never_mutates_database_or_opens_browser(self):
        repo=Mock();repo.target_table='seek_scrap_detail'
        repo.scrap_idle_settings.return_value=dict(idle_less_than=30,idle_less_than2=300,idle_more_than=5,idle_more_than2=25)
        with patch('compare.check_ollama') as model,patch('seek_browser.SeekBrowser') as browser,contextlib.redirect_stdout(io.StringIO()):
            check_connections({'ollama':{'endpoint':'http://ai.internal.example:11434'}},repo)
        model.assert_called_once();browser.assert_not_called()
        repo.apply.assert_not_called();repo.initialize.assert_not_called();repo.prepare_detail.assert_not_called()

    def test_database_schema_failure_does_not_skip_ollama_diagnostic(self):
        repo=Mock();repo.target_table='seek_scrap_detail';repo.preflight.side_effect=ValueError('Wrong column type')
        repo.scrap_idle_settings.return_value=dict(idle_less_than=0,idle_less_than2=0,idle_more_than=0,idle_more_than2=0)
        with patch('compare.check_ollama') as model,contextlib.redirect_stdout(io.StringIO()),self.assertRaises(ValueError):
            check_connections({},repo)
        model.assert_called_once()


if __name__=='__main__':unittest.main()

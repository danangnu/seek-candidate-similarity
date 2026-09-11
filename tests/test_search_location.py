"""WA autosuggest selection, confirmed-query reuse and safe failure diagnostics."""
import contextlib
import io
import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit
from seek_browser import SeekBrowser, BrowserReadError, BASE, LOCATION_ROOT
from work_claims import ClaimLost

LABEL='Western Australia WA'
QUERY='Western Australia'
UID='64113200-3264-e1d5-48ea-539900000000'
# Opaque fixture value, deliberately not an invented live SEEK location ID.
LOCATION_VALUE='Western Australia WA fixture-location'


class Element:
    def __init__(self, text='', attrs=None, click=None, children=None):
        self.text=text;self.attrs=attrs or {};self.on_click=click;self.children=children or []
        self.typed=[]
    def is_displayed(self):return True
    def is_enabled(self):return True
    def get_attribute(self,key):return self.attrs.get(key)
    def click(self):
        if self.on_click:self.on_click()
    def clear(self):self.typed=[]
    def send_keys(self,text):self.typed.append(text)
    def find_elements(self,by,value):return self.children


class Wait:
    def until(self,callback):
        for _ in range(3):
            result=callback(None)
            if result:return result
        raise TimeoutError('https://private.example/?serviceToken=SECRET')


class Driver:
    title='Talent Search'
    def __init__(self, option_label=LABEL):
        self.current_url=BASE+'/talentsearch/keyword';self.selected=[];self.visits=[]
        self.field=Element(attrs={'aria-controls':'location-menu'})
        self.menu=Element(children=[Element(text=option_label,click=self.select)])
        self.clear=Element(click=lambda:self.selected.clear())
    def select(self):
        self.selected=[LABEL]
        query=parse_qs(urlsplit(self.current_url).query);query['locations']=[LOCATION_VALUE]
        self.current_url=BASE+'/talentsearch/keyword?'+urlencode(query,doseq=True)
    def execute_script(self,script,field,value):
        field.attrs['value']=value
        field.typed.append(value)
        return True
    def get(self,url):
        self.current_url=url;self.visits.append(url)
        self.selected=[LABEL] if parse_qs(urlsplit(url).query).get('locations')==[LOCATION_VALUE] else []
    def find_elements(self,by,value):
        if value==LOCATION_ROOT+' input[role="combobox"]':return [self.field]
        if value.startswith(LOCATION_ROOT+' button'):
            return [Element(attrs={'aria-label':'Clear '+name}) for name in self.selected]
        if value=='location-menu':return [self.menu]
        if value=='location-filter-clear-multiselect-autosuggest-input':return [self.clear]
        return []


class SearchLocationTest(unittest.TestCase):
    def browser(self, option_label=LABEL):
        b=SeekBrowser.__new__(SeekBrowser);b.config={};b.delay_seconds=0;b.driver=Driver(option_label);b.wait=Wait()
        b.page_snapshot=Mock(return_value={'total':1,'links':{UID:BASE+'/talentsearch/profiles/'+UID+'?serviceToken=SECRET'},
                                         'names':{UID:'Waqar Ali'},'next':[]})
        b.close_intro=Mock()
        return b

    def test_default_WA_selected_through_autosuggest_then_reloaded(self):
        b=self.browser()
        with contextlib.redirect_stdout(io.StringIO()):links,complete,_=b.search('Waqar Ali')
        self.assertTrue(complete);self.assertIn(UID,links)
        self.assertEqual(len(b.driver.visits),2)
        self.assertNotIn('locations',parse_qs(urlsplit(b.driver.visits[0]).query))
        self.assertEqual(parse_qs(urlsplit(b.driver.visits[1]).query)['locations'],[LOCATION_VALUE])
        self.assertEqual(b.driver.field.typed,[QUERY])
        self.assertTrue(b.last_search_scan['location_verified'])
        self.assertNotIn('SECRET',json.dumps(b.last_search_scan))

    def test_next_name_reuses_real_location_value(self):
        b=self.browser()
        with contextlib.redirect_stdout(io.StringIO()):
            b.search('Waqar Ali');b.search('Another Candidate')
        self.assertEqual(len(b.driver.visits),3)
        query=parse_qs(urlsplit(b.driver.visits[-1]).query)
        self.assertEqual(query['locations'],[LOCATION_VALUE]);self.assertEqual(query['searchQuery'],['Another Candidate'])

    def test_wrong_option_does_not_fall_back_to_unfiltered_search(self):
        b=self.browser(option_label='Perth WA')
        with self.assertRaises(BrowserReadError) as caught:b.search('Waqar Ali')
        self.assertIn('exact Western Australia WA option',str(caught.exception))
        b.page_snapshot.assert_not_called()
        self.assertNotIn('SECRET',json.dumps(caught.exception.diagnostics)+str(caught.exception))

    def test_wrong_chip_after_navigation_stops_before_collecting_results(self):
        b=self.browser();b._resolved_location=(LABEL,'invalid-fixture-value')
        with self.assertRaises(BrowserReadError):b.search('Waqar Ali')
        b.page_snapshot.assert_not_called()

    def test_location_text_elsewhere_is_not_a_selected_chip(self):
        b=self.browser();b.driver.selected=[]
        self.assertFalse(b.location_selected(LABEL))
        b.driver.selected=[LABEL,'Perth WA']
        self.assertFalse(b.location_selected(LABEL))

    def test_null_location_preserves_all_Australia_search(self):
        b=self.browser();b.config['search_location']=None
        with contextlib.redirect_stdout(io.StringIO()):b.search('Waqar Ali')
        self.assertEqual(len(b.driver.visits),1);self.assertEqual(b.driver.field.typed,[])
        self.assertNotIn('locations',parse_qs(urlsplit(b.driver.visits[0]).query))

    def test_invalid_location_rejected(self):
        b=self.browser()
        for value in ('',False,123,[],'x'*121):
            b.config['search_location']=value
            with self.subTest(value=value),self.assertRaises(ValueError):b.search_location()

    def test_claim_loss_is_not_converted_into_browser_timeout(self):
        b=self.browser();b.claim_guard=Mock(side_effect=ClaimLost('expired'))
        with self.assertRaises(ClaimLost):b.search('Waqar Ali')
        self.assertEqual(b.driver.visits,[])

    def test_timeout_diagnostics_classify_verification_without_url_tokens(self):
        b=self.browser();b.driver.current_url=BASE+'/talentsearch/profiles/'+UID+'?serviceToken=SECRET'
        b.driver.title='Just a moment...'
        with self.assertRaises(BrowserReadError) as caught:b.wait_for(lambda _:False,'loading the complete UUID profile')
        self.assertEqual(caught.exception.diagnostics['page_kind'],'verification')
        self.assertNotIn('SECRET',json.dumps(caught.exception.diagnostics)+str(caught.exception))

    def test_incomplete_search_keeps_progress(self):
        b=self.browser();b.config.update(search_location=None,max_search_pages=1)
        b.page_snapshot.return_value['total']=1000;b.page_snapshot.return_value['next']=[Element()]
        links,complete,reason=b.search('Waqar Ali')
        self.assertFalse(complete);self.assertIn('page limit',reason)
        self.assertEqual(b.last_search_scan['cards_checked'],1)
        self.assertEqual(b.last_search_scan['total_results'],1000)

    def test_numeric_profile_timeout_report_has_safe_reason_and_stage(self):
        import argparse
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from compare import run
        repo=Mock();repo.database='test';repo.target_table='seek_scrap_detail'
        repo.rows.return_value=[{'id':563879489,'id_pk':1,'name':None,'uuid':None,'file':None,'scrap_date':None}]
        repo.scrap_idle_settings.return_value=dict(idle_less_than=0,idle_less_than2=0,idle_more_than=0,idle_more_than2=0)
        browser=Mock();browser.numeric_profile.side_effect=BrowserReadError('Timed out loading the complete numeric profile',{'stage':'loading the complete numeric profile','page_kind':'profile'})
        args=argparse.Namespace(apply=False,auto_save=True,id=563879489,csv=None,limit=1,no_ollama=True,uuid=None)
        with tempfile.TemporaryDirectory() as folder,patch('seek_browser.SeekBrowser',return_value=browser),contextlib.redirect_stdout(io.StringIO()):
            run(args,{'created_by':'staff','report_dir':folder},repo)
            report=json.loads(next(Path(folder).rglob('563879489.json')).read_text())
        self.assertEqual(report['status'],'unresolved')
        self.assertIn('complete numeric profile',report['reason'])
        self.assertEqual(report['browser_diagnostics']['stage'],'loading the complete numeric profile')
        repo.submit_proposal.assert_not_called();browser.close.assert_called_once()

    def test_corrupted_location_input_retried_before_option_selection(self):
        b=self.browser();calls=[]
        def set_value(script,field,value):
            calls.append(value)
            field.attrs['value']='etr utai A' if len(calls)==1 else value
            return True
        b.driver.execute_script=set_value
        with contextlib.redirect_stdout(io.StringIO()):links,complete,_=b.search('Waqar Ali')
        self.assertTrue(complete);self.assertIn(UID,links)
        self.assertEqual(calls,[QUERY,QUERY]);self.assertTrue(b.location_selected(LABEL))

    def test_persistently_corrupted_input_never_collects_results(self):
        b=self.browser()
        def set_value(script,field,value):
            field.attrs['value']='etr utai A'
            return True
        b.driver.execute_script=Mock(side_effect=set_value)
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(BrowserReadError) as caught:
            b.search('Waqar Ali')
        self.assertIn('Could not enter the complete Western Australia location',str(caught.exception))
        self.assertEqual(b.driver.execute_script.call_count,3)
        b.page_snapshot.assert_not_called()

    def test_input_is_reacquired_after_delay(self):
        b=self.browser();old=b.driver.field
        def pause(action):
            if action=='entering '+QUERY:
                b.driver.field=Element(attrs={'aria-controls':'location-menu'})
        b.pause=pause
        with contextlib.redirect_stdout(io.StringIO()):b.search('Waqar Ali')
        self.assertNotIn('value',old.attrs)
        self.assertEqual(b.driver.field.attrs['value'],QUERY)

    def test_option_is_reacquired_after_selection_delay(self):
        b=self.browser();old_click=Mock();b.driver.menu.children[0].on_click=old_click
        def pause(action):
            if action=='selecting '+LABEL:
                b.driver.menu=Element(children=[Element(text=LABEL,click=b.driver.select)])
        b.pause=pause
        with contextlib.redirect_stdout(io.StringIO()):links,complete,_=b.search('Waqar Ali')
        self.assertTrue(complete);old_click.assert_not_called()

    def test_claim_loss_during_input_retry_stops_immediately(self):
        b=self.browser()
        def set_value(script,field,value):raise ClaimLost('expired')
        b.driver.execute_script=set_value
        with self.assertRaises(ClaimLost):b.search('Waqar Ali')
        b.page_snapshot.assert_not_called()


class NumberedPaginationTest(unittest.TestCase):
    browser = SearchLocationTest.browser
    # Reuse the browser fixture, but only run the pagination cases below.
    def snapshot(self, count, start, stop):
        ids = [f'{i:08x}-1111-1111-1111-111111111111' for i in range(start, stop)]
        return {'total': count, 'links': {u: BASE+'/talentsearch/profiles/'+u for u in ids},
                'names': {u: 'Phuntsho Wangdi' for u in ids}, 'next': []}

    def test_missing_next_collects_38_cards_and_preserves_WA(self):
        b=self.browser()
        b.page_snapshot=Mock(side_effect=lambda: self.snapshot(38,20,38)
            if parse_qs(urlsplit(b.driver.current_url).query).get('pageNumber')==['2']
            else self.snapshot(38,0,20))
        with contextlib.redirect_stdout(io.StringIO()):links,complete,_=b.search('Phuntsho Wangdi')
        self.assertTrue(complete);self.assertEqual(len(links),38)
        query=parse_qs(urlsplit(b.driver.visits[-1]).query)
        self.assertEqual(query['locations'],[LOCATION_VALUE])
        self.assertEqual(query['searchQuery'],['Phuntsho Wangdi'])
        self.assertEqual(query['pageNumber'],['2'])
        self.assertEqual(b.last_search_scan['pages_read'],2)
        self.assertEqual(b.last_search_scan['cards_checked'],38)

    def test_missing_next_repeated_page_stays_unresolved(self):
        b=self.browser();b.page_snapshot=Mock(return_value=self.snapshot(38,0,20))
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(BrowserReadError):
            b.search('Phuntsho Wangdi')

    def test_missing_next_respects_page_limit(self):
        b=self.browser();b.config['max_search_pages']=1
        b.page_snapshot=Mock(return_value=self.snapshot(38,0,20))
        with contextlib.redirect_stdout(io.StringIO()):_,complete,reason=b.search('Phuntsho Wangdi')
        self.assertFalse(complete);self.assertIn('page limit',reason)
        self.assertEqual(len(b.driver.visits),2)  # Initial search plus WA reload only.

    def test_missing_next_changed_count_stays_unresolved(self):
        b=self.browser()
        b.page_snapshot=Mock(side_effect=lambda: self.snapshot(37,20,37)
            if parse_qs(urlsplit(b.driver.current_url).query).get('pageNumber')==['2']
            else self.snapshot(38,0,20))
        with contextlib.redirect_stdout(io.StringIO()):_,complete,reason=b.search('Phuntsho Wangdi')
        self.assertFalse(complete);self.assertIn('changed during pagination',reason)

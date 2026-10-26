import copy
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from household_tools import calendar_setup
from household_tools.integrations import calendar, yoto


class CalendarTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env=patch.dict(os.environ,{'HERMES_HOME':directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.config={'calendar_id':'shared@example.test','name':'Shared test calendar','time_zone':'Europe/London','default_duration_minutes':60}
        yoto.write_private(calendar.config_path(),self.config)
        self.events={}
        self.requests=[]
        self.post_failure=None
        self.api_status=None
        self.more=False
        original=httpx.Client
        client=patch.object(calendar,'client',side_effect=lambda:original(
            transport=httpx.MockTransport(self.handler),headers={'Authorization':'Bearer TEST_PRIVATE_TOKEN'},follow_redirects=False))
        client.start()
        self.addCleanup(client.stop)

    def handler(self,request):
        self.requests.append(request)
        self.assertEqual(request.url.host,'www.googleapis.com')
        self.assertTrue(request.url.path.startswith('/calendar/v3/calendars/shared@example.test/events'))
        if self.api_status:return httpx.Response(self.api_status,text='PRIVATE_GOOGLE_RESPONSE')
        if request.method=='POST':
            self.assertEqual(request.url.params['sendUpdates'],'none')
            body=json.loads(request.content)
            self.assertNotIn('attendees',body)
            self.assertNotIn('recurrence',body)
            self.events[body['id']]={**body,'status':'confirmed','htmlLink':'https://calendar.google.com/calendar/event?eid=test'}
            if self.post_failure=='timeout':raise httpx.ReadTimeout('PRIVATE_GOOGLE_RESPONSE',request=request)
            return httpx.Response(self.post_failure or 200,json=self.events[body['id']])
        self.assertEqual(request.method,'GET')
        if request.url.path.endswith('/events'):
            return httpx.Response(200,json={'items':list(self.events.values()),'accessRole':'writer',
                **({'nextPageToken':'PRIVATE_PAGE_TOKEN'} if self.more else {})})
        event_id=request.url.path.rsplit('/',1)[1]
        return httpx.Response(200,json=self.events[event_id]) if event_id in self.events else httpx.Response(404)

    def add(self,**kwargs):
        return calendar.create_event('Test appointment','2026-10-30T15:00:00+00:00',**kwargs)

    def test_timed_create_is_verified_and_repeated_identical_details_do_not_duplicate(self):
        created=self.add(location='Test location',description='User notes')
        self.assertEqual(created['status'],'created')
        self.assertEqual(created['end']['dateTime'],'2026-10-30T16:00:00+00:00')
        self.assertEqual([r.method for r in self.requests],['GET','POST','GET'])
        again=self.add(location='Test location',description='User notes')
        self.assertEqual(again['status'],'already_exists')
        self.assertEqual(len(self.events),1)
        self.assertEqual(sum(r.method=='POST' for r in self.requests),1)
        self.assertNotIn('TEST_PRIVATE_TOKEN',repr(created))
        self.assertNotIn('description',created)

    def test_equivalent_time_offsets_dedupe_in_the_configured_zone(self):
        first=calendar.create_event('Test event','2026-07-01T10:00:00+01:00')
        second=calendar.create_event('Test event','2026-07-01T09:00:00Z')
        self.assertEqual(first['event_id'],second['event_id'])
        self.assertEqual(second['status'],'already_exists')
        self.assertEqual(len(self.events),1)

    def test_all_day_default_is_one_day_and_end_is_exclusive(self):
        result=calendar.create_event('Test all-day event','2026-11-05',all_day=True)
        self.assertEqual(result['start'],{'date':'2026-11-05'})
        self.assertEqual(result['end'],{'date':'2026-11-06'})
        multi=calendar.create_event('Test multi-day event','2026-11-05',end='2026-11-08',all_day=True)
        self.assertEqual(multi['end'],{'date':'2026-11-08'})

    def test_clock_changes_reject_ambiguous_or_missing_local_time_without_writing(self):
        for value in ['2026-10-25T01:30:00','2026-03-29T01:30:00']:
            with self.assertRaisesRegex(calendar.CalendarError,'clock change'):
                calendar.create_event('Test event',value)
        self.assertEqual(self.requests,[])
        body=calendar.event_body(self.config,'Test event','2026-10-25T01:30:00+01:00',None,False,'','')
        self.assertEqual(body['end']['dateTime'],'2026-10-25T01:30:00+00:00')
        body=calendar.event_body(self.config,'Test event','2026-10-25T01:30:00+01:00','2026-10-25T01:15:00+00:00',False,'','')
        self.assertEqual(body['end']['dateTime'],'2026-10-25T01:15:00+00:00')

    def test_invalid_inputs_fail_before_authentication_or_network(self):
        for kwargs in [{'title':'','start':'2026-10-30T15:00:00'},
                       {'title':'Test','start':'2026-10-30'},
                       {'title':'Test','start':'2026-10-30T15:00:00','end':'2026-10-30T14:00:00'},
                       {'title':'Test','start':'2026-10-30','end':'2026-10-30','all_day':True}]:
            with self.assertRaises(calendar.CalendarError):calendar.create_event(**kwargs)
        for bad in ['primary','https://calendar.google.com/calendar','Family']:
            with self.assertRaises(calendar.CalendarError):calendar.validate_config({**self.config,'calendar_id':bad})
        self.assertEqual(self.requests,[])

    def test_timeout_or_500_after_server_insert_is_reconciled_by_readback(self):
        for failure in ['timeout',500]:
            with self.subTest(failure=failure):
                self.events={}
                self.requests=[]
                self.post_failure=failure
                result=self.add()
                self.assertEqual(result['status'],'found_after_uncertain_create')
                self.assertEqual(sum(r.method=='POST' for r in self.requests),1)
                self.assertEqual(len(self.events),1)

    def test_unverified_create_does_not_retry_post_or_claim_success(self):
        original=self.handler
        def handler(request):
            response=original(request)
            if request.method=='POST':
                self.events.clear()
                return httpx.Response(500,text='PRIVATE_GOOGLE_RESPONSE')
            return response
        with patch.object(self,'handler',side_effect=handler),self.assertRaisesRegex(calendar.CalendarError,'outcome could not be verified'):
            self.add()
        self.assertEqual(sum(r.method=='POST' for r in self.requests),1)

    def test_changed_existing_event_is_not_overwritten_or_duplicated(self):
        created=self.add()
        self.events[created['event_id']]['summary']='Manually edited'
        with self.assertRaisesRegex(calendar.CalendarError,'details changed'):
            self.add()
        self.assertEqual(sum(r.method=='POST' for r in self.requests),1)
        self.assertEqual(self.events[created['event_id']]['summary'],'Manually edited')

    def test_list_is_read_only_marks_pagination_and_filters_private_fields(self):
        created=self.add()
        self.events[created['event_id']]['attendees']=[{'email':'PRIVATE_EMAIL'}]
        self.events[created['event_id']]['description']='PRIVATE_DESCRIPTION'
        self.requests=[]
        self.more=True
        result=calendar.list_events(start='2026-10-29',end='2026-11-01',limit=10,query='Test')
        self.assertEqual(len(result['events']),1)
        self.assertTrue(result['has_more'])
        self.assertNotIn('PRIVATE_',repr(result))
        request=self.requests[0]
        self.assertEqual(request.method,'GET')
        self.assertEqual(request.url.params['maxResults'],'10')
        self.assertEqual(request.url.params['singleEvents'],'true')
        self.assertEqual(request.url.params['timeMin'],'2026-10-29T00:00:00+00:00')

    def test_permissions_errors_are_actionable_and_do_not_echo_google_response(self):
        for code in [401,403,404]:
            self.api_status=code
            with self.assertRaises(calendar.CalendarError) as caught:
                calendar.list_events()
            self.assertIn(str(code),str(caught.exception))
            self.assertNotIn('PRIVATE_GOOGLE_RESPONSE',str(caught.exception))
        self.assertFalse(any(r.method=='POST' for r in self.requests))

    def test_status_has_current_timezone_and_unconfigured_status_is_not_live_access(self):
        result=calendar.status()
        self.assertEqual(result['time_zone'],'Europe/London')
        self.assertIn('now',result)
        self.assertFalse(result['credentials_saved'])
        calendar.config_path().unlink()
        self.assertFalse(calendar.status()['configured'])
        self.assertEqual(self.requests,[])


class CalendarSetupTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env=patch.dict(os.environ,{'HERMES_HOME':directory.name})
        env.start()
        self.addCleanup(env.stop)

    def test_configure_preserves_all_existing_integrations_and_only_reads_google(self):
        yoto.write_private(calendar.credentials_path(),{'test':'fake key marker'})
        config={'model':'keep','whatsapp':{'group_policy':'pairing'},'mcp_servers':{'household':{
            'timeout':300,'env':{'OTHER':'keep'},'tools':{'include':['ocado_weekly_shop','yoto_playback']}}}}
        yoto.write_private(yoto.home()/'config.yaml',config)
        (yoto.home()/'SOUL.md').write_text('Existing personal instructions.')
        credentials=SimpleNamespace(service_account_email='test@example.iam.gserviceaccount.com')
        with patch.object(calendar,'load_credentials',return_value=credentials),patch.object(calendar,'list_events',return_value={
            'calendar':'Shared test','time_zone':'Europe/London','access_role':'writer'}),patch('builtins.input',side_effect=['shared@example.test','Shared test','','']):
            calendar_setup.configure()
        current=yaml.safe_load((yoto.home()/'config.yaml').read_text())
        self.assertEqual(current['model'],'keep')
        self.assertEqual(current['whatsapp'],config['whatsapp'])
        entry=current['mcp_servers']['household']
        self.assertIn('ocado_weekly_shop',entry['tools']['include'])
        self.assertIn('yoto_playback',entry['tools']['include'])
        self.assertIn('calendar_create_event',entry['tools']['include'])
        self.assertEqual(entry['timeout'],300)
        self.assertEqual(entry['env'],{'OTHER':'keep','HERMES_HOME':str(yoto.home())})
        calendar_setup.enable_tools()
        self.assertEqual((yoto.home()/'SOUL.md').read_text().count('<!-- household-google-calendar-v1 -->'),1)
        self.assertTrue((yoto.home()/'SOUL.md').read_text().startswith('Existing personal instructions.'))
        self.assertEqual(calendar.config_path().stat().st_mode & 0o777,0o600)
        self.assertEqual(calendar.credentials_path().stat().st_mode & 0o777,0o600)
        self.assertTrue(list(yoto.home().glob('config.before-calendar-*')))

    def test_read_only_permission_does_not_enable_calendar_writes(self):
        with patch.object(calendar,'list_events',return_value={'calendar':'Test','time_zone':'Europe/London','access_role':'reader'}):
            with self.assertRaisesRegex(calendar.CalendarError,'event-edit permission'):
                calendar_setup.check_connection()

    def test_real_google_library_loads_only_expected_service_account_and_scope(self):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048).private_bytes(
            serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
        value={'type':'service_account','client_email':'test@example.iam.gserviceaccount.com','token_uri':calendar.TOKEN_URL,'private_key':key}
        yoto.write_private(calendar.credentials_path(),value)
        credentials=calendar.load_credentials()
        self.assertEqual(credentials.scopes,[calendar.SCOPE])
        self.assertEqual(credentials.service_account_email,value['client_email'])
        value['token_uri']='https://untrusted.example/token'
        yoto.write_private(calendar.credentials_path(),value)
        with self.assertRaises(calendar.CalendarError) as caught:
            calendar.load_credentials()
        self.assertNotIn('PRIVATE KEY',str(caught.exception))

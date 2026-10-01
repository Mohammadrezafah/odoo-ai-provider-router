import copy
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from urllib.parse import urlsplit

from odoo import api
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import (
    HOST, BlockedRequest, _registry_test_lock, _super_send, release_test_lock,
)
from odoo.tools import hmac

from odoo.addons.ai.tests.common import TestAICallbackCommon, TestAICommon
from odoo.addons.ai.utils.ai_utils import UserInputResponse
from odoo.addons.ai_provider_router.utils import provider, runtime
from odoo.addons.ai_provider_router.utils.provider import ProviderError

PROVIDER_URL = 'http://127.0.0.1:8081/v1'
TEST_MODEL = 'unit-test-model'
DUMMY_KEY = 'dummy-ai-router-test-key'


class _FakeProviderHandler(BaseHTTPRequestHandler):
    recorded = deque()
    script = deque()

    @staticmethod
    def _is_title_request(body):
        instructions = next(
            (message.get('content') or '' for message in body.get('messages', []) if message.get('role') == 'system'),
            '',
        )
        return 'title generator' in instructions

    def _next_reply(self, body):
        if self._is_title_request(body):
            return {'content': 'AI_ROUTER_VERIFY Chat', 'finish_reason': 'stop'}
        if self.script:
            reply = self.script.popleft()
            return reply(body) if callable(reply) else reply
        return {'content': 'ROUTER_OK', 'finish_reason': 'stop'}

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = {}
        self.recorded.append({
            'method': 'POST',
            'path': self.path,
            'auth_ok': self.headers.get('Authorization') == f'Bearer {DUMMY_KEY}',
            'has_cookie': bool(self.headers.get('Cookie')),
            'is_title': self._is_title_request(body),
            'model': body.get('model'),
            'roles': [message.get('role') for message in body.get('messages', [])],
            'tool_names': [tool.get('function', {}).get('name') for tool in body.get('tools', [])],
            'tool_call_ids': [
                tool_call.get('id') for message in body.get('messages', [])
                for tool_call in message.get('tool_calls', [])
            ],
            'tool_result_ids': [
                message.get('tool_call_id') for message in body.get('messages', [])
                if message.get('role') == 'tool'
            ],
        })
        reply = self._next_reply(body)
        if reply.get('error_status'):
            self.send_response(reply['error_status'])
            self.end_headers()
            self.wfile.write(b'{}')
            return
        message = {'role': 'assistant', 'content': reply.get('content')}
        if reply.get('tool_calls'):
            message['tool_calls'] = reply['tool_calls']
        for key in ('reasoning_content', 'reasoning_details'):
            if key in reply:
                message[key] = reply[key]
        payload = json.dumps({
            'id': 'chatcmpl-fake',
            'object': 'chat.completion',
            'model': body.get('model'),
            'choices': [{
                'index': 0,
                'finish_reason': reply.get('finish_reason', 'stop'),
                'message': message,
            }],
        }).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@tagged("post_install", "-at_install", "ai_provider_router")
class TestAIRouterCommon(TestAICommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.icp = cls.env['ir.config_parameter'].sudo()

    def _enable_router(self):
        self.icp.set_bool(provider.PARAM_ENABLED, True)
        self.icp.set_str(provider.PARAM_BASE_URL, PROVIDER_URL)
        self.icp.set_str(provider.PARAM_MODEL, TEST_MODEL)
        self.icp.set_str(provider.PARAM_API_KEY, DUMMY_KEY)

    def _disable_router(self):
        for key in (provider.PARAM_ENABLED, provider.PARAM_BASE_URL, provider.PARAM_MODEL, provider.PARAM_API_KEY):
            self.icp.set_str(key, None)


class TestAIRouterSession(TestAIRouterCommon):

    def setUp(self):
        super().setUp()
        self._disable_router()

    def test_disabled_sync_delegates_to_native(self):
        self._disable_router()
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'first'}]}]
        with patch('odoo.addons.ai.models.ai_session.call_odoo_ai', return_value={'result': 'native'}) as native_call:
            result = self.env['ai.session']._get_completions(messages, 'native instructions')
        self.assertEqual(result, {'result': 'native'})
        self.assertEqual(native_call.call_count, 1)
        self.assertEqual(native_call.call_args.args[1], '1/get_completions_sync')

    def test_disabled_async_delegates_to_native(self):
        self._disable_router()
        session = self._create_ai_session(self.agent)
        payload = {
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'first'}]}],
            'instructions': 'native instructions',
            'tools': [],
            'usage': 'agent_loop',
        }
        with patch('odoo.addons.ai.models.ai_session.call_odoo_ai_transport', return_value={}) as transport:
            session._save_and_submit_request(
                payload, callback_type='agent_loop',
                request_round=1, request_round_limit=5, state={},
            )
            transport.assert_not_called()
            self.env.cr.postcommit.run()
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[1], '1/get_completions')
        self.assertIn('webhook_url', transport.call_args.args[2])
        self.assertIn('webhook_secret', transport.call_args.args[2])
        self.assertEqual(session.loop_state, 'waiting_model')

    def test_enabled_sync_uses_provider(self):
        self._enable_router()
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'first'}]}]
        tools = [{'name': 'tool_a', 'instructions': 'does a', 'schema': {'type': 'object'}}]
        captured = {}

        def fake_post(router_config, body, session_ref=None):
            captured['config'] = router_config
            captured['body'] = body
            captured['session_ref'] = session_ref
            return {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ROUTER_OK'}}]}

        original_messages = copy.deepcopy(messages)
        with patch.object(runtime, 'post_chat_completions', side_effect=fake_post):
            result = self.env['ai.session']._get_completions(messages, 'native instructions', tools)
        self.assertEqual(messages, original_messages)
        self.assertEqual(captured['config']['endpoint_url'], PROVIDER_URL + '/chat/completions')
        self.assertEqual(captured['config']['api_key'], DUMMY_KEY)
        self.assertEqual(captured['body']['model'], TEST_MODEL)
        self.assertEqual(captured['body']['messages'][0], {'role': 'system', 'content': 'native instructions'})
        self.assertEqual(captured['body']['messages'][1], {'role': 'user', 'content': 'first'})
        self.assertEqual(result['result']['content'], [{'type': 'text', 'text': 'ROUTER_OK'}])

    def test_enabled_sync_error_no_iap_fallback(self):
        self._enable_router()
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'first'}]}]
        with (
            patch.object(runtime, 'post_chat_completions', side_effect=ProviderError('timeout')),
            patch('odoo.addons.ai.models.ai_session.call_odoo_ai') as native_call,
            self.assertRaises(UserError) as ctx,
        ):
            self.env['ai.session']._get_completions(messages, 'native instructions')
        self.assertNotIn(DUMMY_KEY, str(ctx.exception))
        native_call.assert_not_called()

    def test_enabled_async_validates_before_save(self):
        self._enable_router()
        session = self._create_ai_session(self.agent)
        payload = {
            'messages': [{'role': 'user', 'content': [{'type': 'inline_data', 'data': b'x', 'mimetype': 'image/png'}]}],
            'instructions': 'native instructions',
            'tools': [],
        }
        with self.assertRaises(UserError):
            session._save_and_submit_request(
                payload, callback_type='agent_loop',
                request_round=1, request_round_limit=5, state={},
            )
        self.assertEqual(session.loop_state, 'ready')

    def test_enabled_async_persists_native_fields(self):
        self._enable_router()
        session = self._create_ai_session(self.agent)
        payload = {
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'first'}]}],
            'instructions': 'native instructions',
            'tools': [],
            'usage': 'agent_loop',
        }
        with patch.object(runtime, 'submit_async_request') as submit:
            session._save_and_submit_request(
                payload, callback_type='agent_loop',
                request_round=1, request_round_limit=5, state={},
            )
            submit.assert_not_called()
            self.assertEqual(session.loop_state, 'waiting_model')
            self.assertTrue(session.request_uuid)
            self.assertTrue(session.request_webhook_secret)
            self.assertEqual(session.request_round, 1)
            self.assertEqual(session.request_round_limit, 5)
            self.assertEqual(session.request_payload, payload)
            self.assertEqual(session.request_user_id, self.env.user)
            self.assertEqual(session.state['callback_type'], 'agent_loop')
            self.env.cr.postcommit.run()
        submit.assert_called_once()
        job = submit.call_args.args[0]
        self.assertEqual(job['request_uuid'], session.request_uuid)
        self.assertEqual(job['webhook_secret'], session.request_webhook_secret)
        self.assertEqual(job['dbname'], self.env.cr.dbname)
        self.assertEqual(job['config']['endpoint_url'], PROVIDER_URL + '/chat/completions')
        self.assertEqual(job['config']['api_key'], DUMMY_KEY)
        self.assertEqual(job['body']['messages'][0]['content'], 'native instructions')

    def test_postcommit_not_submitted_on_rollback(self):
        self._enable_router()
        session = self._create_ai_session(self.agent)
        payload = {
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'first'}]}],
            'instructions': 'native instructions',
            'tools': [],
        }
        with self.registry_test_mode(), patch.object(runtime, 'submit_async_request') as submit:
            cr = self.registry.cursor()
            test_env = api.Environment(cr, self.env.uid, dict(self.env.context))
            test_session = test_env['ai.session'].browse(session.id)
            test_session._save_and_submit_request(
                payload, callback_type='agent_loop',
                request_round=1, request_round_limit=5, state={},
            )
            cr.commit()
            submit.assert_called_once()
            submit.reset_mock()
            test_session._save_and_submit_request(
                payload, callback_type='agent_loop',
                request_round=2, request_round_limit=5, state={},
            )
            cr.rollback()
            submit.assert_not_called()
            cr.close()

    def test_worker_delivers_signed_result(self):
        self._enable_router()
        job = {
            'config': provider.get_router_config(self.env),
            'body': {'model': TEST_MODEL, 'messages': [], 'stream': False},
            'offered_tool_names': set(),
            'structured': False,
            'dbname': self.env.cr.dbname,
            'request_uuid': 'uuid-1',
            'webhook_secret': 'secret-1',
        }
        delivered = {}
        with (
            patch.object(runtime, 'post_chat_completions', return_value={
                'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ROUTER_OK'}}],
            }),
            patch.object(runtime, 'deliver_callback', side_effect=lambda *a: delivered.update(args=a) or True),
        ):
            runtime._run_job(job)
        dbname, request_uuid, webhook_secret, llm_result, llm_error = delivered['args']
        self.assertEqual((dbname, request_uuid, webhook_secret), (self.env.cr.dbname, 'uuid-1', 'secret-1'))
        self.assertFalse(llm_error)
        self.assertEqual(llm_result['result']['content'], [{'type': 'text', 'text': 'ROUTER_OK'}])

    def test_worker_failure_delivers_signed_error(self):
        self._enable_router()
        job = {
            'config': provider.get_router_config(self.env),
            'body': {},
            'offered_tool_names': set(),
            'structured': False,
            'dbname': self.env.cr.dbname,
            'request_uuid': 'uuid-2',
            'webhook_secret': 'secret-2',
        }
        delivered = {}
        with (
            patch.object(runtime, 'post_chat_completions', side_effect=ProviderError('timeout')),
            patch.object(runtime, 'deliver_callback', side_effect=lambda *a: delivered.update(args=a) or True),
        ):
            runtime._run_job(job)
        self.assertEqual(delivered['args'][3:], (False, True))

    def test_queue_full_delivers_failure_without_provider_call(self):
        self._enable_router()
        job = {
            'config': provider.get_router_config(self.env),
            'body': {},
            'offered_tool_names': set(),
            'structured': False,
            'dbname': self.env.cr.dbname,
            'request_uuid': 'uuid-3',
            'webhook_secret': 'secret-3',
        }
        for _ in range(runtime.MAX_PENDING):
            self.assertTrue(runtime._pending.acquire(blocking=False))
        self.addCleanup(lambda: [runtime._pending.release() for _ in range(runtime.MAX_PENDING)])
        with (
            patch.object(runtime, 'post_chat_completions') as provider_call,
            patch.object(runtime, 'deliver_callback', return_value=True) as callback,
        ):
            runtime.submit_async_request(job)
            deadline = time.monotonic() + 10
            while not callback.called and time.monotonic() < deadline:
                time.sleep(0.01)
        provider_call.assert_not_called()
        callback.assert_called_once()
        self.assertEqual(callback.call_args.args[3:], (False, True))


@tagged("post_install", "-at_install", "ai_provider_router")
class TestAIRouterHttp(TestAICallbackCommon):

    @classmethod
    def _request_handler(cls, s, r, /, **kw):
        timeout = kw.get('timeout')
        if not isinstance(timeout, tuple):
            return super()._request_handler(s, r, **kw)
        url = urlsplit(r.url)
        if url.hostname in (HOST, 'localhost') or url.scheme == 'file':
            return _super_send(s, r, **kw)
        raise BlockedRequest(f"External requests verboten (was {r.method} {r.url})")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.icp = cls.env['ir.config_parameter'].sudo()
        cls.fake_provider = ThreadingHTTPServer(('127.0.0.1', 8081), _FakeProviderHandler)
        cls.fake_provider.daemon_threads = True
        cls.provider_thread = threading.Thread(target=cls.fake_provider.serve_forever, daemon=True)
        cls.provider_thread.start()
        cls.addClassCleanup(cls.fake_provider.shutdown)
        cls.addClassCleanup(cls.fake_provider.server_close)
        cls.tool_router = cls.env['ir.actions.server'].create([{
            'model_id': cls.env['ir.model']._get_id('ai.agent'),
            'state': 'code',
            'name': 'router_tool',
            'ai_tool_name': 'router_tool',
            'use_in_ai': True,
            'code': "ai['result'] = 'tool done'",
        }])
        cls.tool_router_confirm = cls.env['ir.actions.server'].create([{
            'model_id': cls.env['ir.model']._get_id('ai.agent'),
            'state': 'code',
            'name': 'router_tool_confirm',
            'ai_tool_name': 'router_tool_confirm',
            'use_in_ai': True,
            'code': (
                "if not ai['tool_request_confirmed']:\n"
                "    ai['user_input_request'] = {'type': 'confirmation', 'body': 'Do this?', 'choices': ["
                "{'label': 'Yes, do it', 'value': 'confirm_once'},"
                "{'label': 'No, I want something else', 'value': 'decline'}]}\n"
                "else:\n"
                "    ai['result'] = 'tool done'"
            ),
        }])

    def setUp(self):
        super().setUp()
        self.http_request_allow_all = True
        self.icp.set_bool(provider.PARAM_ENABLED, True)
        self.icp.set_str(provider.PARAM_BASE_URL, PROVIDER_URL)
        self.icp.set_str(provider.PARAM_MODEL, TEST_MODEL)
        self.icp.set_str(provider.PARAM_API_KEY, DUMMY_KEY)
        _FakeProviderHandler.recorded.clear()
        _FakeProviderHandler.script.clear()

    def _wait_until(self, check, timeout=30, message='condition not reached'):
        deadline = time.monotonic() + timeout
        with release_test_lock():
            while time.monotonic() < deadline:
                if _registry_test_lock.acquire(timeout=5):
                    try:
                        self.env.invalidate_all()
                        if check():
                            return
                    finally:
                        _registry_test_lock.release()
                else:
                    time.sleep(0.05)
        self.fail(message)

    def _wait_for(self, session, states, timeout=30):
        state = session.loop_state

        def check():
            nonlocal state
            state = session.loop_state
            return state in states

        try:
            self._wait_until(check, timeout=timeout)
        except AssertionError:
            self.fail(f"session {session.id} stayed in state {state}, expected {states}")
        return state

    def _post_message(self, session, body):
        mail_msg = session.channel_id.message_post(body=body, message_type="comment")
        self.authenticate(self.env.user.login, 'admin', session_extra={
            "context": {"allowed_company_ids": self.env.companies.ids},
        })
        ack = self.make_jsonrpc_request("/ai/start_session_advance", {
            "channel_id": session.channel_id.id,
            "mail_message_id": mail_msg.id,
            "ai_session_config": self._get_ai_session_default_controls() | {
                'enable_web_search': False, 'ai_prompt_button_ref': None,
            },
        })
        return ack

    def _channel_texts(self, session):
        self.env.invalidate_all()
        return [
            str(message.body) for message in session.channel_id.message_ids
            if message.author_id == session.agent_id.partner_id
        ]

    def test_native_chat_flow_reaches_provider(self):
        session = self._create_ai_session(self.agent)
        ack = self._post_message(session, "AI_ROUTER_VERIFY: balas dengan ROUTER_OK.")
        self.assertEqual(ack['loop_state'], 'waiting_model')
        self._wait_for(session, ('ready',))
        self._wait_until(
            lambda: session.channel_id.name == 'AI_ROUTER_VERIFY Chat',
            message='channel title was not set by the title route',
        )
        self.assertIn('ROUTER_OK', "".join(self._channel_texts(session)))
        self.assertEqual(session.channel_id.name, 'AI_ROUTER_VERIFY Chat')
        self.assertGreaterEqual(len(_FakeProviderHandler.recorded), 2)
        for request_info in _FakeProviderHandler.recorded:
            self.assertEqual(request_info['method'], 'POST')
            self.assertEqual(request_info['path'], '/v1/chat/completions')
            self.assertTrue(request_info['auth_ok'])
            self.assertFalse(request_info['has_cookie'])
            self.assertEqual(request_info['model'], TEST_MODEL)

    def test_tool_call_confirmation_and_result_flow(self):
        session = self._create_ai_session(self.agent)
        _FakeProviderHandler.script.append({
            'content': None,
            'finish_reason': 'tool_calls',
            'tool_calls': [{
                'id': 'call_1', 'type': 'function',
                'function': {'name': 'router_tool_confirm', 'arguments': '{}'},
            }],
        })
        _FakeProviderHandler.script.append({'content': 'ROUTER_OK', 'finish_reason': 'stop'})
        with self.mock_default_tools(self.tool_router_confirm):
            ack = self._post_message(session, "AI_ROUTER_VERIFY: balas dengan ROUTER_OK.")
            self._wait_for(session, ('waiting_confirmation',))
            self.assertTrue(session.resume_token)
            self.assertIn('Do this?', session.pending_tool_call['user_input_request']['body'])
            chat_requests = [r for r in _FakeProviderHandler.recorded if not r['is_title']]
            self.assertIn('router_tool_confirm', chat_requests[0]['tool_names'])
            resume = self.make_jsonrpc_request("/ai/resume_pending_interaction", {
                "channel_id": session.channel_id.id,
                "session_id": session.id,
                "resume_token": session.resume_token,
                "response": {'kind': 'confirmation', 'value': UserInputResponse.CONFIRM_ONCE},
                "ai_session_config": self._get_ai_session_default_controls() | {'enable_web_search': False},
            })
            self.assertTrue(resume['interactionConsumed'])
            self._wait_for(session, ('ready',))
        tool_results = [
            part for event in session.event_ids
            for part in event.metadata.get('content', [])
            if part.get('type') == 'tool_result'
        ]
        self.assertTrue(tool_results)
        self.assertEqual(tool_results[0]['tool_call_id'], 'call_1')
        self.assertTrue(tool_results[0]['success'])
        self.assertEqual(tool_results[0]['result'][0]['text'], 'tool done')
        self.assertIn('ROUTER_OK', "".join(self._channel_texts(session)))
        chat_requests = [r for r in _FakeProviderHandler.recorded if not r['is_title']]
        self.assertEqual(chat_requests[-1]['roles'][-1], 'tool')
        self.assertEqual(chat_requests[-1]['tool_result_ids'], ['call_1'])

    def test_denied_tool_does_not_execute(self):
        session = self._create_ai_session(self.agent)
        _FakeProviderHandler.script.append({
            'content': None,
            'finish_reason': 'tool_calls',
            'tool_calls': [{
                'id': 'call_1', 'type': 'function',
                'function': {'name': 'router_tool_confirm', 'arguments': '{}'},
            }],
        })
        with self.mock_default_tools(self.tool_router_confirm):
            self._post_message(session, "AI_ROUTER_VERIFY: balas dengan ROUTER_OK.")
            self._wait_for(session, ('waiting_confirmation',))
            self.make_jsonrpc_request("/ai/resume_pending_interaction", {
                "channel_id": session.channel_id.id,
                "session_id": session.id,
                "resume_token": session.resume_token,
                "response": {'kind': 'confirmation', 'value': UserInputResponse.DECLINE},
            })
            self._wait_for(session, ('ready',))
        tool_results = [
            part for event in session.event_ids
            for part in event.metadata.get('content', [])
            if part.get('type') == 'tool_result'
        ]
        self.assertTrue(tool_results)
        self.assertFalse(tool_results[0]['success'])
        self.assertIn('not to proceed', tool_results[0]['result'][0]['text'])
        chat_requests = [r for r in _FakeProviderHandler.recorded if not r['is_title']]
        self.assertTrue(chat_requests)
        self.assertFalse([part for part in tool_results if part.get('success')])

    def test_unknown_tool_call_fails_safely(self):
        session = self._create_ai_session(self.agent)
        _FakeProviderHandler.script.append({
            'content': None,
            'finish_reason': 'tool_calls',
            'tool_calls': [{
                'id': 'call_1', 'type': 'function',
                'function': {'name': 'never_offered', 'arguments': '{}'},
            }],
        })
        with self.mock_default_tools(self.tool_router):
            self._post_message(session, "AI_ROUTER_VERIFY: balas dengan ROUTER_OK.")
            self._wait_for(session, ('ready',))
        self.assertFalse([
            part for event in session.event_ids
            for part in event.metadata.get('content', [])
            if part.get('type') == 'tool_result'
        ])
        self.assertIn('unreachable', "".join(self._channel_texts(session)))

    def test_provider_error_exits_waiting_model(self):
        session = self._create_ai_session(self.agent)
        _FakeProviderHandler.script.append({'error_status': 500})
        self._post_message(session, "AI_ROUTER_VERIFY: balas dengan ROUTER_OK.")
        self._wait_for(session, ('ready',))
        self.assertIn('unreachable', "".join(self._channel_texts(session)))

    def test_wrong_signature_and_duplicate_callback_ignored(self):
        session = self._create_ai_session(self.agent)
        self._post_message(session, "AI_ROUTER_VERIFY: balas dengan ROUTER_OK.")
        self._wait_for(session, ('ready',))
        message_count = len(session.channel_id.message_ids)
        llm_result = {'result': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'second'}], 'provider_metadata': {}}}
        self.env.cr.execute(
            "SELECT request_uuid, request_webhook_secret FROM ai_session WHERE id = %s",
            [session.id],
        )
        request_uuid, secret = self.env.cr.fetchone()
        good_signature = hmac(None, 'odoo_ai-webhook', (request_uuid, llm_result, False), secret=secret)
        bad = self.url_open("/ai/completion_result_ready", json={
            'request_uuid': request_uuid,
            'llm_result': llm_result,
            'llm_error': False,
            'signature': 'deadbeef' + good_signature[8:],
        })
        self.assertEqual(bad.status_code, 200)
        dup = self.url_open("/ai/completion_result_ready", json={
            'request_uuid': request_uuid,
            'llm_result': llm_result,
            'llm_error': False,
            'signature': good_signature,
        })
        self.assertEqual(dup.status_code, 200)
        self.env.invalidate_all()
        self.assertEqual(len(session.channel_id.message_ids), message_count)
        self.assertEqual(session.loop_state, 'ready')

    def test_multi_turn_history_roles_preserved(self):
        session = self._create_ai_session(self.agent)
        self._post_message(session, "first")
        self._wait_for(session, ('ready',))
        self._post_message(session, "second")
        self._wait_for(session, ('ready',))
        chat_requests = [r for r in _FakeProviderHandler.recorded if not r['is_title']]
        self.assertEqual(len(chat_requests), 2)
        last = chat_requests[-1]
        self.assertEqual(last['roles'][0], 'system')
        self.assertIn('user', last['roles'])
        self.assertIn('assistant', last['roles'])
        self.assertEqual(last['roles'][-1], 'user')

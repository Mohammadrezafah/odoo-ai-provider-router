import copy

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.ai_provider_router.utils import provider
from odoo.addons.ai_provider_router.utils.provider import ProviderError


PROVIDER_META = {'base_url': 'http://127.0.0.1:8081/v1', 'model': 'unit-test-model'}


def _body(messages, tools=None, options=None, instructions='native instructions', model='unit-test-model'):
    return provider.build_request_body(
        model=model,
        provider_meta=PROVIDER_META,
        messages=messages,
        instructions=instructions,
        tools=tools,
        options=options,
    )


@tagged("post_install", "-at_install", "ai_provider_router")
class TestURLValidation(TransactionCase):

    def test_valid_urls(self):
        self.assertEqual(
            provider.normalize_base_url('https://openrouter.ai/api/v1'),
            'https://openrouter.ai/api/v1',
        )
        self.assertEqual(
            provider.normalize_base_url('https://api.deepseek.com/v1/'),
            'https://api.deepseek.com/v1',
        )
        for url in (
            'http://127.0.0.1:8081/v1',
            'http://localhost:8081/v1',
            'http://[::1]:8080/v1',
        ):
            self.assertTrue(provider.normalize_base_url(url))

    def test_chat_completions_url_appended_once(self):
        self.assertEqual(
            provider.chat_completions_url('https://openrouter.ai/api/v1/'),
            'https://openrouter.ai/api/v1/chat/completions',
        )

    def test_invalid_urls_rejected(self):
        for url in (
            '', None, 'notaurl', 'ftp://example.com',
            'http://example.com/v1',
            'https://user:pass@example.com/v1',
            'https://example.com/v1?x=1',
            'https://example.com/v1#frag',
            'https://example.com/v1/chat/completions',
            'https://example.com:99999/v1',
            'https://example.com/bad port/v1',
        ):
            with self.assertRaises(UserError, msg=url):
                provider.normalize_base_url(url)


@tagged("post_install", "-at_install", "ai_provider_router")
class TestRequestMapping(TransactionCase):

    def test_basic_body_structure(self):
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'first'}]}]
        body = _body(messages)
        self.assertEqual(body['model'], 'unit-test-model')
        self.assertIs(body['stream'], False)
        self.assertEqual(body['messages'][0], {'role': 'system', 'content': 'native instructions'})
        self.assertEqual(body['messages'][1], {'role': 'user', 'content': 'first'})
        self.assertNotIn('tools', body)
        self.assertNotIn('tool_choice', body)
        self.assertNotIn('response_format', body)
        for forbidden in ('temperature', 'max_tokens', 'top_p', 'seed', 'usage'):
            self.assertNotIn(forbidden, body)

    def test_multi_part_text_joined_and_nonstring_serialized(self):
        messages = [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'first'},
                {'type': 'text', 'text': 'second'},
            ]},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': {'ok': True}}]},
        ]
        body = _body(messages)
        self.assertEqual(body['messages'][1]['content'], 'first\nsecond')
        self.assertEqual(body['messages'][2]['content'], '{"ok": true}')

    def test_input_messages_not_mutated(self):
        messages = [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'first'}]},
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'second'},
                {'type': 'tool_call', 'name': 'tool_a', 'args': {'v': 1}, 'call_id': 'c1'},
            ], 'provider_metadata': {'ai_provider_router': dict(PROVIDER_META, reasoning_content='r')}},
        ]
        snapshot = copy.deepcopy(messages)
        _body(messages)
        self.assertEqual(messages, snapshot)

    def test_assistant_tool_call_mapping(self):
        messages = [
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'first'},
                {'type': 'tool_call', 'name': 'tool_a', 'args': {'v': 1}, 'call_id': 'c1'},
            ]},
        ]
        body = _body(messages)
        assistant = body['messages'][1]
        self.assertEqual(assistant['role'], 'assistant')
        self.assertEqual(assistant['content'], 'first')
        self.assertEqual(assistant['tool_calls'], [{
            'id': 'c1',
            'type': 'function',
            'function': {'name': 'tool_a', 'arguments': '{"v": 1}'},
        }])

    def test_tool_results_become_tool_messages_and_preserve_order(self):
        messages = [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'first'},
            {'type': 'tool_result', 'tool_name': 'tool_a', 'tool_call_id': 'c1',
             'result': [{'type': 'text', 'text': 'tool done'}], 'success': True},
            {'type': 'tool_result', 'tool_name': 'tool_b', 'tool_call_id': 'c2',
             'result': [{'type': 'text', 'text': 'tool denied'}], 'success': False},
            {'type': 'text', 'text': 'second'},
        ]}]
        body = _body(messages)
        self.assertEqual(body['messages'][1], {'role': 'user', 'content': 'first'})
        self.assertEqual(body['messages'][2], {'role': 'tool', 'tool_call_id': 'c1', 'content': 'tool done'})
        self.assertEqual(body['messages'][3], {'role': 'tool', 'tool_call_id': 'c2', 'content': 'tool denied'})
        self.assertEqual(body['messages'][4], {'role': 'user', 'content': 'second'})

    def test_tools_mapping(self):
        tools = [
            {'name': 'tool_a', 'instructions': 'does a', 'schema': {'type': 'object', 'properties': {'v': {'type': 'string'}}}},
            {'name': 'tool_b', 'instructions': '', 'schema': None},
        ]
        body = _body([], tools=tools)
        self.assertEqual(body['tool_choice'], 'auto')
        self.assertEqual(body['tools'], [
            {'type': 'function', 'function': {
                'name': 'tool_a', 'description': 'does a',
                'parameters': {'type': 'object', 'properties': {'v': {'type': 'string'}}},
            }},
            {'type': 'function', 'function': {
                'name': 'tool_b', 'description': '',
                'parameters': {'type': 'object', 'properties': {}},
            }},
        ])

    def test_schema_sets_response_format(self):
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
        body = _body([], options={'schema': schema})
        self.assertEqual(body['response_format'], {
            'type': 'json_schema',
            'json_schema': {'name': 'odoo_ai_response', 'schema': schema},
        })

    def test_unsupported_options_fail(self):
        for option in ('image_generation', 'web_grounding', 'boost_reasoning', 'aspect_ratio'):
            with self.assertRaises(UserError, msg=option):
                _body([], options={option: 'x' if option == 'aspect_ratio' else True})
        body = _body([], options={k: False for k in ('image_generation', 'web_grounding', 'boost_reasoning')})
        self.assertNotIn('response_format', body)

    def test_inline_data_input_fails(self):
        messages = [{'role': 'user', 'content': [{'type': 'inline_data', 'data': b'x', 'mimetype': 'image/png'}]}]
        with self.assertRaises(UserError):
            _body(messages)

    def test_reasoning_metadata_round_trip_same_provider(self):
        stored_meta = {
            'ai_provider_router': dict(PROVIDER_META, reasoning_content='rc', reasoning_details=[{'i': 1}]),
        }
        messages = [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'second'}], 'provider_metadata': stored_meta}]
        body = _body(messages)
        self.assertEqual(body['messages'][1]['reasoning_content'], 'rc')
        self.assertEqual(body['messages'][1]['reasoning_details'], [{'i': 1}])

    def test_reasoning_metadata_dropped_on_provider_switch(self):
        stored_meta = {
            'ai_provider_router': {
                'base_url': 'https://other.example.com/v1',
                'model': 'other-model',
                'reasoning_content': 'rc',
            },
        }
        messages = [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'second'}], 'provider_metadata': stored_meta}]
        body = _body(messages)
        self.assertNotIn('reasoning_content', body['messages'][1])
        self.assertNotIn('reasoning_details', body['messages'][1])


@tagged("post_install", "-at_install", "ai_provider_router")
class TestResponseMapping(TransactionCase):

    def _parse(self, response, offered=(), structured=False):
        return provider.parse_response(
            response, provider_meta=PROVIDER_META,
            offered_tool_names=offered, structured=structured,
        )

    def _response(self, content='ROUTER_OK', finish='stop', extra_message=None, extra=None):
        message = {'role': 'assistant', 'content': content}
        message.update(extra_message or {})
        return {
            'choices': [{'index': 0, 'finish_reason': finish, 'message': message}],
            **(extra or {}),
        }

    def test_text_response_envelope(self):
        result = self._parse(self._response())
        self.assertEqual(result['result']['role'], 'assistant')
        self.assertEqual(result['result']['content'], [{'type': 'text', 'text': 'ROUTER_OK'}])
        meta = result['result']['provider_metadata']['ai_provider_router']
        self.assertEqual(meta['base_url'], PROVIDER_META['base_url'])
        self.assertEqual(meta['model'], PROVIDER_META['model'])

    def test_tool_call_response(self):
        response = self._response(content=None, finish='tool_calls', extra_message={
            'tool_calls': [{
                'id': 'c1', 'type': 'function',
                'function': {'name': 'tool_a', 'arguments': '{"v": 1}'},
            }],
        })
        result = self._parse(response, offered={'tool_a'})
        self.assertEqual(result['result']['content'], [
            {'type': 'tool_call', 'name': 'tool_a', 'args': {'v': 1}, 'call_id': 'c1'},
        ])

    def test_tool_call_validation(self):
        base_tool_call = {'id': 'c1', 'type': 'function', 'function': {'name': 'tool_a', 'arguments': '{}'}}
        bad_cases = [
            dict(base_tool_call, function={'name': 'not_offered', 'arguments': '{}'}),
            dict(base_tool_call, function={'name': 'tool_a', 'arguments': 'not json'}),
            dict(base_tool_call, function={'name': 'tool_a', 'arguments': '[1,2]'}),
            dict(base_tool_call, id=''),
            dict(base_tool_call, type='not_function'),
        ]
        for tool_call in bad_cases:
            response = self._response(content=None, extra_message={'tool_calls': [tool_call]})
            with self.assertRaises(ProviderError, msg=str(tool_call)):
                self._parse(response, offered={'tool_a'})
        duplicated = self._response(content=None, extra_message={'tool_calls': [base_tool_call, dict(base_tool_call)]})
        with self.assertRaises(ProviderError):
            self._parse(duplicated, offered={'tool_a'})
        missing_name = self._response(content=None, extra_message={
            'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'arguments': '{}'}}],
        })
        with self.assertRaises(ProviderError):
            self._parse(missing_name, offered={'tool_a'})

    def test_error_and_empty_responses_fail(self):
        for response in (
            {'error': {'message': 'nope'}},
            {'choices': []},
            {'choices': 'broken'},
            {'choices': [{'finish_reason': 'stop'}]},
            self._response(content=''),
            self._response(content=None),
            'not a dict',
            self._response(finish='length'),
            self._response(finish='content_filter'),
            self._response(content=[{'type': 'weird', 'text': 'x'}]),
        ):
            with self.assertRaises(ProviderError, msg=repr(response)):
                self._parse(response)

    def test_structured_response_decoded(self):
        response = self._response(content='{"ok": true}')
        result = self._parse(response, structured=True)
        self.assertEqual(result['result']['content'], [{'type': 'text', 'text': {'ok': True}}])
        with self.assertRaises(ProviderError):
            self._parse(self._response(content='not json'), structured=True)

    def test_reasoning_preserved_in_metadata(self):
        response = self._response(extra_message={
            'reasoning_content': 'rc', 'reasoning_details': [{'i': 1}],
        })
        result = self._parse(response)
        meta = result['result']['provider_metadata']['ai_provider_router']
        self.assertEqual(meta['reasoning_content'], 'rc')
        self.assertEqual(meta['reasoning_details'], [{'i': 1}])
        self.assertEqual(result['result']['content'], [{'type': 'text', 'text': 'ROUTER_OK'}])


@tagged("post_install", "-at_install", "ai_provider_router")
class TestHeaders(TransactionCase):

    def test_headers_generic_provider(self):
        config = {
            'api_key': 'k',
            'endpoint_url': 'https://openrouter.ai/api/v1/chat/completions',
        }
        headers = provider.build_headers(config, session_ref=7)
        self.assertEqual(headers['Authorization'], 'Bearer k')
        self.assertEqual(headers['User-Agent'], provider.USER_AGENT)
        self.assertNotIn('x-opencode-session', headers)

    def test_headers_opencode_session(self):
        config = {
            'api_key': 'k',
            'endpoint_url': 'https://opencode.ai/zen/go/v1/chat/completions',
        }
        headers = provider.build_headers(config, session_ref=7)
        self.assertEqual(headers['x-opencode-session'], 'odoo-ai-7')
        headers = provider.build_headers(config)
        self.assertEqual(headers['x-opencode-session'], 'odoo-ai-default')

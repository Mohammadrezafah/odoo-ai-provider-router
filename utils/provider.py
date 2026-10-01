import json
import re
from urllib.parse import urlsplit, urlunsplit

from odoo.exceptions import UserError

PARAM_PREFIX = 'ai_provider_router.'
PARAM_ENABLED = PARAM_PREFIX + 'enabled'
PARAM_BASE_URL = PARAM_PREFIX + 'base_url'
PARAM_MODEL = PARAM_PREFIX + 'model'
PARAM_API_KEY = PARAM_PREFIX + 'api_key'

DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1'
CHAT_COMPLETIONS_SUFFIX = '/chat/completions'
LOOPBACK_HOSTS = {'127.0.0.1', '::1', 'localhost'}
UNSAFE_URL_CHARS = re.compile(r'[\s\x00-\x1f\x7f]')
KEY_UNSAFE_CHARS = re.compile(r'[\r\n]')
METADATA_KEY = 'ai_provider_router'


class ProviderError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


USER_AGENT = 'Odoo-AI-Provider-Router/20.0'
OPENCODE_HOST_SUFFIX = '.opencode.ai'
OPENCODE_HOST = 'opencode.ai'


def build_headers(router_config, session_ref=None):
    headers = {
        'Authorization': f"Bearer {router_config['api_key']}",
        'Content-Type': 'application/json',
        'User-Agent': USER_AGENT,
    }
    host = urlsplit(router_config['endpoint_url']).hostname or ''
    if host == OPENCODE_HOST or host.endswith(OPENCODE_HOST_SUFFIX):
        headers['x-opencode-session'] = f'odoo-ai-{session_ref or "default"}'
    return headers


def _invalid_url():
    return UserError("The AI provider base URL is not valid. Use an HTTPS URL, "
                     "or HTTP only for 127.0.0.1, ::1 or localhost, without "
                     "credentials, query or fragment.")


def normalize_base_url(base_url):
    if not isinstance(base_url, str):
        raise _invalid_url()
    url = base_url.strip()
    if not url or UNSAFE_URL_CHARS.search(url):
        raise _invalid_url()
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise _invalid_url() from None
    if parts.username or parts.password:
        raise _invalid_url()
    if parts.query or parts.fragment:
        raise _invalid_url()
    if not parts.scheme or not parts.netloc or not hostname:
        raise _invalid_url()
    if port is not None and not (0 < port < 65536):
        raise _invalid_url()
    if parts.scheme == 'http':
        if hostname not in LOOPBACK_HOSTS:
            raise _invalid_url()
    elif parts.scheme != 'https':
        raise _invalid_url()
    path = parts.path.rstrip('/')
    if path.endswith(CHAT_COMPLETIONS_SUFFIX):
        raise _invalid_url()
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def chat_completions_url(base_url):
    return normalize_base_url(base_url) + CHAT_COMPLETIONS_SUFFIX


def get_router_config(env):
    """Return a plain-scalar provider config dict when routing is enabled, else None."""
    icp = env['ir.config_parameter'].sudo()
    if not icp.get_bool(PARAM_ENABLED, False):
        return None
    base_url = icp.get_str(PARAM_BASE_URL, '')
    model = icp.get_str(PARAM_MODEL, '')
    api_key = icp.get_str(PARAM_API_KEY, '')
    if not (base_url and model and api_key):
        raise UserError(env._(
            "AI provider routing is enabled but not fully configured. "
            "An administrator must set the base URL, model and API key in Settings."
        ))
    normalized = normalize_base_url(base_url)
    return {
        'base_url': normalized,
        'endpoint_url': normalized + CHAT_COMPLETIONS_SUFFIX,
        'model': model,
        'api_key': api_key,
    }


def _serialize_text_parts(parts):
    texts = [part['text'] for part in parts]
    serialized = [
        text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
        for text in texts
    ]
    return serialized[0] if len(serialized) == 1 else "\n".join(serialized)


def _tool_result_content(result_parts):
    text_parts = [part for part in result_parts if part['type'] == 'text']
    if len(text_parts) != len(result_parts):
        return json.dumps(result_parts, ensure_ascii=False)
    return _serialize_text_parts(text_parts)


def _map_user_message(message):
    mapped = []
    text_buffer = []

    def flush_buffer():
        if text_buffer:
            mapped.append({'role': 'user', 'content': _serialize_text_parts(text_buffer)})
            text_buffer.clear()

    for part in message['content']:
        part_type = part['type']
        if part_type == 'text':
            text_buffer.append(part)
        elif part_type == 'tool_result':
            flush_buffer()
            mapped.append({
                'role': 'tool',
                'tool_call_id': str(part['tool_call_id']),
                'content': _tool_result_content(part['result']),
            })
        elif part_type == 'inline_data':
            raise UserError("AI provider routing does not support inline data inputs.")
        else:
            raise UserError("AI provider routing does not support message part type '%s'." % part_type)
    flush_buffer()
    return mapped


def _map_assistant_message(message, provider_meta):
    text_parts = []
    tool_calls = []
    for part in message['content']:
        part_type = part['type']
        if part_type == 'text':
            text_parts.append(part)
        elif part_type == 'tool_call':
            tool_calls.append({
                'id': str(part['call_id']),
                'type': 'function',
                'function': {
                    'name': part['name'],
                    'arguments': json.dumps(part['args'], ensure_ascii=False),
                },
            })
        elif part_type == 'inline_data':
            raise UserError("AI provider routing does not support inline data inputs.")
        else:
            raise UserError("AI provider routing does not support message part type '%s'." % part_type)
    mapped = {'role': 'assistant', 'content': _serialize_text_parts(text_parts) if text_parts else None}
    if tool_calls:
        mapped['tool_calls'] = tool_calls
    elif mapped['content'] is None:
        mapped['content'] = ''
    stored_meta = (message.get('provider_metadata') or {}).get(METADATA_KEY) or {}
    if (
        stored_meta.get('base_url') == provider_meta['base_url']
        and stored_meta.get('model') == provider_meta['model']
    ):
        for key in ('reasoning_content', 'reasoning_details'):
            if key in stored_meta:
                mapped[key] = stored_meta[key]
    return mapped


def build_request_body(*, model, provider_meta, messages, instructions, tools=None, options=None):
    options = options or {}
    unsupported = [
        option for option in ('image_generation', 'web_grounding', 'boost_reasoning', 'aspect_ratio')
        if options.get(option)
    ]
    if unsupported:
        raise UserError(
            "AI provider routing does not support the requested option(s): %s."
            % ", ".join(sorted(unsupported))
        )
    out_messages = [{'role': 'system', 'content': instructions or ''}]
    for message in messages:
        role = message['role']
        if role == 'user':
            out_messages.extend(_map_user_message(message))
        elif role == 'assistant':
            out_messages.append(_map_assistant_message(message, provider_meta))
        else:
            raise UserError("AI provider routing does not support message role '%s'." % role)
    body = {
        'model': model,
        'messages': out_messages,
        'stream': False,
    }
    if tools:
        body['tools'] = [
            {
                'type': 'function',
                'function': {
                    'name': tool['name'],
                    'description': tool.get('instructions') or '',
                    'parameters': tool.get('schema') or {'type': 'object', 'properties': {}},
                },
            }
            for tool in tools
        ]
        body['tool_choice'] = 'auto'
    if options.get('schema'):
        body['response_format'] = {
            'type': 'json_schema',
            'json_schema': {'name': 'odoo_ai_response', 'schema': options['schema']},
        }
    return body


def _fail(code):
    raise ProviderError(code)


def parse_response(response_json, *, provider_meta, offered_tool_names=(), structured=False):
    if not isinstance(response_json, dict):
        _fail('invalid_response')
    if response_json.get('error'):
        _fail('provider_error')
    choices = response_json.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        _fail('empty_choices')
    choice = choices[0]
    finish_reason = choice.get('finish_reason')
    if finish_reason in ('length', 'content_filter'):
        _fail('finish_' + finish_reason)
    message = choice.get('message')
    if not isinstance(message, dict):
        _fail('malformed_message')
    content = message.get('content')
    content_parts = []
    if content is None:
        pass
    elif isinstance(content, str):
        if content:
            content_parts.append({'type': 'text', 'text': content})
    elif isinstance(content, list):
        for entry in content:
            if (
                isinstance(entry, dict)
                and entry.get('type') == 'text'
                and isinstance(entry.get('text'), str)
            ):
                content_parts.append({'type': 'text', 'text': entry['text']})
            else:
                _fail('malformed_content')
    else:
        _fail('malformed_content')
    offered = set(offered_tool_names)
    seen_call_ids = set()
    tool_call_parts = []
    for tool_call in message.get('tool_calls') or []:
        if not isinstance(tool_call, dict) or tool_call.get('type') != 'function':
            _fail('unsupported_tool_call')
        call_id = tool_call.get('id')
        if not isinstance(call_id, str) or not call_id:
            _fail('missing_tool_call_id')
        if call_id in seen_call_ids:
            _fail('duplicate_tool_call_id')
        seen_call_ids.add(call_id)
        function = tool_call.get('function')
        if not isinstance(function, dict):
            _fail('malformed_tool_call')
        name = function.get('name')
        if not isinstance(name, str) or not name:
            _fail('missing_tool_name')
        if name not in offered:
            _fail('unknown_tool')
        try:
            args = json.loads(function.get('arguments') or '')
        except (TypeError, ValueError):
            _fail('malformed_tool_arguments')
        if not isinstance(args, dict):
            _fail('malformed_tool_arguments')
        tool_call_parts.append({
            'type': 'tool_call',
            'name': name,
            'args': args,
            'call_id': call_id,
        })
    content_parts.extend(tool_call_parts)
    if not content_parts:
        _fail('empty_reply')
    if structured:
        for part in content_parts:
            if part['type'] != 'text':
                continue
            try:
                part['text'] = json.loads(part['text'])
            except (TypeError, ValueError):
                _fail('invalid_structured_json')
    provider_metadata = {
        METADATA_KEY: {
            'base_url': provider_meta['base_url'],
            'model': provider_meta['model'],
        },
    }
    for key in ('reasoning_content', 'reasoning_details'):
        if key in message and message[key] is not None:
            provider_metadata[METADATA_KEY][key] = message[key]
    return {
        'result': {
            'role': 'assistant',
            'content': content_parts,
            'provider_metadata': provider_metadata,
        },
    }

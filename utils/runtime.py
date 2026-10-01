import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from odoo.tools import config, hmac

from .provider import ProviderError, build_headers, parse_response

_logger = logging.getLogger(__name__)

MAX_WORKERS = 4
MAX_PENDING = 16
PROVIDER_TIMEOUT = (5, 60)
CALLBACK_TIMEOUT = 15
CALLBACK_ATTEMPTS = 2
CALLBACK_PATH = '/ai/completion_result_ready'

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix='ai_provider_router')
_pending = threading.Semaphore(MAX_PENDING)


def _new_session():
    session = requests.Session()
    session.trust_env = False
    return session


def post_chat_completions(router_config, body, session_ref=None):
    """Perform one provider call and return the decoded JSON body."""
    session = _new_session()
    try:
        response = session.post(
            router_config['endpoint_url'],
            headers=build_headers(router_config, session_ref=session_ref),
            json=body,
            timeout=PROVIDER_TIMEOUT,
            allow_redirects=False,
        )
    except requests.Timeout:
        raise ProviderError('timeout') from None
    except requests.RequestException:
        raise ProviderError('unreachable') from None
    finally:
        session.close()
    if response.status_code != 200:
        raise ProviderError('http_error')
    try:
        return response.json()
    except ValueError:
        raise ProviderError('invalid_json') from None


def _callback_url():
    return f"http://127.0.0.1:{config['http_port']}{CALLBACK_PATH}"


def _signed_callback_payload(request_uuid, webhook_secret, llm_result, llm_error):
    return {
        'request_uuid': request_uuid,
        'llm_result': llm_result,
        'llm_error': llm_error,
        'signature': hmac(
            None, 'odoo_ai-webhook',
            (request_uuid, llm_result, llm_error),
            secret=webhook_secret,
        ),
    }


def deliver_callback(dbname, request_uuid, webhook_secret, llm_result, llm_error):
    """POST the signed plain-JSON payload to the native completion callback."""
    payload = _signed_callback_payload(request_uuid, webhook_secret, llm_result, llm_error)
    for attempt in range(CALLBACK_ATTEMPTS):
        session = _new_session()
        try:
            response = session.post(
                _callback_url(),
                json=payload,
                headers={'X-Odoo-Database': dbname},
                timeout=CALLBACK_TIMEOUT,
                allow_redirects=False,
            )
            if response.status_code < 500:
                return True
            _logger.warning("AI provider callback for request %s got status %s", request_uuid, response.status_code)
        except requests.RequestException:
            _logger.warning("AI provider callback for request %s failed (attempt %s)", request_uuid, attempt + 1)
        finally:
            session.close()
    return False


def _deliver_failure_async(job):
    thread = threading.Thread(
        target=_deliver_failure_safe, args=(job,),
        name='ai_provider_router_fail', daemon=True,
    )
    thread.start()


def _deliver_failure_safe(job):
    try:
        deliver_callback(job['dbname'], job['request_uuid'], job['webhook_secret'], False, True)
    except Exception:  # noqa: BLE001
        _logger.exception("AI provider failure callback for request %s could not be delivered", job['request_uuid'])


def _run_job(job):
    try:
        try:
            data = post_chat_completions(job['config'], job['body'], session_ref=job.get('session_ref'))
            llm_result = parse_response(
                data,
                provider_meta={
                    'base_url': job['config']['base_url'],
                    'model': job['config']['model'],
                },
                offered_tool_names=job['offered_tool_names'],
                structured=job['structured'],
            )
            llm_error = False
        except ProviderError as exc:
            _logger.warning("AI provider request %s failed: %s", job['request_uuid'], exc.code)
            llm_result, llm_error = False, True
        except Exception:  # noqa: BLE001
            _logger.exception("AI provider request %s failed unexpectedly", job['request_uuid'])
            llm_result, llm_error = False, True
        try:
            deliver_callback(job['dbname'], job['request_uuid'], job['webhook_secret'], llm_result, llm_error)
        except Exception:  # noqa: BLE001
            _logger.exception("AI provider callback for request %s could not be delivered", job['request_uuid'])
    finally:
        _pending.release()


def submit_async_request(job):
    """Enqueue a provider request job; the queue is bounded by a semaphore."""
    if not _pending.acquire(blocking=False):
        _logger.warning("AI provider request %s rejected: queue full", job['request_uuid'])
        _deliver_failure_async(job)
        return
    try:
        _executor.submit(_run_job, job)
    except Exception:  # noqa: BLE001
        _pending.release()
        _logger.exception("AI provider request %s could not be scheduled", job['request_uuid'])
        _deliver_failure_async(job)

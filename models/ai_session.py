import copy
import uuid

from odoo import models
from odoo.exceptions import UserError

from ..utils import provider as provider_utils
from ..utils import runtime


class AiSession(models.Model):
    _inherit = 'ai.session'

    def _ai_router_config(self):
        return provider_utils.get_router_config(self.env)

    def _save_and_submit_request(
        self, payload, *, callback_type,
        request_round, request_round_limit,
        state,
    ) -> None:
        router_config = self._ai_router_config()
        if router_config is None:
            return super()._save_and_submit_request(
                payload, callback_type=callback_type,
                request_round=request_round, request_round_limit=request_round_limit,
                state=state,
            )
        self.ensure_one()
        options = {
            key: value for key, value in payload.items()
            if key not in ('messages', 'instructions', 'tools')
        }
        provider_meta = {'base_url': router_config['base_url'], 'model': router_config['model']}
        body = provider_utils.build_request_body(
            model=router_config['model'],
            provider_meta=provider_meta,
            messages=copy.deepcopy(payload.get('messages') or []),
            instructions=payload.get('instructions'),
            tools=payload.get('tools'),
            options=options,
        )
        job = {
            'config': router_config,
            'body': body,
            'offered_tool_names': {tool['name'] for tool in (payload.get('tools') or [])},
            'structured': bool(options.get('schema')),
            'dbname': self.env.cr.dbname,
            'session_ref': self.id,
        }
        guest = self.env.context.get("guest")
        vals = {
            "loop_state": "waiting_model",
            "request_uuid": str(uuid.uuid4()),
            "request_webhook_secret": uuid.uuid4().hex,
            "request_round": request_round,
            "request_round_limit": request_round_limit,
            "request_payload": payload,
            "request_user_id": self.env.uid,
            "request_guest_id": guest.id if guest else False,
            "request_context": self._get_request_context_snapshot(),
            "resume_token": False,
            "pending_tool_call": False,
        }
        state = copy.deepcopy(state)
        state["callback_type"] = callback_type
        vals["state"] = state
        self.write(vals)
        self._publish_response_state()
        job['request_uuid'] = vals["request_uuid"]
        job['webhook_secret'] = vals["request_webhook_secret"]

        @self.env.cr.postcommit.add
        def submit():
            runtime.submit_async_request(job)

    def _get_completions(self, messages, instructions, tools=None, **options):
        router_config = self._ai_router_config()
        if router_config is None:
            return super()._get_completions(messages, instructions, tools=tools, **options)
        try:
            body = provider_utils.build_request_body(
                model=router_config['model'],
                provider_meta={'base_url': router_config['base_url'], 'model': router_config['model']},
                messages=copy.deepcopy(messages),
                instructions=instructions,
                tools=tools,
                options=options,
            )
            data = runtime.post_chat_completions(
                router_config, body, session_ref=self.id if len(self) == 1 else None
            )
            return provider_utils.parse_response(
                data,
                provider_meta={'base_url': router_config['base_url'], 'model': router_config['model']},
                offered_tool_names={tool['name'] for tool in (tools or [])},
                structured=bool(options.get('schema')),
            )
        except provider_utils.ProviderError as exc:
            raise UserError(self.env._(
                "The configured AI provider request failed (%s).", exc.code
            )) from None

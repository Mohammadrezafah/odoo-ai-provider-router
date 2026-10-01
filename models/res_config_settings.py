from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..utils import provider as provider_utils


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ai_router_enabled = fields.Boolean(
        'Enable AI Provider Routing', groups='base.group_system',
    )
    ai_router_base_url = fields.Char(
        'Provider Base URL', groups='base.group_system',
        default=provider_utils.DEFAULT_BASE_URL,
    )
    ai_router_model = fields.Char(
        'Provider Model', groups='base.group_system',
    )
    ai_router_api_key = fields.Char(
        'Provider API Key', groups='base.group_system',
    )
    ai_router_has_api_key = fields.Boolean(
        'API Key Configured', groups='base.group_system', readonly=True,
    )
    ai_router_clear_api_key = fields.Boolean(
        'Clear API Key', groups='base.group_system',
    )

    def _ai_router_check_admin(self):
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(self.env._(
                "Only administrators can manage AI provider routing settings."
            ))

    @api.model
    def get_values(self):
        self._ai_router_check_admin()
        res = super().get_values()
        icp = self.env['ir.config_parameter'].sudo()
        res.update({
            'ai_router_enabled': icp.get_bool(provider_utils.PARAM_ENABLED, False),
            'ai_router_base_url': (
                icp.get_str(provider_utils.PARAM_BASE_URL, '')
                or provider_utils.DEFAULT_BASE_URL
            ),
            'ai_router_model': icp.get_str(provider_utils.PARAM_MODEL, ''),
            'ai_router_api_key': False,
            'ai_router_has_api_key': bool(icp.get_str(provider_utils.PARAM_API_KEY)),
            'ai_router_clear_api_key': False,
        })
        return res

    def set_values(self):
        self._ai_router_check_admin()
        icp = self.env['ir.config_parameter'].sudo()
        enabled = bool(self.ai_router_enabled)
        base_url = (self.ai_router_base_url or '').strip()
        model = (self.ai_router_model or '').strip()
        new_key = self.ai_router_api_key or ''
        clear_key = bool(self.ai_router_clear_api_key)
        stored_key = icp.get_str(provider_utils.PARAM_API_KEY)

        if provider_utils.KEY_UNSAFE_CHARS.search(new_key):
            raise ValidationError(self.env._("The API key must not contain line breaks."))
        normalized_base_url = provider_utils.normalize_base_url(base_url) if base_url else ''
        effective_key = new_key or ('' if clear_key else stored_key)
        if enabled:
            if not normalized_base_url:
                raise ValidationError(self.env._("A valid provider base URL is required to enable AI provider routing."))
            if not model:
                raise ValidationError(self.env._("A provider model is required to enable AI provider routing."))
            if not effective_key:
                raise ValidationError(self.env._(
                    "An API key is required to enable AI provider routing. "
                    "Enter a new key or disable routing before clearing the stored key."
                ))

        super().set_values()
        icp.set_bool(provider_utils.PARAM_ENABLED, enabled)
        icp.set_str(provider_utils.PARAM_BASE_URL, normalized_base_url)
        icp.set_str(provider_utils.PARAM_MODEL, model)
        if new_key:
            icp.set_str(provider_utils.PARAM_API_KEY, new_key)
        elif clear_key:
            icp.set_str(provider_utils.PARAM_API_KEY, None)
        if self.ai_router_api_key:
            self.ai_router_api_key = False
        self.ai_router_clear_api_key = False

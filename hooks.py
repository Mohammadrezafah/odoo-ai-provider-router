import logging

_logger = logging.getLogger(__name__)


def uninstall_hook(env):
    """Remove every AI provider routing parameter, including the stored API key."""
    icp = env['ir.config_parameter'].sudo()
    from .utils import provider as provider_utils
    for key in (
        provider_utils.PARAM_ENABLED,
        provider_utils.PARAM_BASE_URL,
        provider_utils.PARAM_MODEL,
        provider_utils.PARAM_API_KEY,
        provider_utils.PARAM_TIMEOUT_S,
    ):
        icp.set_str(key, None)
    _logger.info("AI provider router parameters removed on uninstall")

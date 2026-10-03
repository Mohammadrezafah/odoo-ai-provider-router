from odoo.fields import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.ai_provider_router.utils import provider


@tagged("post_install", "-at_install", "ai_provider_router")
class TestAIRouterSettings(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.icp = cls.env['ir.config_parameter'].sudo()
        cls.user_internal = cls.env['res.users'].create({
            'name': 'User internal',
            'login': 'ai_router_internal_user',
            'email': 'ai_router_internal_user@user.com',
            'group_ids': [Command.set(cls.env.ref('base.group_user').ids)],
        })

    def _clear_params(self):
        for key in (
            provider.PARAM_ENABLED, provider.PARAM_BASE_URL, provider.PARAM_MODEL,
            provider.PARAM_API_KEY, provider.PARAM_TIMEOUT_S,
        ):
            self.icp.set_str(key, None)

    def setUp(self):
        super().setUp()
        self._clear_params()

    def _save(self, **values):
        wizard = self.env['res.config.settings'].create(values)
        wizard.set_values()
        return wizard

    def _stored(self):
        return {
            'enabled': self.icp.get_bool(provider.PARAM_ENABLED, False),
            'base_url': self.icp.get_str(provider.PARAM_BASE_URL, ''),
            'model': self.icp.get_str(provider.PARAM_MODEL, ''),
            'api_key': self.icp.get_str(provider.PARAM_API_KEY, ''),
        }

    def test_settings_fields_exist_and_default_disabled(self):
        settings_fields = self.env['res.config.settings']._fields
        for fname in (
            'ai_router_enabled', 'ai_router_base_url', 'ai_router_model',
            'ai_router_api_key', 'ai_router_has_api_key', 'ai_router_clear_api_key',
        ):
            self.assertIn(fname, settings_fields)
        wizard = self.env['res.config.settings'].create({})
        self.assertFalse(wizard.ai_router_enabled)
        self.assertEqual(wizard.ai_router_base_url, 'https://openrouter.ai/api/v1')
        self.assertFalse(wizard.ai_router_model)
        self.assertFalse(wizard.ai_router_api_key)
        self.assertFalse(wizard.ai_router_has_api_key)

    def test_save_endpoint_model_key_disabled(self):
        self._save(
            ai_router_base_url='http://127.0.0.1:8081/v1',
            ai_router_model='unit-test-model',
            ai_router_api_key='dummy-ai-router-test-key',
        )
        stored = self._stored()
        self.assertFalse(stored['enabled'])
        self.assertEqual(stored['base_url'], 'http://127.0.0.1:8081/v1')
        self.assertEqual(stored['model'], 'unit-test-model')
        self.assertEqual(stored['api_key'], 'dummy-ai-router-test-key')

    def test_enable_requires_complete_config(self):
        for values, missing in (
            ({'ai_router_enabled': True}, 'model'),
            ({'ai_router_enabled': True, 'ai_router_model': 'unit-test-model'}, 'api key'),
            ({'ai_router_enabled': True, 'ai_router_base_url': 'http://127.0.0.1:8081/v1',
              'ai_router_api_key': 'dummy-ai-router-test-key'}, 'model'),
        ):
            with self.assertRaises(ValidationError):
                self._save(**values)
            self.assertEqual(self._stored(), {
                'enabled': False, 'base_url': '', 'model': '', 'api_key': '',
            })

    def test_enable_invalid_url_has_no_side_effects(self):
        for url in ('http://example.com/v1', 'https://user:pw@example.com/v1', 'example.com', ''):
            with self.assertRaises((UserError, ValidationError), msg=url):
                self._save(
                    ai_router_enabled=True,
                    ai_router_base_url=url,
                    ai_router_model='unit-test-model',
                    ai_router_api_key='dummy-ai-router-test-key',
                )
            self.assertEqual(self._stored(), {
                'enabled': False, 'base_url': '', 'model': '', 'api_key': '',
            })

    def test_key_lifecycle(self):
        self._save(
            ai_router_base_url='http://127.0.0.1:8081/v1',
            ai_router_model='unit-test-model',
            ai_router_api_key='dummy-ai-router-test-key',
        )
        self.assertEqual(self._stored()['api_key'], 'dummy-ai-router-test-key')
        # blank preserves
        self._save(ai_router_api_key='')
        self.assertEqual(self._stored()['api_key'], 'dummy-ai-router-test-key')
        # replace
        self._save(ai_router_api_key='dummy-ai-router-test-key-2')
        self.assertEqual(self._stored()['api_key'], 'dummy-ai-router-test-key-2')
        # clear while disabled
        self._save(ai_router_clear_api_key=True)
        self.assertEqual(self._stored()['api_key'], '')
        wizard = self.env['res.config.settings'].create({})
        self.assertFalse(wizard.ai_router_has_api_key)

    def test_clear_key_while_enabled_rejected(self):
        self._save(
            ai_router_enabled=True,
            ai_router_base_url='http://127.0.0.1:8081/v1',
            ai_router_model='unit-test-model',
            ai_router_api_key='dummy-ai-router-test-key',
        )
        with self.assertRaises(ValidationError):
            self._save(
                ai_router_enabled=True, ai_router_clear_api_key=True,
                ai_router_base_url='http://127.0.0.1:8081/v1',
                ai_router_model='unit-test-model',
            )
        self.assertEqual(self._stored()['api_key'], 'dummy-ai-router-test-key')
        # replacing in the same save is allowed
        self._save(
            ai_router_enabled=True, ai_router_clear_api_key=True,
            ai_router_api_key='dummy-ai-router-test-key-3',
        )
        self.assertEqual(self._stored()['api_key'], 'dummy-ai-router-test-key-3')

    def test_key_rejects_crlf(self):
        for key in ('bad\nkey', 'bad\rkey'):
            with self.assertRaises(ValidationError):
                self._save(ai_router_api_key=key)
        self.assertEqual(self._stored()['api_key'], '')

    def test_read_saved_wizard_never_exposes_key(self):
        wizard = self._save(
            ai_router_base_url='http://127.0.0.1:8081/v1',
            ai_router_model='unit-test-model',
            ai_router_api_key='dummy-ai-router-test-key',
        )
        wizard.invalidate_recordset()
        self.assertFalse(wizard.ai_router_api_key)
        self.assertTrue(self._stored()['api_key'])
        defaults = self.env['res.config.settings'].default_get([
            'ai_router_api_key', 'ai_router_has_api_key',
        ])
        self.assertFalse(defaults['ai_router_api_key'])
        self.assertTrue(defaults['ai_router_has_api_key'])
        fresh = self.env['res.config.settings'].create({})
        self.assertFalse(fresh.ai_router_api_key)
        self.assertTrue(fresh.ai_router_has_api_key)

    def test_non_admin_denied(self):
        wizard = self.env['res.config.settings'].create({})
        internal_settings = self.env['res.config.settings'].with_user(self.user_internal)
        with self.assertRaises(AccessError):
            internal_settings.get_values()
        with self.assertRaises(AccessError):
            wizard.with_user(self.user_internal).set_values()
        with self.assertRaises(AccessError):
            wizard.with_user(self.user_internal).ai_router_enabled

    def test_invalid_url_saved_when_disabled(self):
        self._save(ai_router_base_url='not a url', ai_router_model='unit-test-model')
        stored = self._stored()
        self.assertFalse(stored['enabled'])
        self.assertEqual(stored['base_url'], 'not a url')
        # enabling with the stored garbage still fails
        with self.assertRaises((UserError, ValidationError)):
            self._save(
                ai_router_enabled=True,
                ai_router_base_url='not a url',
                ai_router_model='unit-test-model',
                ai_router_api_key='dummy-ai-router-test-key',
            )

    def test_timeout_parameter_clamped(self):
        self._save(
            ai_router_enabled=True,
            ai_router_base_url='http://127.0.0.1:8081/v1',
            ai_router_model='unit-test-model',
            ai_router_api_key='dummy-ai-router-test-key',
        )
        env = self.env

        def read_timeout(raw):
            if raw is None:
                self.icp.set_str(provider.PARAM_TIMEOUT_S, None)
            else:
                self.icp.set_str(provider.PARAM_TIMEOUT_S, raw)
            return provider.get_router_config(env)['timeout'][1]

        self.assertEqual(read_timeout(None), provider.DEFAULT_READ_TIMEOUT_S)
        self.assertEqual(read_timeout('garbage'), provider.DEFAULT_READ_TIMEOUT_S)
        self.assertEqual(read_timeout('300'), 300)
        self.assertEqual(read_timeout('2'), provider.MIN_READ_TIMEOUT_S)
        self.assertEqual(read_timeout('99999'), provider.MAX_READ_TIMEOUT_S)

    def test_uninstall_hook_clears_params(self):
        self._save(
            ai_router_enabled=True,
            ai_router_base_url='http://127.0.0.1:8081/v1',
            ai_router_model='unit-test-model',
            ai_router_api_key='dummy-ai-router-test-key',
        )
        from odoo.addons.ai_provider_router.hooks import uninstall_hook
        uninstall_hook(self.env)
        self.assertEqual(self._stored(), {
            'enabled': False, 'base_url': '', 'model': '', 'api_key': '',
        })
        self.assertEqual(self.icp.get_str(provider.PARAM_TIMEOUT_S, ''), '')

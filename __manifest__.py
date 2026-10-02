{
    'name': 'AI Provider Router',
    'version': '20.0.1.1.0',
    'category': 'Productivity',
    'summary': 'Route Ask AI completions to an OpenAI-compatible API',
    'author': 'Mohammad Reza Fahlepi',
    'website': 'https://github.com/Mohammadrezafah/odoo-ai-provider-router',
    'depends': ['ai', 'base_setup'],
    'data': [
        'views/res_config_settings_views.xml',
    ],
    'uninstall_hook': 'uninstall_hook',
    'license': 'LGPL-3',
}

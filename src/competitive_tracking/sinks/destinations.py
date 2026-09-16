from copy import deepcopy


def destination_config(config, platform):
    """Give existing sink implementations a platform-specific destination configuration."""
    result = deepcopy(config)
    for section in ('feishu_output', 'feishu_sheets'):
        if platform == config.get(section, {}).get('platform'):
            continue
        name = f'{platform}_{section}'
        result[section] = deepcopy(config.get(name, {'enabled': False, 'platform': platform}))
    return result

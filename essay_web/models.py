from decimal import Decimal

# Explicit capabilities: do not infer endpoint or supported parameters from names.
# Standard short-context text rates, USD per million tokens, 2026-10-16.
MODELS = {
    'gpt-6-luna': {'endpoint': 'responses', 'reasoning': 'medium', 'input': '0.10', 'cached': '0.01', 'write': '0.125', 'output': '0.50'},
    'gpt-4.1-mini': {'endpoint': 'chat', 'temperature': 0, 'input': '0.40', 'cached': '0.10', 'write': None, 'output': '1.60'},
    'gpt-5.6-luna': {'endpoint': 'responses', 'reasoning': 'medium', 'input': '0.20', 'cached': None, 'write': None, 'output': '1.20'},
    'gpt-5.6-terra': {'endpoint': 'responses', 'reasoning': 'medium', 'input': '2.00', 'cached': None, 'write': None, 'output': '12.00'},
}
PRICE_VERSION = '2026-10-16-standard-text-v1'


def price(usage, model):
    """Reasoning is already included in output; cache subsets are not additive."""
    p = MODELS[model]
    if usage is None or usage['input'] is None or usage['output'] is None:
        return None
    cached, writes = usage.get('cached', 0), usage.get('cache_write', 0)
    if cached is None or writes is None:
        return None
    if cached and p['cached'] is None or writes and p['write'] is None:
        return None  # Unknown rate must not silently become zero.
    if min(usage['input'], usage['output'], cached, writes) < 0 or cached + writes > usage['input']:
        return None
    return (Decimal(usage['input'] - cached - writes) * Decimal(p['input'])
            + Decimal(cached) * Decimal(p['cached'] or '0')
            + Decimal(writes) * Decimal(p['write'] or '0')
            + Decimal(usage['output']) * Decimal(p['output'])) / Decimal(1000000)


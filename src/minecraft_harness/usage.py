"""Versioned provider usage and non-overlapping cost components.

Unknown cache/reasoning quantities remain null. Provider totals are never added
again as components; standard Chat Completions details are subsets of totals.
"""
from __future__ import annotations

import math

USAGE_VERSION = 'usage-v2'


def token(value):
    return value if type(value) is int and 0 <= value <= 2**63-1 else None


def _first(mapping, *keys):
    for key in keys:
        if key in mapping:
            return token(mapping[key])
    return None


def normalize_usage(raw):
    raw = raw if isinstance(raw, dict) else {}
    # Re-normalization is supported without trusting a provider-supplied
    # version marker or adding separately reported cache/reasoning twice.
    normalized = raw.get('usage_version') == USAGE_VERSION
    inp = _first(raw, 'reported_input_tokens') if normalized else _first(raw, 'input_tokens', 'prompt_tokens')
    out = _first(raw, 'reported_output_tokens') if normalized else _first(raw, 'output_tokens', 'completion_tokens')
    input_details = raw.get('input_tokens_details', raw.get('prompt_tokens_details', {}))
    output_details = raw.get('output_tokens_details', raw.get('completion_tokens_details', {}))
    input_details = input_details if isinstance(input_details, dict) else {}
    output_details = output_details if isinstance(output_details, dict) else {}
    cached = _first(raw, 'cached_input_tokens', 'cache_read_input_tokens')
    if cached is None:
        cached = _first(input_details, 'cached_tokens')
    reasoning = _first(raw, 'reasoning_tokens', 'reasoning_output_tokens')
    if reasoning is None:
        reasoning = _first(output_details, 'reasoning_tokens')
    cache_included = raw.get('cached_input_included', raw.get('input_tokens_include_cached'))
    if type(cache_included) is not bool:
        cache_included = True if ('cached_tokens' in input_details or token(raw.get('prompt_tokens')) is not None) else False if 'cache_read_input_tokens' in raw else None
    reasoning_included = raw.get('reasoning_included_in_output', raw.get('output_tokens_include_reasoning'))
    if type(reasoning_included) is not bool:
        reasoning_included = True if 'reasoning_tokens' in output_details or token(raw.get('completion_tokens')) is not None else None
    warnings = [value for value in raw.get('warnings',[])[:64] if isinstance(value,str)] if normalized and isinstance(raw.get('warnings'),list) else []
    # DeepSeek documents prompt_tokens = cache hits + cache misses. These are
    # input subsets, not additional input, and some responses expose only these
    # provider-specific fields rather than prompt_tokens_details.cached_tokens.
    # https://api-docs.deepseek.com/api/create-chat-completion/
    if not normalized and ('prompt_cache_hit_tokens' in raw or 'prompt_cache_miss_tokens' in raw):
        hit_present, miss_present = 'prompt_cache_hit_tokens' in raw, 'prompt_cache_miss_tokens' in raw
        hit, miss = token(raw.get('prompt_cache_hit_tokens')), token(raw.get('prompt_cache_miss_tokens'))
        invalid = False
        if hit_present and hit is None:
            warnings.append('invalid_prompt_cache_hit_tokens'); invalid = True
        if miss_present and miss is None:
            warnings.append('invalid_prompt_cache_miss_tokens'); invalid = True
        if any(key in mapping and token(mapping[key]) is None for mapping, key in (
                (raw, 'cached_input_tokens'), (raw, 'cache_read_input_tokens'), (input_details, 'cached_tokens'))):
            warnings.append('invalid_deepseek_cache_alias'); invalid = True
        if cache_included is False:
            warnings.append('deepseek_cache_inclusion_conflict'); invalid = True
        if hit is not None:
            if cached is not None and cached != hit:
                warnings.append('deepseek_cache_hit_alias_conflict'); invalid = True
            cached = hit
        elif not hit_present and miss is not None and inp is not None:
            # The total and a valid miss subtotal determine hits exactly.
            if miss <= inp:
                derived = inp - miss
                if cached is not None and cached != derived:
                    warnings.append('deepseek_cache_miss_alias_conflict'); invalid = True
                cached = derived
            else:
                warnings.append('prompt_cache_miss_exceeds_input_total'); invalid = True
        if inp is not None and miss is not None and cached is not None and cached + miss != inp:
            warnings.append('deepseek_cache_subtotals_mismatch'); invalid = True
        if invalid:
            # Contradictory/illegal telemetry cannot establish a discounted
            # price or a trusted input total. Never turn an invalid hit into 0.
            inp = cached = None
        cache_included = True
    if cached is not None and inp is not None and cache_included is True and cached > inp:
        warnings.append('cached_input_exceeds_input_total')
        inp = cached = None
    if reasoning is not None and out is not None and reasoning_included is True and reasoning > out:
        warnings.append('reasoning_exceeds_output_total')
        out = reasoning = None
    noncached = inp - cached if inp is not None and cached is not None and cache_included is True else inp if cache_included is False else None
    visible_output = out - reasoning if out is not None and reasoning is not None and reasoning_included is True else out if reasoning_included is False else None
    input_total = inp + cached if inp is not None and cached is not None and cache_included is False else inp if cache_included is not False and not (cached is not None and cache_included is None) else None
    output_total = out + reasoning if out is not None and reasoning is not None and reasoning_included is False else out if reasoning_included is not False and not (reasoning is not None and reasoning_included is None) else None
    cache_creation=token(raw.get('cache_creation_input_tokens'))
    if (not normalized and 'cache_creation_input_tokens' in raw and cache_creation!=0) or raw.get('unsupported_cache_creation'):
        # Cache writes can have a separate price and provider-specific total
        # semantics. Never silently price them as ordinary/cache-read input.
        input_total=noncached=None
        if 'cache_creation_semantics_unknown' not in warnings: warnings.append('cache_creation_semantics_unknown')
    cost = raw.get('cost_usd')
    cost = cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0 else None
    return {'usage_version': USAGE_VERSION, 'input_tokens': input_total, 'output_tokens': output_total,
            'reported_input_tokens': inp, 'reported_output_tokens': out,
            'cached_input_tokens': cached, 'noncached_input_tokens': noncached,
            'cache_creation_input_tokens':cache_creation,'unsupported_cache_creation':input_total is None and 'cache_creation_semantics_unknown' in warnings,
            'reasoning_tokens': reasoning, 'visible_output_tokens': visible_output,
            'cached_input_included': cache_included, 'reasoning_included_in_output': reasoning_included,
            'total_tokens': _first(raw, 'total_tokens'), 'cost_usd': cost, 'warnings': warnings}


def estimate_cost(raw, rates):
    usage = normalize_usage(raw)
    if usage.get('cost_usd') is not None:
        return {'cost_usd': usage['cost_usd'], 'complete': True, 'method': 'provider_reported', 'components': {}}
    rates = rates if isinstance(rates, dict) else {}
    components = {}
    def add(name, amount, rate_name):
        rate = rates.get(rate_name)
        valid_rate = isinstance(rate,(int,float)) and not isinstance(rate,bool) and math.isfinite(rate) and rate >= 0
        value = amount * (rate / 1_000_000) if amount is not None and valid_rate else None
        components[name] = value if value is not None and math.isfinite(value) else None
    if 'cached_input' in rates:
        add('noncached_input', usage['noncached_input_tokens'], 'input')
        add('cached_input', usage['cached_input_tokens'], 'cached_input')
    else:
        add('input', usage['input_tokens'], 'input')
    if 'reasoning' in rates:
        add('visible_output', usage['visible_output_tokens'], 'output')
        add('reasoning', usage['reasoning_tokens'], 'reasoning')
    else:
        add('output', usage['output_tokens'], 'output')
    complete = bool(components) and all(value is not None for value in components.values())
    return {'cost_usd': sum(components.values()) if complete else None, 'complete': complete,
            'method': 'frozen_component_rates', 'components': components}


def reservation_cost(max_input_tokens, max_output_tokens, rates):
    # Totals bound all input/output subsets. Use the largest applicable price;
    # do not reserve both a total and its cached/reasoning subset twice.
    return (max_input_tokens * max(rates.get('input', 0), rates.get('cached_input', 0))
            + max_output_tokens * max(rates.get('output', 0), rates.get('reasoning', 0))) / 1_000_000

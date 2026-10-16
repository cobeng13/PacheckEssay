import json
import math
from decimal import Decimal

from .legacy.rubric import _schema, build_user_content, extract_rubric_criteria
from .models import MODELS


class InvalidGrade(ValueError):
    pass


def request_for(config, question, response, limit):
    model = config['model']
    capability = MODELS[model]
    content = build_user_content(rubric=question.rubric, model_answer=question.model_answer,
        extra_instructions=question.extra_instructions, student_response=response,
        question_id=question.question_id, question_title=question.question_title, max_points=question.max_points)
    # UTF-8 byte count is a conservative token ceiling, plus message/schema overhead.
    input_bound = len((content + config['system_instruction'] + json.dumps(_schema(question.max_points))).encode('utf-8')) + 1024
    if input_bound > 60000:
        raise InvalidGrade('prompt_too_large')
    rate = capability
    reservation = (Decimal(input_bound) * max(Decimal(rate['input']), Decimal(rate['write'] or '0'))
                   + Decimal(limit) * Decimal(rate['output'])) / Decimal(1000000)
    if capability['endpoint'] == 'responses':
        schema = _schema(question.max_points)
        kwargs = dict(model=model, input=[{'role': 'system', 'content': config['system_instruction']},
                        {'role': 'user', 'content': content}], store=False,
                      reasoning={'effort': capability['reasoning']}, max_output_tokens=limit,
                      text={'format': {'type': 'json_schema', **schema}})
    else:
        kwargs = dict(model=model, messages=[{'role': 'system', 'content': config['system_instruction']},
                      {'role': 'user', 'content': content}], temperature=capability['temperature'],
                      max_completion_tokens=limit, store=False,
                      response_format={'type': 'json_schema', 'json_schema': _schema(question.max_points)})
    effective = {k: v for k, v in kwargs.items() if k in ('model', 'reasoning', 'temperature', 'max_output_tokens', 'max_completion_tokens', 'store')}
    return kwargs, effective, reservation


def get_usage(response, endpoint):
    data = response.model_dump() if hasattr(response, 'model_dump') else response
    usage = data.get('usage')
    if not usage:
        return None
    inp, out = ('input_tokens', 'output_tokens') if endpoint == 'responses' else ('prompt_tokens', 'completion_tokens')
    details = usage.get(inp.replace('_tokens', '_tokens_details')) or {}
    output_details = usage.get(out.replace('_tokens', '_tokens_details')) or {}
    return {'input': usage.get(inp), 'output': usage.get(out), 'cached': details.get('cached_tokens', 0),
            'cache_write': details.get('cache_write_tokens', usage.get('cache_creation_input_tokens', 0)),
            'reasoning': output_details.get('reasoning_tokens', 0), 'raw': usage}


def parse_grade(response, endpoint, question):
    data = response.model_dump() if hasattr(response, 'model_dump') else response
    if endpoint == 'responses':
        if data.get('status') != 'completed':
            raise InvalidGrade('incomplete_response')
        blocks = [block for item in data.get('output', []) for block in item.get('content', [])]
        if any(block.get('type') == 'refusal' for block in blocks):
            raise InvalidGrade('refusal')
        text = ''.join(block.get('text', '') for block in blocks if block.get('type') == 'output_text')
    else:
        choice = data['choices'][0]
        if choice.get('finish_reason') != 'stop' or choice['message'].get('refusal'):
            raise InvalidGrade('incomplete_or_refused')
        text = choice['message']['content']
    try:
        grade = json.loads(text)
        if set(grade) != {'score_total', 'criteria', 'feedback', 'flags'} or not isinstance(grade['feedback'], str):
            raise ValueError()
        def number(value):
            return type(value) in (int, float) and math.isfinite(value)
        total = grade['score_total']
        if not number(total) or not 0 <= total <= question.max_points or not isinstance(grade['criteria'], list) or not grade['criteria']:
            raise ValueError()
        for criterion in grade['criteria']:
            if set(criterion) != {'name', 'points_awarded', 'justification'} or not isinstance(criterion['name'], str) or not isinstance(criterion['justification'], str):
                raise ValueError()
            if not number(criterion['points_awarded']) or criterion['points_awarded'] < 0:
                raise ValueError()
        rubric = extract_rubric_criteria(question.rubric)
        if rubric:
            if len(rubric) != len(grade['criteria']):
                raise ValueError()
            for expected, actual in zip(rubric, grade['criteria']):
                if expected['name'].casefold() != actual['name'].strip().casefold() or actual['points_awarded'] > expected['max_points']:
                    raise ValueError()
        if not math.isclose(sum(c['points_awarded'] for c in grade['criteria']), total, abs_tol=0.000001):
            raise ValueError()
        if set(grade['flags']) != {'off_topic', 'too_short', 'suspected_plagiarism', 'policy_issue'} or any(type(v) is not bool for v in grade['flags'].values()):
            raise ValueError()
        return grade
    except (ValueError, TypeError, KeyError):
        raise InvalidGrade('invalid_grade') from None


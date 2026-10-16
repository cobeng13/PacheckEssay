import json
import math
import re
import secrets
import time
from pathlib import Path

from .legacy.csv_ingest import build_students, load_preview
from .legacy.questions import QuestionConfig
from .legacy.rubric import DEFAULT_SYSTEM_INSTRUCTION, resolve_effective_max_points
from .models import MODELS
from .store import digest, fingerprint


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def validate_config(config, settings, headers):
    if not isinstance(config, dict) or not isinstance(config.get('labels', {}), dict) or not isinstance(config.get('mapping', {}), dict):
        raise ValueError('Invalid grading configuration.')
    if not isinstance(config.get('questions', []), list) or any(not isinstance(q, dict) for q in config.get('questions', [])):
        raise ValueError('Questions must be a list of question configurations.')
    labels = {k: str(config.get('labels', {}).get(k, '')).strip() for k in ('faculty', 'department', 'course', 'assessment')}
    if not labels['faculty'] or any(len(v) > 160 for v in labels.values()):
        raise ValueError('Enter a faculty name; labels must be at most 160 characters.')
    model = config.get('model', settings.default_model)
    if model not in MODELS:
        raise ValueError('Select a supported model.')
    questions = []
    for index, raw in enumerate(config.get('questions', []), 1):
        columns = raw.get('columns', [])
        if not columns or not isinstance(columns, list) or any(c not in headers for c in columns):
            raise ValueError('Each question must map to existing response columns.')
        try:
            points = float(raw.get('max_points', 10))
        except (ValueError, TypeError):
            raise ValueError('Enter a numeric maximum score.') from None
        if not math.isfinite(points) or not 0 < points <= 10000:
            raise ValueError('Maximum points must be between 0 and 10000.')
        text_fields = {k: str(raw.get(k, '')).strip() for k in ('rubric', 'model_answer', 'extra_instructions', 'question_title')}
        if not text_fields['rubric'] or any(len(v) > 50000 for v in text_fields.values()):
            raise ValueError('Each question needs a rubric; text fields are limited to 50000 characters.')
        points = resolve_effective_max_points(text_fields['rubric'], points)
        if not math.isfinite(points) or not 0 < points <= 10000:
            raise ValueError('Rubric allocations must total between 0 and 10000.')
        questions.append(QuestionConfig(f'Q{index}', list(dict.fromkeys(columns)), points, **text_fields).to_dict())
    if not questions or len(questions) > 50:
        raise ValueError('Configure between 1 and 50 questions.')
    mapping = {k: config.get('mapping', {}).get(k) or None for k in ('first_col', 'last_col', 'name_col', 'id_col')}
    if any(v not in headers for v in mapping.values() if v):
        raise ValueError('Invalid identity column.')
    instruction = str(config.get('system_instruction', DEFAULT_SYSTEM_INSTRUCTION)).strip()
    if not instruction or len(instruction) > 10000:
        raise ValueError('System instruction must contain 1–10000 characters.')
    return {'labels': labels, 'model': model, 'questions': questions, 'mapping': mapping,
            'system_instruction': instruction, 'per_question': bool(config.get('per_question', False)),
            'per_student': bool(config.get('per_student', True))}


def load_batch(directory, config):
    upload = next(directory.glob('input.*'))
    cols = list(dict.fromkeys(c for q in config['questions'] for c in q['columns']))
    _, students = build_students(str(upload), response_columns=cols, **config['mapping'])
    # Row identity prevents duplicate/normalized IDs overwriting grades or reports.
    for student in students:
        student.student_id = f'row_{student.row_index}'
        student.safe_stem = f'row_{student.row_index:06d}'
    return students, [QuestionConfig(**q) for q in config['questions']]


def tasks_for(students, questions):
    for student in students:
        for question in questions:
            response = '\n\n-----\n\n'.join(student.responses_by_column[c] for c in question.columns if student.responses_by_column[c].strip()).strip()
            yield f'{student.row_index}:{question.question_id}', student, question, response


def batch_fingerprint(directory, config):
    upload = next(directory.glob('input.*'))
    return fingerprint({'input': digest(upload.read_bytes().hex()), 'config': config})


def submit(store, ident, session, config):
    directory = store.directory(ident)
    preview = load_preview(str(next(directory.glob('input.*'))))
    config = validate_config(config, store.settings, preview.headers)
    students, questions = load_batch(directory, config)
    total = len(students) * len(questions)
    if not 0 < total <= store.settings.max_tasks:
        raise ValueError(f'Batch must contain 1–{store.settings.max_tasks} student/question tasks.')
    token = secrets.token_urlsafe(32)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        draft = db.execute('SELECT * FROM drafts WHERE id=? AND session=?', (ident, digest(session))).fetchone()
        if not draft or draft['submitted'] or draft['created'] < time.time() - 86400:
            raise ValueError('Draft expired or already submitted.')
        if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= store.settings.max_queue:
            raise ValueError('Queue is full; try later.')
        write_json(directory / 'config.json', config)
        db.execute('INSERT INTO jobs(id,token,state,created,fingerprint,labels,model,total) VALUES (?,?,?,?,?,?,?,?)',
                   (ident, digest(token), 'queued', time.time(), batch_fingerprint(directory, config), json.dumps(config['labels']), config['model'], total))
        db.executemany('INSERT INTO tasks(job,id) VALUES (?,?)', [(ident, task) for task, *_ in tasks_for(students, questions)])
        db.execute('UPDATE drafts SET submitted=? WHERE id=?', (ident, ident))
    return token


def resume(store, ident, retry_unknown=False):
    directory = store.directory(ident)
    config = read_json(directory / 'config.json')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        job = db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()
        if job['state'] not in ('failed', 'cancelled', 'interrupted', 'partial') or job['ended'] < time.time() - 86400:
            raise ValueError('Job cannot be resumed.')
        if batch_fingerprint(directory, config) != job['fingerprint']:
            raise ValueError('Input or grading settings changed. Start a new job.')
        if db.execute("SELECT count(*) FROM tasks WHERE job=? AND state='unknown'", (ident,)).fetchone()[0] and not retry_unknown:
            raise ValueError('Uncertain calls may already have been billed. Explicitly confirm retry.')
        if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= store.settings.max_queue:
            raise ValueError('Queue is full; try later.')
        db.execute("UPDATE tasks SET state='pending' WHERE job=? AND state IN ('failed','unknown')", (ident,))
        db.execute("UPDATE jobs SET state='queued',cancel=0,ended=NULL,message='' WHERE id=?", (ident,))


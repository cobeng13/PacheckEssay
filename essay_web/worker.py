import json
import os
import time
import zipfile
import signal
import threading
from decimal import Decimal

from openai import OpenAI, APIStatusError, APIConnectionError

from .config import Settings
from .grading import InvalidGrade, get_usage, parse_grade, request_for
from .legacy import export
from .models import MODELS, PRICE_VERSION, price
from .service import batch_fingerprint, load_batch, read_json, tasks_for
from .store import Store

STOP = threading.Event()


class BudgetExceeded(Exception):
    pass


def reserve(store, job, task, model, effective, amount):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        midnight = int(time.time() // 86400) * 86400
        rows = db.execute('SELECT cost,reservation FROM attempts WHERE started>=?', (midnight,)).fetchall()
        spent = sum((Decimal(row['cost'] if row['cost'] is not None else row['reservation']) for row in rows), Decimal(0))
        if spent + amount > store.settings.daily_budget:
            raise BudgetExceeded()
        number = db.execute('SELECT count(*) FROM attempts WHERE job=? AND task=?', (job['id'], task)).fetchone()[0] + 1
        cursor = db.execute('''INSERT INTO attempts(job,task,number,started,model,settings,labels,status,reservation,pricing)
            VALUES (?,?,?,?,?,?,?,?,?,?)''', (job['id'], task, number, time.time(), model, json.dumps(effective), job['labels'],
            'sending', str(amount), json.dumps({'version': PRICE_VERSION, 'rates': MODELS[model]})))
        db.execute("UPDATE tasks SET state='sending' WHERE job=? AND id=?", (job['id'], task))
        return cursor.lastrowid


def finish_attempt(store, ident, status, response=None, endpoint=None, error=None, request_id=None):
    usage = get_usage(response, endpoint) if response is not None else None
    data = response.model_dump() if hasattr(response, 'model_dump') else (response or {})
    with store.connect() as db:
        model = db.execute('SELECT model FROM attempts WHERE id=?', (ident,)).fetchone()[0]
        cost = price(usage, model)
        # Definite rejected HTTP requests (e.g. 401,429) are distinguished from uncertain delivery.
        if status == 'rejected':
            cost = Decimal(0)
        db.execute('''UPDATE attempts SET status=?,ended=?,provider_request=?,provider_response=?,usage=?,cost=?,error=? WHERE id=?''',
                   (status, time.time(), request_id or getattr(response, '_request_id', None), data.get('id'),
                    json.dumps(usage) if usage is not None else None, str(cost) if cost is not None else None, error, ident))


def save_task(store, ident, task, state, result=None):
    with store.connect() as db:
        db.execute('UPDATE tasks SET state=?,result=? WHERE job=? AND id=?',
                   (state, json.dumps(result, ensure_ascii=False) if result is not None else None, ident, task))
        db.execute("UPDATE jobs SET done=(SELECT count(*) FROM tasks WHERE job=? AND state IN ('success','empty')) WHERE id=?", (ident, ident))


def export_results(store, ident, config, students, questions):
    directory = store.directory(ident)
    out = directory / 'results'
    (out / 'per_student').mkdir(parents=True, exist_ok=True)
    (out / 'per_question').mkdir(exist_ok=True)
    with store.connect() as db:
        rows = db.execute('SELECT id,result FROM tasks WHERE job=? AND result IS NOT NULL', (ident,)).fetchall()
    results = {}
    by_row = {str(s.row_index): s for s in students}
    for row in rows:
        student_row, qid = row['id'].split(':')
        student = by_row[student_row]
        result = json.loads(row['result'])
        results[(student.student_id, qid)] = result
        if config['per_question']:
            export.write_per_question_outputs(out, qid, student.safe_stem, result, True)
    if config['per_student']:
        export.write_per_student_reports(out, students, questions, results)
    export.export_gradebook_final(out, students, questions, results)
    export.export_lms_upload_csv(out, students, questions, results)
    export.export_summary_per_question(out, students, questions, results)
    # Formula injection protection for CSVs opened in spreadsheet software.
    import csv
    for path in out.rglob('*.csv'):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            table = list(csv.reader(stream))
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            csv.writer(stream).writerows([["'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value for value in row] for row in table])
    temporary = directory / 'download.tmp'
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in out.rglob('*'):
            if path.is_file():
                archive.write(path, path.relative_to(out))
    temporary.replace(directory / 'download.zip')


def run_job(store, ident, client):
    job = store.get_job(ident)
    directory = store.directory(ident)
    config = read_json(directory / 'config.json')
    if batch_fingerprint(directory, config) != job['fingerprint']:
        raise ValueError('fingerprint_changed')
    students, questions = load_batch(directory, config)
    model = config['model']
    endpoint = MODELS[model]['endpoint']
    stop = False
    message = ''
    for task, student, question, response in tasks_for(students, questions):
        store.heartbeat()
        if STOP.is_set() or store.get_job(ident)['cancel']:
            break
        with store.connect() as db:
            state = db.execute('SELECT state FROM tasks WHERE job=? AND id=?', (ident, task)).fetchone()[0]
        if state in ('success', 'empty'):
            with store.connect() as db:
                db.execute('INSERT INTO events(job,task,created,kind) VALUES (?,?,?,?)', (ident, task, time.time(), 'resume_skip'))
            continue
        if state != 'pending':
            continue
        if not response:
            result = {'score_total': 0, 'criteria': [], 'feedback': 'No response provided.',
                      'flags': {'off_topic': False, 'too_short': True, 'suspected_plagiarism': False, 'policy_issue': False}}
            save_task(store, ident, task, 'empty', enrich(result, student, question, response, model))
            with store.connect() as db:
                db.execute('INSERT INTO events(job,task,created,kind) VALUES (?,?,?,?)', (ident, task, time.time(), 'empty_response'))
            continue
        try:
            kwargs, effective, reservation = request_for(config, question, response, store.settings.max_output_tokens)
        except InvalidGrade:
            save_task(store, ident, task, 'failed')
            continue
        for retry in range(3):
            if STOP.is_set() or store.get_job(ident)['cancel']:
                break
            try:
                attempt = reserve(store, job, task, model, effective, reservation)
            except BudgetExceeded:
                message, stop = 'Daily cost limit reached, including reservations for uncertain calls.', True
                break
            try:
                result_response = client.responses.create(**kwargs) if endpoint == 'responses' else client.chat.completions.create(**kwargs)
            except APIStatusError as error:
                code = error.status_code
                uncertain = code >= 500
                finish_attempt(store, attempt, 'unknown' if uncertain else 'rejected', error=f'http_{code}', request_id=error.request_id)
                # 5xx may have executed: do not automatically repeat an uncertain paid call.
                save_task(store, ident, task, 'unknown' if uncertain else 'failed')
                if code in (401, 403, 404):
                    message, stop = 'API credentials or selected model are unavailable. Contact the administrator.', True
                if code == 429 and retry < 2:
                    time.sleep(2 ** retry)
                    continue
                break
            except APIConnectionError:
                finish_attempt(store, attempt, 'unknown', error='connection_or_timeout')
                save_task(store, ident, task, 'unknown')
                break
            except Exception:
                # Do not serialize exception text: it can include prompts or credentials.
                finish_attempt(store, attempt, 'unknown', error='unexpected_transport_error')
                save_task(store, ident, task, 'unknown')
                break
            # Commit provider usage before parsing or accepting a grade.
            finish_attempt(store, attempt, 'received', result_response, endpoint)
            try:
                grade = parse_grade(result_response, endpoint, question)
            except (InvalidGrade, KeyError, TypeError):
                with store.connect() as db:
                    db.execute("UPDATE attempts SET status='invalid_output',error='invalid_or_incomplete_grade' WHERE id=?", (attempt,))
                save_task(store, ident, task, 'failed')
                break
            save_task(store, ident, task, 'success', enrich(grade, student, question, response, model, effective))
            with store.connect() as db:
                db.execute("UPDATE attempts SET status='success' WHERE id=?", (attempt,))
            break
        if stop:
            break
    export_results(store, ident, config, students, questions)
    current = store.get_job(ident)
    with store.connect() as db:
        unknown = db.execute("SELECT count(*) FROM tasks WHERE job=? AND state='unknown'", (ident,)).fetchone()[0]
        state = 'cancelled' if current['cancel'] else ('completed' if current['done'] == current['total'] else 'partial')
        if unknown:
            message = f'{unknown} uncertain call(s). These may have been billed; review before retrying.'
        db.execute('UPDATE jobs SET state=?,ended=?,message=? WHERE id=?', (state, time.time(), message, ident))


def enrich(grade, student, question, response, model, effective=None):
    return {**grade, 'student_id': student.student_id_raw or student.student_id,
            'student_name': student.display_name, 'question_id': question.question_id,
            'question_title': question.question_title, 'max_points': question.max_points,
            'raw_response_text': response, 'model_metadata': {'model': model, 'effective_settings': effective or {}, 'timestamp': time.time()}}


def main():
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    store = Store(Settings())
    lock = (store.root / 'worker.lock').open('a+b')
    # The lock is released by the OS on a crash. Never run recovery over a live worker.
    if os.name == 'nt':
        import msvcrt
        lock.write(b'0'); lock.flush(); lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    key = store.settings.key_file.read_text(encoding='utf-8').strip()
    if not key:
        raise RuntimeError('API key file is empty')
    client = OpenAI(api_key=key, max_retries=0, timeout=120)
    store.recover()
    while not STOP.is_set():
        store.heartbeat()
        store.cleanup()
        ident = store.claim()
        if ident:
            try:
                run_job(store, ident, client)
            except Exception:
                store.recover()
                with store.connect() as db:
                    db.execute("UPDATE jobs SET state='failed', ended=?,message='Worker could not finish this job. Administrator review required.' WHERE id=?", (time.time(), ident))
        else:
            STOP.wait(2)


if __name__ == '__main__':
    main()

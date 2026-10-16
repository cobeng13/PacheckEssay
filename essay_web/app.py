import json
import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .legacy.csv_ingest import load_preview
from .legacy.rubric import DEFAULT_SYSTEM_INSTRUCTION, estimate_call_tokens
from .models import MODELS
from .service import load_batch, read_json, resume, submit, tasks_for, validate_config, write_json
from .store import Store, digest


def create_app(settings=None):
    settings = settings or Settings()
    store = Store(settings)
    @asynccontextmanager
    async def lifespan(app):
        async def retention_loop():
            while True:
                try:
                    await asyncio.to_thread(store.cleanup)
                except Exception:
                    logging.getLogger(__name__).warning('retention_cleanup_failed')
                await asyncio.sleep(60)
        task = asyncio.create_task(retention_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    app = FastAPI(title='Essay Checker', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    web = Path(__file__).parent
    templates = Jinja2Templates(directory=web / 'templates')
    app.mount('/static', StaticFiles(directory=web / 'static'), name='static')

    @app.middleware('http')
    async def boundary(request, call_next):
        # Only trust proxy headers from the configured reverse proxy (uvicorn setting).
        if request.method == 'POST':
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': 'Cross-site submission refused.'}, 403)
        if not store.rate_limit(request.client.host if request.client else 'unknown'):
            return JSONResponse({'detail': 'Too many requests; please wait a minute.'}, 429)
        # Enforce a streamed bound even without Content-Length (chunked uploads).
        received = 0
        original = request._receive
        async def limited_receive():
            nonlocal received
            message = await original()
            received += len(message.get('body', b''))
            if received > settings.max_upload + 256 * 1024:
                raise HTTPException(413, 'Request is too large.')
            return message
        request._receive = limited_receive
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def validation_error(request, error):
        return JSONResponse({'detail': str(error)}, 400)

    def session(request):
        value = request.cookies.get('essay_session', '')
        if len(value) != 43:
            raise HTTPException(403, 'Open the home page first.')
        return value

    def draft(request, ident):
        with store.connect() as db:
            row = db.execute('SELECT * FROM drafts WHERE id=? AND session=? AND submitted IS NULL', (ident, digest(session(request)))).fetchone()
        if not row or row['created'] < time.time() - settings.retention_seconds:
            raise HTTPException(404, 'Draft unavailable.')
        return store.directory(ident)

    def authorized(request, ident):
        try:
            job = store.get_job(ident)
        except ValueError:
            job = None
        token = request.headers.get('X-Job-Token', '')
        if not job or job['state'] == 'expired' or not secrets.compare_digest(job['token'], digest(token)):
            raise HTTPException(404, 'Job unavailable.')
        if job['ended'] is not None and job['ended'] < time.time() - settings.retention_seconds:
            raise HTTPException(404, 'Job expired.')
        return job

    @app.get('/', response_class=HTMLResponse)
    def index(request: Request):
        response = templates.TemplateResponse(request=request, name='index.html', context={
            'models': list(MODELS), 'default_model': settings.default_model,
            'instruction': DEFAULT_SYSTEM_INSTRUCTION, 'max_upload_mb': settings.max_upload // 1024 // 1024})
        if len(request.cookies.get('essay_session', '')) != 43:
            response.set_cookie('essay_session', secrets.token_urlsafe(32), httponly=True, secure=settings.secure_cookie, samesite='strict', max_age=86400)
        return response

    @app.get('/health')
    def health():
        with store.connect() as db:
            row = db.execute('SELECT heartbeat FROM worker WHERE id=1').fetchone()
        alive = bool(row and row[0] > time.time() - 180)
        return JSONResponse({'status': 'ready' if alive else 'worker_unavailable'}, 200 if alive else 503)

    @app.post('/api/drafts')
    async def upload(request: Request):
        cookie = session(request)
        with store.connect() as db:
            count = db.execute('SELECT count(*) FROM drafts WHERE submitted IS NULL AND created>?', (time.time() - 86400,)).fetchone()[0]
        if count >= settings.max_queue * 5:
            raise HTTPException(429, 'Too many drafts; try later.')
        form = await request.form(max_files=1, max_fields=1)
        upload = form.get('file')
        if not upload or not hasattr(upload, 'filename'):
            raise ValueError('Select an input file.')
        suffix = Path(upload.filename or '').suffix.lower()
        if suffix not in ('.csv', '.json', '.html', '.htm'):
            raise ValueError('Use CSV, JSON, or HTML files.')
        content = await upload.read(settings.max_upload + 1)
        await upload.close()
        if not content or len(content) > settings.max_upload:
            raise HTTPException(413, 'File is empty or too large.')
        ident = secrets.token_hex(16)
        directory = store.directory(ident)
        directory.mkdir()
        path = directory / f'input{suffix}'
        try:
            path.write_bytes(content)
            preview = load_preview(str(path))
            if len(preview.headers) > 300 or len(set(preview.headers)) != len(preview.headers):
                raise ValueError('Use at most 300 unique column headers.')
            with store.connect() as db:
                db.execute('INSERT INTO drafts VALUES (?,?,?,NULL)', (ident, digest(cookie), time.time()))
        except Exception:
            path.unlink(missing_ok=True)
            directory.rmdir()
            raise ValueError('Could not read input. Check headers and tabular file format.') from None
        return {'id': ident, 'headers': preview.headers, 'rows': preview.rows,
                'mapping': {k: getattr(preview, k) for k in ('first_col','last_col','name_col','id_col')},
                'response_columns': preview.response_cols}

    @app.post('/api/drafts/{ident}/estimate')
    async def estimate(ident: str, request: Request):
        directory = draft(request, ident)
        preview = load_preview(str(next(directory.glob('input.*'))))
        config = validate_config(await request.json(), settings, preview.headers)
        students, questions = load_batch(directory, config)
        if not 0 < len(students) * len(questions) <= settings.max_tasks:
            raise ValueError('Batch exceeds task limit or contains no rows.')
        calls = inp = out = 0
        for _, _, question, response in tasks_for(students, questions):
            if response:
                i, o = estimate_call_tokens(system_instruction=config['system_instruction'], rubric=question.rubric,
                    model_answer=question.model_answer, extra_instructions=question.extra_instructions,
                    student_response=response, question_id=question.question_id,
                    question_title=question.question_title, max_points=question.max_points)
                inp += i; out += o; calls += 1
        rates = MODELS[config['model']]
        cost = (Decimal(inp) * Decimal(rates['input']) + Decimal(out) * Decimal(rates['output'])) / Decimal(1000000)
        return {'students': len(students), 'questions': len(questions), 'calls': calls, 'estimated_usd': str(cost),
                'notice': 'Approximate visible-text estimate. Reasoning, retries and caching can change actual cost.'}

    @app.post('/api/drafts/{ident}/submit')
    async def start(ident: str, request: Request):
        draft(request, ident)
        token = submit(store, ident, session(request), await request.json())
        return {'id': ident, 'token': token}

    @app.get('/api/jobs/{ident}')
    def status(ident: str, request: Request):
        job = authorized(request, ident)
        with store.connect() as db:
            unknown = db.execute("SELECT count(*) FROM tasks WHERE job=? AND state='unknown'", (ident,)).fetchone()[0]
            failures = db.execute("SELECT count(*) FROM tasks WHERE job=? AND state='failed'", (ident,)).fetchone()[0]
        return {k: job[k] for k in ('state','total','done','message')} | {
            'unknown': unknown, 'failed': failures, 'download': (store.directory(ident) / 'download.zip').exists(),
            'expires': job['ended'] + 86400 if job['ended'] else None}

    @app.post('/api/jobs/{ident}/cancel')
    def cancel(ident: str, request: Request):
        authorized(request, ident)
        with store.connect() as db:
            db.execute("UPDATE jobs SET cancel=1 WHERE id=? AND state IN ('running','queued')", (ident,))
            db.execute("UPDATE jobs SET state='cancelled',ended=? WHERE id=? AND state='queued'", (time.time(), ident))
        return {'ok': True}

    @app.post('/api/jobs/{ident}/resume')
    async def retry(ident: str, request: Request):
        authorized(request, ident)
        body = await request.json()
        resume(store, ident, retry_unknown=body.get('retry_unknown') is True)
        return {'ok': True}

    @app.get('/api/jobs/{ident}/download')
    def download(ident: str, request: Request):
        job = authorized(request, ident)
        path = store.directory(ident) / 'download.zip'
        if job['state'] in ('running', 'queued') or not path.exists():
            raise HTTPException(409, 'Download is not ready.')
        return FileResponse(path, media_type='application/zip', filename='essay-grading-results.zip')

    return app


# Import with ESSAY_DATA set in development; factory avoids side effects in tests.

import csv
import io
import json
import time
import zipfile
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError

from essay_web.admin import export_usage
from essay_web.app import create_app
from essay_web.config import Settings
from essay_web.grading import get_usage, parse_grade, InvalidGrade
from essay_web.models import price
from essay_web.service import read_json, resume
from essay_web.worker import run_job


@pytest.fixture
def env(tmp_path):
    settings=Settings(data=tmp_path/'data',secure_cookie=False,daily_budget=Decimal('100'),requests_per_minute=1000)
    app=create_app(settings)
    with TestClient(app) as client:
        client.get('/')
        yield client,app.state.store


def setup_job(client, model='gpt-6-luna', rows='Student ID,Name,Response 1\n1,Student A,A useful answer\n1,Student B,Another answer\n2,Student C,\n'):
    upload=client.post('/api/drafts',files={'file':('answers.csv',rows.encode(),'text/csv')})
    assert upload.status_code==200,upload.text
    draft=upload.json()
    config={'labels':{'faculty':'Faculty Test','course':'Course'},'mapping':draft['mapping'],
            'questions':[{'columns':['Response 1'],'max_points':5,'rubric':'Accuracy (5 points possible)'}],
            'model':model,'per_student':True,'per_question':True}
    estimate=client.post(f"/api/drafts/{draft['id']}/estimate",json=config)
    assert estimate.status_code==200,estimate.text
    result=client.post(f"/api/drafts/{draft['id']}/submit",json=config)
    assert result.status_code==200,result.text
    return result.json(),config


def headers(job):
    return {'X-Job-Token':job['token']}


def grade_payload():
    return {'score_total':3,'criteria':[{'name':'Accuracy','points_awarded':3,'justification':'Evidence supports three points.'}],
            'feedback':'Explain the remaining details.','flags':{'off_topic':False,'too_short':False,'suspected_plagiarism':False,'policy_issue':False}}


def api_response(endpoint='responses', payload=None, usage=True, status='completed'):
    text=json.dumps(payload if payload is not None else grade_payload())
    if endpoint=='responses':
        data={'id':'resp_test','status':status,'output':[{'type':'message','content':[{'type':'output_text','text':text}]}]}
        if usage:data['usage']={'input_tokens':100,'output_tokens':50,'input_tokens_details':{'cached_tokens':20},'output_tokens_details':{'reasoning_tokens':10}}
    else:
        data={'id':'chat_test','choices':[{'finish_reason':'stop','message':{'content':text}}]}
        if usage:data['usage']={'prompt_tokens':100,'completion_tokens':50,'prompt_tokens_details':{'cached_tokens':20}}
    response=Mock();response.model_dump.return_value=data;response._request_id='req_test'
    return response


def mock_client(endpoint='responses', **kwargs):
    client=Mock()
    response=api_response(endpoint,**kwargs)
    client.responses.create.return_value=response
    client.chat.completions.create.return_value=response
    return client


def run(store,job,client):
    assert store.claim()==job['id']
    run_job(store,job['id'],client)


def test_end_to_end_and_duplicate_ids(env,tmp_path):
    client,store=env; job,_=setup_job(client)
    api=mock_client();run(store,job,api)
    assert api.responses.create.call_count==2
    request=api.responses.create.call_args.kwargs
    assert request['store'] is False and request['reasoning']=={'effort':'medium'}
    assert 'temperature' not in request
    status=client.get(f"/api/jobs/{job['id']}",headers=headers(job)).json()
    assert status['state']=='completed' and status['done']==3
    result=client.get(f"/api/jobs/{job['id']}/download",headers=headers(job))
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        names=archive.namelist()
        assert 'gradebook_final.csv' in names and 'ForLMSUpload/Q1.csv' in names
        assert len([n for n in names if n.startswith('per_student/') and n.endswith('.json')])==3
        table=list(csv.DictReader(io.StringIO(archive.read('gradebook_final.csv').decode('utf-8-sig'))))
        assert [r['Q1 Score'] for r in table]==['3.0','3.0','0.0']
    with store.connect() as db:
        attempts=db.execute('SELECT * FROM attempts').fetchall()
        assert len(attempts)==2
        assert all(Decimal(r['cost'])==Decimal('0.0000332') for r in attempts)
        assert db.execute("SELECT count(*) FROM events WHERE kind='empty_response'").fetchone()[0]==1
    export_usage(store,tmp_path/'reports')
    assert 'Faculty Test' in (tmp_path/'reports/summary.csv').read_text(encoding='utf-8-sig')
    assert 'Student A' not in (tmp_path/'reports/requests.csv').read_text(encoding='utf-8-sig')


@pytest.mark.parametrize('model,endpoint',[('gpt-4.1-mini','chat'),('gpt-5.6-luna','responses'),('gpt-5.6-terra','responses')])
def test_models(env,model,endpoint):
    client,store=env;job,_=setup_job(client,model)
    api=mock_client(endpoint);run(store,job,api)
    assert store.get_job(job['id'])['state']=='completed'
    if endpoint=='chat':
        assert api.chat.completions.create.call_args.kwargs['temperature']==0


def test_session_and_tokens(env):
    client,store=env
    draft=client.post('/api/drafts',files={'file':('a.csv',b'Name,Response 1\nA,B')}).json()
    with TestClient(create_app(store.settings)) as other:
        other.get('/')
        assert other.post(f"/api/drafts/{draft['id']}/submit",json={}).status_code==404
    job,_=setup_job(client)
    assert client.get(f"/api/jobs/{job['id']}").status_code==404
    assert client.post(f"/api/jobs/{job['id']}/cancel",headers={'X-Job-Token':'wrong'}).status_code==404
    assert client.get(f"/api/jobs/{job['id']}/download",headers=headers(job)).status_code==409
    assert client.post('/api/drafts',headers={'Origin':'https://evil.example'},files={'file':('a.csv',b'a,b')}).status_code==403
    assert client.get('/static/../../ledger.sqlite3').status_code==404
    assert 'frame-ancestors' in client.get('/').headers['content-security-policy']


def test_no_double_submit(env):
    client,store=env;job,config=setup_job(client)
    assert client.post(f"/api/drafts/{job['id']}/submit",json=config).status_code==404
    assert store.claim()==job['id']
    assert store.claim() is None


@pytest.mark.parametrize('fault',['timeout','500','invalid','incomplete','no_usage'])
def test_failures_keep_cost_and_unknown(env,fault):
    client,store=env;job,_=setup_job(client,rows='Name,Response 1\nA,Answer')
    api=mock_client()
    if fault=='timeout':api.responses.create.side_effect=APIConnectionError(request=httpx.Request('POST','https://api.openai.com'))
    if fault=='500':api.responses.create.side_effect=APIStatusError('secret text must not leak',response=httpx.Response(500,request=httpx.Request('POST','https://api.openai.com')),body=None)
    if fault=='invalid':api.responses.create.return_value=api_response(payload={**grade_payload(),'score_total':4})
    if fault=='incomplete':api.responses.create.return_value=api_response(status='incomplete')
    if fault=='no_usage':api.responses.create.return_value=api_response(usage=False)
    run(store,job,api)
    assert api.responses.create.call_count==1
    with store.connect() as db:
        row=db.execute('SELECT * FROM attempts').fetchone()
        if fault in ('timeout','500','no_usage'):assert row['cost'] is None
        else:assert row['cost'] is not None
        assert 'secret text' not in (row['error'] or '')
    if fault in ('timeout','500'):
        assert client.post(f"/api/jobs/{job['id']}/resume",headers=headers(job),json={}).status_code==400
        assert client.post(f"/api/jobs/{job['id']}/resume",headers=headers(job),json={'retry_unknown':True}).status_code==200


@pytest.mark.parametrize('code',[400,401,403,404,429])
def test_http_rejections(env,code,monkeypatch):
    monkeypatch.setattr('essay_web.worker.time.sleep',lambda _:None)
    client,store=env;job,_=setup_job(client,rows='Name,Response 1\nA,Answer')
    api=mock_client();api.responses.create.side_effect=APIStatusError('private',response=httpx.Response(code,request=httpx.Request('POST','https://api.openai.com')),body=None)
    run(store,job,api)
    assert api.responses.create.call_count==(3 if code==429 else 1)
    with store.connect() as db:
        assert all(r[0]=='0' for r in db.execute('SELECT cost FROM attempts'))


def test_cancel_resume_fingerprint_and_skips(env):
    client,store=env;job,_=setup_job(client)
    api=mock_client()
    def first(**kwargs):
        with store.connect() as db:db.execute('UPDATE jobs SET cancel=1 WHERE id=?',(job['id'],))
        return api_response()
    api.responses.create.side_effect=first
    run(store,job,api)
    assert store.get_job(job['id'])['state']=='cancelled'
    assert client.post(f"/api/jobs/{job['id']}/resume",headers=headers(job),json={}).status_code==200
    api=mock_client();run(store,job,api)
    assert api.responses.create.call_count==1
    assert store.get_job(job['id'])['state']=='completed'
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='resume_skip'").fetchone()[0]==1


def test_restart_marks_uncertain(env):
    client,store=env;job,_=setup_job(client)
    assert store.claim()==job['id']
    with store.connect() as db:
        db.execute("UPDATE tasks SET state='sending' WHERE job=? AND id='1:Q1'",(job['id'],))
    store.recover()
    assert store.get_job(job['id'])['state']=='interrupted'
    with pytest.raises(ValueError,match='Uncertain'):resume(store,job['id'])
    path=store.directory(job['id'])/'config.json'
    config=read_json(path);config['model']='gpt-5.6-luna';path.write_text(json.dumps(config),encoding='utf-8')
    with pytest.raises(ValueError,match='changed'):resume(store,job['id'],True)


def test_retention_keeps_only_accounting(env,tmp_path):
    client,store=env;job,_=setup_job(client)
    run(store,job,mock_client())
    draft=client.post('/api/drafts',files={'file':('d.csv',b'Name,Response 1\nX,Y')}).json()
    with store.connect() as db:
        db.execute('UPDATE jobs SET ended=? WHERE id=?',(time.time()-86401,job['id']))
        db.execute('UPDATE drafts SET created=? WHERE id=?',(time.time()-86401,draft['id']))
    assert client.get(f"/api/jobs/{job['id']}",headers=headers(job)).status_code==404
    store.cleanup();store.cleanup()
    assert not store.directory(job['id']).exists() and not store.directory(draft['id']).exists()
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0]==2
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    export_usage(store,tmp_path/'backup')
    assert 'Student A' not in ''.join(p.read_text(encoding='utf-8-sig') for p in (tmp_path/'backup').iterdir())


def test_budget_prevents_call(env):
    client,store=env;job,_=setup_job(client);store.settings.daily_budget=Decimal('0.00000001')
    api=mock_client();run(store,job,api)
    api.responses.create.assert_not_called()
    assert store.get_job(job['id'])['state']=='partial'


def test_price_does_not_double_charge_reasoning():
    usage={'input':100,'output':50,'cached':20,'cache_write':10,'reasoning':40}
    assert price(usage,'gpt-6-luna')==Decimal('0.00003345')
    assert price(None,'gpt-6-luna') is None
    assert price({**usage,'cached':200},'gpt-6-luna') is None
    assert price(usage,'gpt-5.6-luna') is None


@pytest.mark.parametrize('filename,content',[
    ('a.json',json.dumps([{'Name':'A','Response 1':'Answer'}]).encode()),
    ('a.html',b'<table><tr><th>Name</th><th>Response 1</th></tr><tr><td>A</td><td>Answer</td></tr></table>'),
    ('a.csv',b'Name,Response 1\nA,Answer')])
def test_formats(env,filename,content):
    client,_=env
    result=client.post('/api/drafts',files={'file':(filename,content)})
    assert result.status_code==200
    assert result.json()['response_columns']==['Response 1']


def test_limits_health_and_secret_absence(env):
    client,store=env
    assert client.get('/health').status_code==503
    store.heartbeat();assert client.get('/health').json()=={'status':'ready'}
    assert 'api_key' not in client.get('/').text
    store.settings.max_upload=10
    assert client.post('/api/drafts',files={'file':('a.csv',b'x'*11)}).status_code==413
    store.settings.requests_per_minute=1
    assert client.get('/').status_code==429


def test_web_does_not_read_secret(env):
    client,store=env
    store.settings.key_file=store.root/'must-not-exist'
    assert client.get('/').status_code==200
    setup_job(client)


def test_attempt_is_durable_before_network(env):
    client,store=env;job,_=setup_job(client,rows='Name,Response 1\nA,Answer')
    api=mock_client()
    def inspect(**kwargs):
        with store.connect() as db:
            attempt=db.execute('SELECT * FROM attempts').fetchone()
            assert attempt['status']=='sending' and attempt['cost'] is None
            assert Decimal(attempt['reservation'])>0
            assert 'Answer' not in attempt['settings']
        return api_response()
    api.responses.create.side_effect=inspect
    run(store,job,api)


def test_actual_sdk_models_are_accepted(env):
    from openai.types.responses import Response
    client,store=env;job,_=setup_job(client,rows='Name,Response 1\nA,Answer')
    data=api_response().model_dump()
    response=Response.model_construct(**data)
    api=mock_client();api.responses.create.return_value=response
    run(store,job,api)
    assert store.get_job(job['id'])['state']=='completed'


def test_refusal_keeps_usage(env):
    client,store=env;job,_=setup_job(client,rows='Name,Response 1\nA,Answer')
    response=api_response();data=response.model_dump()
    data['output'][0]['content']=[{'type':'refusal','refusal':'Cannot grade'}]
    response.model_dump.return_value=data
    api=mock_client();api.responses.create.return_value=response
    run(store,job,api)
    with store.connect() as db:
        row=db.execute('SELECT * FROM attempts').fetchone()
        assert row['status']=='invalid_output' and row['cost'] is not None


def test_merge_columns_and_blank_without_paid_call(env):
    client,store=env
    draft=client.post('/api/drafts',files={'file':('a.csv',b'Name,Response A,Response B\nA,First,Second\nB,,')}).json()
    config={'labels':{'faculty':'Faculty'},'mapping':draft['mapping'],'model':'gpt-6-luna',
            'questions':[{'columns':['Response A','Response B'],'rubric':'Accuracy (5 points possible)','max_points':5}]}
    job=client.post(f"/api/drafts/{draft['id']}/submit",json=config).json()
    api=mock_client();run(store,job,api)
    assert api.responses.create.call_count==1
    sent=api.responses.create.call_args.kwargs['input'][1]['content']
    assert 'First\n\n-----\n\nSecond' in sent


def test_queue_limit_and_bad_config(env):
    client,store=env;job,_=setup_job(client)
    store.settings.max_queue=1
    draft=client.post('/api/drafts',files={'file':('b.csv',b'Name,Response 1\nA,B')}).json()
    assert client.post(f"/api/drafts/{draft['id']}/estimate",json=[]).status_code==400
    config={'labels':{'faculty':'F'},'mapping':draft['mapping'],'questions':[{'columns':['Response 1'],'rubric':'Accuracy (5 points possible)'}]}
    assert client.post(f"/api/drafts/{draft['id']}/submit",json=config).status_code==400


def test_orphan_cleanup(env):
    import os
    client,store=env
    ident='a'*32;directory=store.directory(ident);directory.mkdir();(directory/'input.csv').write_text('Synthetic')
    os.utime(directory,(time.time()-90000,time.time()-90000))
    store.cleanup()
    assert not directory.exists()


def test_worker_recovery_preserves_reservation_and_pricing(env):
    from essay_web.worker import reserve
    from essay_web.grading import request_for
    from essay_web.service import load_batch
    client,store=env;job,_=setup_job(client)
    directory=store.directory(job['id']);config=read_json(directory/'config.json')
    students,questions=load_batch(directory,config)
    _,effective,amount=request_for(config,questions[0],'Answer',8192)
    assert store.claim()==job['id']
    reserve(store,store.get_job(job['id']),'1:Q1',config['model'],effective,amount)
    store.recover()
    with store.connect() as db:
        row=db.execute('SELECT * FROM attempts').fetchone()
        assert row['status']=='unknown' and row['cost'] is None
        assert Decimal(row['reservation'])==amount and 'version' in row['pricing']


def test_csv_text_formula_is_escaped(env):
    client,store=env;job,_=setup_job(client,rows='Name,Response 1\n=FORMULA,Answer')
    api=mock_client();run(store,job,api)
    text=(store.directory(job['id'])/'results/gradebook_final.csv').read_text(encoding='utf-8-sig')
    assert "'=FORMULA" in text


def test_offline_model_comparison(tmp_path):
    from essay_web.compare import compare
    scores=tmp_path/'scores.csv';requests=tmp_path/'requests.csv'
    with scores.open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['sample_id','max_points','instructor_score','gpt-4.1-mini','gpt-6-luna'])
        writer.writerows([(str(i),5,3,3,3) for i in range(30)])
    with requests.open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['model','status','cost_usd','duration_seconds'])
        writer.writerows([['gpt-4.1-mini','success','0.02','2'],['gpt-6-luna','success','0.01','1']])
    result=compare(scores,requests)
    assert result['numeric_gate_pass'] and result['human_feedback_review_required']

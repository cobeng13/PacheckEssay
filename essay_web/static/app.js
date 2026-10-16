const $ = id => document.getElementById(id);
let draft, questions = [], job, timer;
function error(e) { $('error').textContent = e.message || String(e); $('error').hidden = false; }
async function api(path, options = {}) {
  $('error').hidden = true;
  const response = await fetch(path, options);
  if (!response.ok) { let message; try { message = (await response.json()).detail; } catch { message = 'Server could not complete this request.'; } throw Error(typeof message === 'string' ? message : 'Check the entered values.'); }
  return response;
}
function jsonOptions(body, token) { return {method:'POST',headers:{'Content-Type':'application/json',...(token ? {'X-Job-Token':token} : {})},body:JSON.stringify(body)}; }
function select(options, value, multiple = false) {
  const element = document.createElement('select'); element.multiple = multiple;
  for (const name of options) { const option = new Option(name || '(none)', name); option.selected = multiple ? value.includes(name) : value === name; element.add(option); }
  return element;
}
function labelled(parent, text, element) { const label = document.createElement('label'); label.append(document.createTextNode(text),element); parent.append(label); return element; }
function mapping() {
  $('question-mapping').replaceChildren();
  questions.forEach((q,i) => {
    const div = document.createElement('div'); div.className='question';
    const name = document.createElement('input'); name.value=q.question_title; name.placeholder='Optional question title'; name.oninput=()=>{q.question_title=name.value;invalidate();};
    labelled(div,`Q${i+1} title`,name);
    const cols=select(draft.headers,q.columns,true); cols.onchange=()=>{q.columns=Array.from(cols.selectedOptions,o=>o.value); invalidate();};
    labelled(div,'Response columns',cols);
    const remove=document.createElement('button'); remove.textContent='Remove question'; remove.className='secondary'; remove.onclick=()=>{questions.splice(i,1);mapping();invalidate();};div.append(remove);
    $('question-mapping').append(div);
  });
}
function rubrics() {
  $('question-rubrics').replaceChildren();
  questions.forEach((q,i)=>{
    const div=document.createElement('div');div.className='question';const title=document.createElement('h3');title.textContent=`Q${i+1} · ${q.question_title || q.columns.join(' + ')}`;div.append(title);
    const points=document.createElement('input');points.type='number';points.min='0.1';points.step='any';points.value=q.max_points;points.oninput=()=>{q.max_points=Number(points.value);invalidate();};labelled(div,'Maximum points',points);
    for(const [key,label] of [['rubric','Rubric'],['model_answer','Model answer (optional)'],['extra_instructions','Extra instructions (optional)']]) {
      const area=document.createElement('textarea');area.value=q[key]||'';area.oninput=()=>{q[key]=area.value;invalidate();};labelled(div,label,area);
      const file=document.createElement('input');file.type='file';file.accept='.txt';file.onchange=async()=>{if(file.files[0]){if(file.files[0].size>200000)return error(Error('Text file is too large.')); area.value=await file.files[0].text();q[key]=area.value;invalidate();}};labelled(div,`Load ${label.toLowerCase()} from text`,file);
    }
    $('question-rubrics').append(div);
  });
}
function invalidate(){ $('submit').disabled=true; $('estimate-output').textContent=''; }
function config() {
  return {labels:Object.fromEntries(['faculty','department','course','assessment'].map(k=>[k,$(k).value])),
    mapping:Object.fromEntries(['first_col','last_col','name_col','id_col'].map(k=>[k,$(k).value])),questions,
    model:$('model').value,system_instruction:$('instruction').value,per_student:$('per-student').checked,per_question:$('per-question').checked};
}
for (const id of ['faculty','department','course','assessment','model','instruction','per-student','per-question']) $(id).addEventListener('input',invalidate);
$('load').onclick=async()=>{try{
  const file=$('input').files[0];if(!file)throw Error('Select a responses file.');if(!$('faculty').value.trim())throw Error('Enter your faculty name.');
  const form=new FormData();form.append('file',file);draft=await (await api('/api/drafts',{method:'POST',body:form})).json();
  $('identity').replaceChildren();for(const [key,label] of [['first_col','First name'],['last_col','Last name'],['name_col','Full name'],['id_col','Student ID']]){const el=select(['',...draft.headers],draft.mapping[key]||'');el.id=key;el.onchange=invalidate;labelled($('identity'),label,el);}
  const table=document.createElement('table');const head=table.createTHead().insertRow();for(const h of draft.headers){const th=document.createElement('th');th.textContent=h;head.append(th);}const body=table.createTBody();for(const row of draft.rows){const tr=body.insertRow();for(const h of draft.headers)tr.insertCell().textContent=row[h]||'';}$('preview').replaceChildren(table);
  questions=draft.response_columns.map(c=>({columns:[c],question_title:'',max_points:10,rubric:'',model_answer:'',extra_instructions:''}));mapping();$('mapping').hidden=false;invalidate();
}catch(e){error(e);}};
$('add-question').onclick=()=>{questions.push({columns:[],question_title:'',max_points:10,rubric:'',model_answer:'',extra_instructions:''});mapping();invalidate();};
$('to-rubrics').onclick=()=>{if(!questions.length || questions.some(q=>!q.columns.length))return error(Error('Map response columns for every question.'));rubrics();$('rubrics').hidden=false;$('rubrics').scrollIntoView({behavior:'smooth'});};
$('to-run').onclick=()=>{if(questions.some(q=>!q.rubric.trim()))return error(Error('Add a rubric for every question.'));$('run').hidden=false;$('run').scrollIntoView({behavior:'smooth'});};
function saveFile(blob,name){const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}
$('save-preset').onclick=()=>saveFile(new Blob([JSON.stringify(questions,null,2)],{type:'application/json'}),'rubric-preset.json');
$('preset').onchange=async()=>{try{const file=$('preset').files[0];if(file.size>2000000)throw Error('Preset is too large.');const preset=JSON.parse(await file.text());if(!Array.isArray(preset)||preset.length!==questions.length)throw Error('Preset must match the question count.');questions.forEach((q,i)=>{for(const key of ['rubric','model_answer','extra_instructions'])q[key]=String(preset[i][key]||'');q.max_points=Number(preset[i].max_points)||10;});rubrics();invalidate();}catch(e){error(e);}};
$('estimate').onclick=async()=>{try{const snapshot=JSON.stringify(config());const result=await(await api(`/api/drafts/${draft.id}/estimate`,jsonOptions(JSON.parse(snapshot)))).json();if(snapshot!==JSON.stringify(config()))return invalidate();$('estimate-output').textContent=`${result.students} students · ${result.questions} questions · ${result.calls} API calls · approximately $${Number(result.estimated_usd).toFixed(4)} USD. ${result.notice}`;$('submit').disabled=false;}catch(e){error(e);}};
$('submit').onclick=async()=>{ $('submit').disabled=true;try{job=await(await api(`/api/drafts/${draft.id}/submit`,jsonOptions(config()))).json();location.hash=`job=${job.id}&token=${job.token}`;showJob();}catch(e){error(e);}};
function showJob(){for(const id of ['upload','mapping','rubrics','run'])$(id).hidden=true;$('job').hidden=false;$('private-link').href=location.href;poll();}
async function poll(){clearTimeout(timer);try{const status=await(await api(`/api/jobs/${job.id}`,{headers:{'X-Job-Token':job.token}})).json();$('job-status').textContent=`${status.state} · ${status.done} of ${status.total} graded · ${status.failed} failed · ${status.unknown} uncertain`;$('progress').max=status.total;$('progress').value=status.done;$('job-message').textContent=status.message;const active=['queued','running'].includes(status.state);$('cancel').hidden=!active;$('resume').hidden=!['partial','failed','cancelled','interrupted'].includes(status.state);$('download').hidden=!status.download||active;$('uncertain-label').hidden=!status.unknown;$('expiry').textContent=status.expires?`Downloads expire ${new Date(status.expires*1000).toLocaleString()}.`:'';if(active)timer=setTimeout(poll,3000);}catch(e){error(e);}}
$('cancel').onclick=async()=>{try{await api(`/api/jobs/${job.id}/cancel`,jsonOptions({},job.token));poll();}catch(e){error(e);}};
$('resume').onclick=async()=>{try{await api(`/api/jobs/${job.id}/resume`,jsonOptions({retry_unknown:$('retry-unknown').checked},job.token));$('retry-unknown').checked=false;poll();}catch(e){error(e);}};
$('download').onclick=async()=>{try{saveFile(await(await api(`/api/jobs/${job.id}/download`,{headers:{'X-Job-Token':job.token}})).blob(),'essay-grading-results.zip');}catch(e){error(e);}};
const fragment=new URLSearchParams(location.hash.slice(1));if(fragment.get('job')&&fragment.get('token')){job={id:fragment.get('job'),token:fragment.get('token')};showJob();}

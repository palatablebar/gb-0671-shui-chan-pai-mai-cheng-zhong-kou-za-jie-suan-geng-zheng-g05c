const state={paused:false,revision:1};
const feedback=document.querySelector('#feedback');
async function request(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.detail||'运行失败，请重试');return data;}
document.querySelector('#run').addEventListener('click',async(event)=>{event.target.disabled=true;feedback.textContent='运行中…';try{const data=await request(document.querySelector('#dataset').value===document.body.dataset.source?'/api/runs/'+document.body.dataset.runId+'/rerun':'/api/runs',{dataset:document.querySelector('#dataset').value});feedback.textContent=`批次 ${data.manifest.run_id} · ${data.balance.amount} CNY`;const url=new URL(location.href);url.searchParams.set('dataset',document.querySelector('#dataset').value);url.searchParams.set('run_id',data.manifest.run_id);location.href=url;}catch(error){feedback.textContent=error.message;}finally{event.target.disabled=false;}});
document.querySelector('#pause').addEventListener('click',event=>{state.paused=!state.paused;event.target.textContent=state.paused?'继续日志':'暂停日志';document.querySelector('#log-state').textContent=state.paused?'已暂停':'滚动中';});
document.querySelector('#filter').addEventListener('input',event=>{const text=event.target.value.trim();document.querySelectorAll('.primary-panel tbody tr').forEach(row=>row.hidden=!row.textContent.includes(text));});
const review=document.querySelector('#review');if(review)review.addEventListener('click',async()=>{try{const run=await fetch('/api/runs/'+review.dataset.id).then(r=>r.json());const result=await request('/api/runs/'+review.dataset.id+'/review',{revision:run.revision});feedback.textContent=result.status;}catch(error){feedback.textContent=error.message;}});
const selected=new URL(location.href).searchParams.get('dataset');if(selected)document.querySelector('#dataset').value=selected;

const filePicker=document.querySelector('#csv-file');
if(filePicker){
 const editor=document.querySelector('#csv-text');
 filePicker.addEventListener('change',async()=>{const file=filePicker.files[0];if(!file)return;if(file.size>2000000){editor.value='';alert('文件超过2MB，请缩小后导入');return;}const bytes=await file.arrayBuffer();try{editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch(e){editor.value=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}});
 document.querySelector('#import-csv').addEventListener('click',async event=>{event.target.disabled=true;try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:editor.value})});const data=await response.json();if(!response.ok)throw Error(data.detail);location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(e){editor.setCustomValidity(e.message);editor.reportValidity();}finally{event.target.disabled=false;}});
 editor.addEventListener('input',()=>editor.setCustomValidity(''));
}

// ---- M8 跨日承接 ----
const carryRows=document.querySelector('#carry-rows');
if(carryRows){
 const $=id=>document.querySelector(id);
 const log=(text,warn=true)=>{const tail=$('#log-tail');const now=new Date().toTimeString().slice(0,8);tail.insertAdjacentHTML('afterbegin',`<div><time>${now}</time><span class="lamp ${warn?'warn':''}"></span> ${text}</div>`);};
 const moneyOrDash=v=>v===null||v===undefined?'—':v;
 let lastDraft=null;
 async function postJSON(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.detail||'请求失败');return data;}
 function renderDraft(draft){
  lastDraft=draft;
  $('#carry-status').textContent=draft.status;
  $('#base-total').textContent=draft.base_total;
  $('#current-total').textContent=draft.current_total;
  $('#base-seal').textContent='封账封存码 '+draft.base_seal.slice(0,48)+'…';
  $('#delta-amount').textContent=draft.delta_amount;
  $('#delta-paid').textContent=draft.delta_paid;
  $('#delta-due').textContent=draft.delta_due;
  $('#carry-counts').textContent=`${draft.carried_count} / ${draft.blocked_count} / ${draft.new_tickets.length}`;
  $('#closed-state').textContent=draft.closed?'差额闭合':'差额未闭合';
  carryRows.innerHTML=draft.rows.map(r=>`<tr${r.status==='待处理'?' style="color:var(--warn)"':''}><td>${r.ticket}</td><td>${moneyOrDash(r.base_version)}</td><td>${r.current_version}</td><td>${moneyOrDash(r.base_amount)}</td><td>${r.current_amount}</td><td>${moneyOrDash(r.delta_amount)}</td><td>${moneyOrDash(r.delta_paid)}</td><td>${moneyOrDash(r.delta_due)}</td><td>${r.status}</td><td>${r.code}</td><td title="${r.detail}">${r.detail}</td></tr>`).join('');
  log(`核对完成：${draft.carried_count} 票可承接，${draft.blocked_count} 票待处理，状态 ${draft.status}`,!draft.closed);
 }
 async function loadIndex(){
  try{
   const data=await fetch('/api/carryover').then(r=>r.json());
   $('#seal-list').innerHTML=(data.seals||[]).map(s=>`<option value="${s.run_id}">`).join('');
   $('#record-rows').innerHTML=(data.records||[]).map(r=>`<tr><td>${r.base_run_id}</td><td>${r.current_run_id}</td><td>${r.status}</td><td>${r.published?'是':'否'}</td></tr>`).join('')||'<tr><td>暂无承接记录。</td></tr>';
   if(!$('#base-run').value && data.seals && data.seals[0]) $('#base-run').value=data.seals[0].run_id;
  }catch(e){/* 封账尚未建立时忽略 */}
 }
 async function doPreview(){
  try{
   const data=await postJSON('/api/carryover/preview',{base_run_id:$('#base-run').value.trim(),current_run_id:$('#current-run').value.trim()});
   renderDraft(data.draft);
  }catch(e){feedback.textContent=e.message;log('核对失败：'+e.message);}
 }
 $('#carry-preview').addEventListener('click',doPreview);
 $('#carry-save').addEventListener('click',async()=>{
  try{
   const data=await postJSON('/api/carryover',{base_run_id:$('#base-run').value.trim(),current_run_id:$('#current-run').value.trim()});
   renderDraft(data.record.draft);
   log(data.changed?'草稿已保留为待处理/可承接，未新增重复记录。':'已发布记录不可覆盖，沿用旧版承接结果。');
   loadIndex();
  }catch(e){feedback.textContent=e.message;log('保留失败：'+e.message);}
 });
 $('#carry-publish').addEventListener('click',async()=>{
  try{
   const data=await postJSON('/api/carryover/publish',{base_run_id:$('#base-run').value.trim(),current_run_id:$('#current-run').value.trim()});
   renderDraft(data.record.draft);
   log(data.changed?'承接结果已发布，旧版封账与原证据保持不变。':'承接记录此前已发布，本次重跑未新增记录。',false);
   loadIndex();
  }catch(e){feedback.textContent=e.message;log('发布失败：'+e.message+'（旧版封账与证据保留）');}
 });
 loadIndex();
}

// 归档页封账按钮
const sealBtn=document.querySelector('#seal');
if(sealBtn)sealBtn.addEventListener('click',async()=>{
 try{
  const r=await fetch('/api/runs/'+sealBtn.dataset.id+'/seal',{method:'POST'});
  const data=await r.json();
  if(!r.ok)throw Error(data.detail);
  feedback.textContent='已封账：'+data.seal.slice(0,16)+'…';
 }catch(e){feedback.textContent=e.message;}
});

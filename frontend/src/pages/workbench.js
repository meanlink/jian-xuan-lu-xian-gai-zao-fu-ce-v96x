import {datasets,jobs} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {status,progressBar} from '../components/common/status.js';
import {esc,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const ACTIVE=['queued','running','paused'];

function queueItem(j){
 const canOperate=session.user.role!=='viewer';
 const p=j.progress||{};
 const action=j.status==='paused'
   ?'<button class="button small" data-resume="'+j.id+'" '+(canOperate?'':'disabled')+'>恢复</button>'
   :(ACTIVE.includes(j.status)?'<button class="button small" data-pause="'+j.id+'" '+(canOperate?'':'disabled')+'>暂停</button>':'<a class="button small" href="#/results/'+j.id+'">查看结果</a>');
 return '<article class="queue-item"><div class="queue-main"><div class="queue-title"><strong>'+esc(j.name)+'</strong>'+status(j.status)+'</div><p class="queue-meta">建立 '+date(j.created_at)+' · 共 '+p.total+' 段'+(j.status==='completed'?' · 已完成':'')+'</p>'+progressBar(p)+'<p class="queue-reason">'+esc(j.waiting_reason||'')+'</p></div><div class="queue-actions">'+action+'<a class="text-button" href="#/results/'+j.id+'">结果页 ↗</a></div></article>';
}

export async function workbench(){
 [workspace.datasets,workspace.jobs]=await Promise.all([datasets.list(),jobs.list()]);const m=workspace.meta;
 const active=workspace.jobs.filter(j=>ACTIVE.includes(j.status));
 const history=workspace.jobs.filter(j=>!ACTIVE.includes(j.status)).slice(0,6);
 return heading('03 / ANALYSIS LAB',m.workbench_title,'选择业务批次和分析口径。分段推进、暂停保留已完成段，全部段到齐才形成可批准结论。')+'<div class="workbench-grid"><section class="panel"><form id="job-form"><label>分析任务名称<input name="name" required minlength="2" value="'+esc(m.name)+' · 复核试算"></label><label>数据批次<select name="dataset_id" required>'+workspace.datasets.map(d=>'<option value="'+d.id+'">'+esc(d.name)+' · '+d.rows+' 条</option>').join('')+'</select></label><div class="parameter-grid">'+m.parameters.map(p=>'<label>'+esc(p.label)+(p.type==='select'?'<select name="param-'+esc(p.key)+'">'+p.options.map(o=>'<option value="'+esc(o.value)+'" '+(o.value==p.default?'selected':'')+'>'+esc(o.label)+'</option>').join('')+'</select>':'<input type="number" step="any" name="param-'+esc(p.key)+'" value="'+esc(p.default)+'" min="'+esc(p.min??'')+'" max="'+esc(p.max??'')+'" required>')+'</label>').join('')+'</div><button type="submit" class="primary" '+(session.user.role==='viewer'||!workspace.datasets.length?'disabled':'')+'>'+esc(m.action_label||'执行分析')+' ↗</button><p class="hint">提交后由后台分段计算。观察员可查看参数和结果，无法提交。</p></form></section><section class="method-sheet"><p class="eyebrow">METHOD / BUSINESS RULE</p><h2>'+esc(m.method)+'</h2><p>'+esc(m.tagline)+'</p><h3>业务判读边界</h3><ul>'+m.limitations.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul><div class="method-trace"><span>输入</span><strong>'+esc(m.dataset_name)+'</strong><span>输出</span><strong>可复核结果 / 参数版本 / 分发归档</strong></div></section></div>'+
 '<section class="panel queue-panel"><div class="section-title"><h2>待办与分段进度</h2><span>各负责人轮流推进 · '+(active.length?active.length+' 项进行中':'暂无进行中的任务')+'</span></div><div id="queue-active">'+(active.length?active.map(queueItem).join(''):'<p class="empty">提交任务后会在此显示分段进度、暂停或恢复入口以及等待原因。</p>')+'</div></section>'+
 (history.length?'<section class="panel"><div class="section-title"><h2>近期分析</h2><span>旧分析仍可查看</span></div><div id="queue-history">'+history.map(queueItem).join('')+'</div></section>':'');
}

let poller=null;
export function stopWorkbenchPolling(){if(poller){clearInterval(poller);poller=null;}}

export function bindWorkbench(){
 stopWorkbenchPolling();
 const form=document.querySelector('#job-form');
 if(form)submit(form,async f=>{const params={};for(const p of workspace.meta.parameters){const value=f.get('param-'+p.key);params[p.key]=p.type==='number'?Number(value):value;}const j=await jobs.create({name:f.get('name'),dataset_id:Number(f.get('dataset_id')),params});workspace.selectedJob=j.id;notify('分析任务已提交，正在分段计算');location.hash='#/results/'+j.id;});
 const bindButtons=()=>{
   document.querySelectorAll('[data-pause]').forEach(b=>b.onclick=async()=>{try{await jobs.pause(b.dataset.pause);notify('已暂停，已完成段已保留');await refreshQueue();}catch(e){notify(e.message,true);}});
   document.querySelectorAll('[data-resume]').forEach(b=>b.onclick=async()=>{try{await jobs.resume(b.dataset.resume);notify('已恢复，从未完成段继续');await refreshQueue();}catch(e){notify(e.message,true);}});
 };
 // 只刷新待办面板，避免轮询覆盖正在填写的表单。
 const refreshQueue=async()=>{
   try{
     workspace.jobs=await jobs.list();
     const active=document.querySelector('#queue-active'),history=document.querySelector('#queue-history');
     if(active)active.innerHTML=workspace.jobs.filter(j=>ACTIVE.includes(j.status)).map(queueItem).join('')||'<p class="empty">提交任务后会在此显示分段进度、暂停或恢复入口以及等待原因。</p>';
     if(history)history.innerHTML=workspace.jobs.filter(j=>!ACTIVE.includes(j.status)).slice(0,6).map(queueItem).join('');
     bindButtons();
   }catch(e){/* 轮询失败不打断页面，用户仍可手动刷新 */}
 };
 bindButtons();
 if(workspace.jobs.some(j=>ACTIVE.includes(j.status)&&j.status!=='paused'))poller=setInterval(refreshQueue,4000);
}

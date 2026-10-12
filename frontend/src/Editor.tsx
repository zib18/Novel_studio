import {useCallback,useEffect,useRef,useState} from 'react';
import {api,ApiError,errorMessage,post} from './api';
import {editDraft,getCache,getDraft,initialiseDraft,listBackups,markConflict,putCache,resolveConflict,syncDraft,type Draft} from './drafts';
import {Badge,Compare,Empty,labelStatus,Modal} from './components';
import type {Chapter,Finding,Job,Novel,Page,Revision,ServerState,User} from './types';
const terminal=new Set(['awaiting_author','needs_author_review','failed','cancelled','applied','rejected']);
type RevisionResult=Revision|{revision:Revision}|{current_revision:Revision;saved_revision?:Revision};
const unwrapRevision=(value:RevisionResult)=>'current_revision'in value?value.saved_revision??value.current_revision:'revision'in value?value.revision:value;
export function Editor({user,novel,notify}:{user:User;novel:Novel;notify:(s:string)=>void}){
 const [nextOffset,setNextOffset]=useState(0);
 const [chapters,setChapters]=useState<Chapter[]>([]),[total,setTotal]=useState(0),[chapterId,setChapterId]=useState(''),[chapter,setChapter]=useState<Chapter|null>(null);
 const [draft,setDraft]=useState<Draft|null>(null),[text,setText]=useState(''),[loading,setLoading]=useState(true),[saving,setSaving]=useState(false);
 const [lock,setLock]=useState<'pending'|'owned'|'blocked'|'unsupported'>('pending'),[persisting,setPersisting]=useState(false),editEpoch=useRef(0);
 const [newTitle,setNewTitle]=useState(''),[creating,setCreating]=useState(false),[instructions,setInstructions]=useState(''),[passes,setPasses]=useState(Number(novel.settings?.max_review_passes??3));
 const [job,setJob]=useState<Job|null>(null),[jobs,setJobs]=useState<Job[]>([]),[jobBusy,setJobBusy]=useState(false),[ai,setAi]=useState<ServerState|null>(null);
 const [criticalConfirmed,setCriticalConfirmed]=useState(false),[historyOpen,setHistoryOpen]=useState(false),[revisions,setRevisions]=useState<Revision[]>([]),[comparison,setComparison]=useState<Revision|null>(null),[backups,setBackups]=useState<Draft[]>([]),[conflictOpen,setConflictOpen]=useState(false);
 const editor=useRef({chapterId:'',text:''}),pendingWrites=useRef<Promise<unknown>>(Promise.resolve());
 const [transitionBusy,setTransitionBusy]=useState(false),editingFrozen=useRef(false);
 const freezeEditing=()=>{editingFrozen.current=true;setTransitionBusy(true);};
 const thawEditing=()=>{editingFrozen.current=false;setTransitionBusy(false);};
 const notifyError=useCallback((e:unknown)=>notify(errorMessage(e)),[notify]);
 useEffect(()=>{
  if(!chapterId)return;setLock('pending');if(!navigator.locks){setLock('unsupported');return;}
  let cancelled=false,release:(()=>void)|undefined;
  void navigator.locks.request('novel-studio-editor:'+user.id+':'+chapterId,{ifAvailable:true},async acquired=>{
   if(cancelled)return;if(!acquired){setLock('blocked');return;}setLock('owned');await new Promise<void>(resolve=>{release=resolve;if(cancelled)resolve();});
  }).catch(()=>{if(!cancelled)setLock('unsupported');});
  return()=>{cancelled=true;release?.();};
 },[chapterId,user.id]);
 const loadChapters=useCallback(async(append=false,offset=0)=>{
 const cacheKey='chapters:'+novel.id;
 const metadata=(c:Chapter):Chapter=>({id:c.id,novel_id:c.novel_id,title:c.title,number:c.number,status:c.status,current_revision_id:c.current_revision_id});
 const merge=(previous:Chapter[],fresh:Chapter[])=>{const records=new Map<string,Chapter>();for(const c of previous)records.set(c.id,metadata(c));for(const c of fresh)records.set(c.id,metadata(c));return [...records.values()].sort((a,b)=>a.number-b.number||a.id.localeCompare(b.id));};
 try{
  const [page,previous,preferred]=await Promise.all([api<Page<Chapter>>('/novels/'+novel.id+'/chapters?limit=100&offset='+offset),getCache<Page<Chapter>>(user.id,cacheKey),getCache<string>(user.id,'selected-chapter:'+novel.id)]);
  let combined=merge(previous?.items??[],page.items);
  if(preferred&&!combined.some(c=>c.id===preferred)){const detail=await getCache<Chapter>(user.id,'chapter:'+preferred);if(detail&&detail.novel_id===novel.id)combined=merge(combined,[detail]);}
  setTotal(page.total);setNextOffset(offset+page.items.length);setChapters(combined);
  await putCache(user.id,cacheKey,{items:combined,total:page.total});
  if(!append)setChapterId(current=>current||(combined.some(c=>c.id===preferred)?preferred!:page.items[0]?.id??combined[0]?.id??''));
 }catch(error){
  if(!append){
   const [cached,preferred]=await Promise.all([getCache<Page<Chapter>>(user.id,cacheKey),getCache<string>(user.id,'selected-chapter:'+novel.id)]);
   let combined=cached?.items??[];
   if(preferred&&!combined.some(c=>c.id===preferred)){const detail=await getCache<Chapter>(user.id,'chapter:'+preferred);if(detail&&detail.novel_id===novel.id)combined=merge(combined,[detail]);}
   if(combined.length){setChapters(combined);setTotal(cached?.total??combined.length);setNextOffset(0);setChapterId(current=>current||(combined.some(c=>c.id===preferred)?preferred!:combined[0]?.id??''));}
  }
  notifyError(error);
 }finally{setLoading(false);}
},[novel.id,user.id,notifyError]);
 useEffect(()=>{
  void loadChapters();void api<ServerState>('/system/status').then(setAi).catch(notifyError);
  void api<Page<Job>>('/novels/'+novel.id+'/jobs').then(page=>{setJobs(page.items);setJob(page.items.find(j=>!terminal.has(j.status))??page.items[0]??null);}).catch(()=>{});
 },[loadChapters,novel.id,notifyError]);
 useEffect(()=>{
  if(!chapterId)return;let cancelled=false;setChapter(null);setDraft(null);setText('');editor.current={chapterId,text:''};
  const load=async()=>{try{
   const detail=await api<Chapter>('/chapters/'+chapterId);if(!detail.current_revision)throw new Error('Сервер не вернул текст главы.');
   const local=await initialiseDraft(user.id,detail,detail.current_revision);if(cancelled)return;
   setChapter(detail);setDraft(local);setText(local.text);editor.current={chapterId,text:local.text};if(local.state==='conflict')setConflictOpen(true);await putCache(user.id,'selected-chapter:'+novel.id,chapterId);
  }catch(error){const [local,cached]=await Promise.all([getDraft(user.id,chapterId),getCache<Chapter>(user.id,'chapter:'+chapterId)]);if(cancelled)return;if(local){setDraft(local);setText(local.text);editor.current={chapterId,text:local.text};setChapter(cached??chapters.find(c=>c.id===chapterId)??null);}notifyError(error);}};
  void load();return()=>{cancelled=true;};
 },[chapterId,user.id,novel.id,notifyError]);
 const save=useCallback(async():Promise<Draft>=>{
  const id=editor.current.chapterId;if(!id)throw new Error('Выберите главу.');await pendingWrites.current;setSaving(true);
  try{
   const result=await syncDraft(user.id,id,async(currentId,payload)=>unwrapRevision(await api<RevisionResult>('/chapters/'+currentId+'/text',{method:'PUT',body:JSON.stringify(payload)})));
   if(editor.current.chapterId===id)setDraft({...result,text:editor.current.text,state:editor.current.text===result.syncedText?'synced':'local'});return result;
  }catch(error){
   if(error instanceof ApiError&&error.status===409){try{const detail=await api<Chapter>('/chapters/'+id);if(detail.current_revision){const local=await markConflict(user.id,id,detail.current_revision);if(editor.current.chapterId===id){setDraft(local);setConflictOpen(true);}}}catch{}}
   throw error;
  }finally{if(editor.current.chapterId===id)setSaving(false);}
 },[user.id]);
 useEffect(()=>{
  if(!draft||!chapterId||lock!=='owned'||(text===draft.syncedText&&!draft.pending&&draft.state==='synced')||draft.state==='conflict')return;
  const timer=window.setTimeout(()=>{void save().catch(notifyError);},1400);return()=>window.clearTimeout(timer);
 },[text,chapterId,lock,draft?.syncedText,draft?.state,draft?.pending?.mutation_id,save,notifyError]);
 useEffect(()=>{
  const retry=()=>{if(lock==='owned'&&editor.current.chapterId)void save().catch(notifyError);};window.addEventListener('novel-session-ready',retry);
  const warn=(event:BeforeUnloadEvent)=>{if(editor.current.text!==draft?.syncedText)event.preventDefault();};window.addEventListener('beforeunload',warn);
  return()=>{window.removeEventListener('novel-session-ready',retry);window.removeEventListener('beforeunload',warn);};
 },[save,lock,notifyError,draft?.syncedText]);
 useEffect(()=>{
  if(!job||terminal.has(job.status))return;let stopped=false;const interval=window.setInterval(()=>{void api<Job>('/jobs/'+job.id).then(next=>{if(!stopped)setJob(next);}).catch(notifyError);},3000);
  return()=>{stopped=true;window.clearInterval(interval);};
 },[job?.id,job?.status,notifyError]);
 const onText=(value:string)=>{
  if(lock!=='owned'||editingFrozen.current)return;const epoch=++editEpoch.current;setPersisting(true);setText(value);editor.current={chapterId,text:value};setDraft(current=>current?{...current,text:value,state:current.state==='conflict'?'conflict':'local'}:current);const id=chapterId;
  pendingWrites.current=pendingWrites.current.catch(()=>undefined).then(()=>editDraft(user.id,id,value)).then(result=>{if(epoch===editEpoch.current)setPersisting(false);return result;}).catch(error=>{notify('Не удалось сохранить текст на устройстве. Не закрывайте редактор: '+errorMessage(error));throw error;});void pendingWrites.current.catch(()=>{});
 };
 const createChapter=async(e:React.FormEvent)=>{
  e.preventDefault();setCreating(true);try{const created=await post<Chapter>('/novels/'+novel.id+'/chapters',{title:newTitle.trim()});setNewTitle('');setChapterId(created.id);await loadChapters();}catch(error){notifyError(error);}finally{setCreating(false);}
 };
 const startJob=async(kind:'write'|'review')=>{
  setJobBusy(true);let requestCacheKey='';
  try{
   const current=await save(),key='job-request:'+current.chapterId;requestCacheKey=key;
   type RequestRecord={uuid:string;payload:Record<string,unknown>};
   const previous=await getCache<RequestRecord|null>(user.id,key),request=previous??{uuid:crypto.randomUUID(),payload:{chapter_id:current.chapterId,base_revision_id:current.baseRevisionId,kind,instructions,max_review_passes:passes}};
   if(previous)notify('Повторяем ранее отправленный запрос без создания дубликата.');await putCache(user.id,key,request);
   const next=await post<Job>('/novels/'+novel.id+'/jobs',request.payload,{'Idempotency-Key':request.uuid});await putCache(user.id,key,null);setJob(next);setJobs(prev=>[next,...prev.filter(j=>j.id!==next.id)]);setCriticalConfirmed(false);
  }catch(error){if(requestCacheKey&&error instanceof ApiError&&error.status>=400&&error.status<500&&![408,425,429].includes(error.status))await putCache(user.id,requestCacheKey,null).catch(()=>{});notifyError(error);}finally{setJobBusy(false);}
 };
 const applyJob=async()=>{
  if(!job||editingFrozen.current)return;freezeEditing();setJobBusy(true);
  try{
   const current=await save(),textBeforeApply=editor.current.text;
   const result=unwrapRevision(await post<RevisionResult>('/jobs/'+job.id+'/apply',{base_revision_id:current.baseRevisionId,mutation_id:crypto.randomUUID(),acknowledge_critical:criticalConfirmed}));await pendingWrites.current;
   if(editor.current.chapterId!==current.chapterId||editor.current.text!==textBeforeApply){await markConflict(user.id,current.chapterId,result);if(editor.current.chapterId===current.chapterId){setDraft(await getDraft(user.id,current.chapterId)??null);setConflictOpen(true);}notify('Пока результат применялся, вы изменили текст. Обе версии сохранены; сравните их.');}
   else{const local=await resolveConflict(user.id,current.chapterId,result.text,result);setText(result.text);setDraft(local);editor.current.text=result.text;notify('Редакция принята и сохранена. Канон остаётся под вашим контролем.');}
   setJob(await api<Job>('/jobs/'+job.id));
  }catch(error){notifyError(error);}finally{setJobBusy(false);thawEditing();}
 };
 const jobAction=async(action:'cancel'|'reject')=>{if(!job)return;try{await post('/jobs/'+job.id+'/'+action);setJob(await api<Job>('/jobs/'+job.id));}catch(error){notifyError(error);}};
 const openHistory=async()=>{try{const page=await api<Page<Revision>>('/chapters/'+chapterId+'/revisions');setRevisions(page.items);setBackups(await listBackups(user.id,chapterId));setHistoryOpen(true);}catch(error){notifyError(error);}};
 const restore=async(revision:Revision)=>{
  if(!window.confirm('Восстановить эту редакцию? Текущий текст останется в истории.'))return;
  if(editingFrozen.current)return;freezeEditing();
  try{
   const current=await save(),before=editor.current.text;
   const result=unwrapRevision(await post<RevisionResult>('/chapters/'+chapterId+'/restore',{base_revision_id:current.baseRevisionId,revision_id:revision.id,mutation_id:crypto.randomUUID()}));await pendingWrites.current;
   if(editor.current.chapterId!==chapterId){await markConflict(user.id,chapterId,result);notify('Редакция прежней главы сохранена; откройте её для сравнения.');return;}
   if(editor.current.text!==before){setDraft(await markConflict(user.id,chapterId,result));setConflictOpen(true);}else{const next=await resolveConflict(user.id,chapterId,result.text,result);setDraft(next);setText(result.text);editor.current.text=result.text;}
   setHistoryOpen(false);setComparison(null);
  }catch(error){notifyError(error);}finally{thawEditing();}
 };
 const chooseConflict=async(server:boolean)=>{
  if(!draft?.serverRevision){notify('Серверная версия недоступна. Восстановите связь и повторите сохранение.');return;}
  if(editingFrozen.current)return;freezeEditing();
  try{await pendingWrites.current;const next=await resolveConflict(user.id,chapterId,server?draft.serverRevision.text:editor.current.text,draft.serverRevision);setDraft(next);setText(next.text);editor.current.text=next.text;setConflictOpen(false);if(!server)await save();}catch(error){notifyError(error);}finally{thawEditing();}
 };
 const approve=async()=>{
  if(!window.confirm('Утвердить эту редакцию главы? Утверждение текста само по себе не утверждает новые записи канона.'))return;
  try{const current=await save();await post('/chapters/'+chapterId+'/approve',{base_revision_id:current.baseRevisionId});notify('Глава утверждена.');await loadChapters();}catch(error){notifyError(error);}
 };
 const findings=job?.artifacts?.findings??job?.findings??[],hasCritical=job?.artifacts?.check_complete===false||findings.some(f=>f.severity==='critical'&&f.status!=='resolved'),candidate=job?.artifacts?.revised_text??job?.artifacts?.draft_text,belongs=job?.chapter_id===chapterId;
 return <div className="writing-room"><aside className="chapter-list"><div className="section-heading"><h2>Рукопись</h2><span className="muted">{total}</span></div>
 <form className="new-chapter" onSubmit={e=>void createChapter(e)}><input aria-label="Название новой главы" data-testid="chapter-create-title" required value={newTitle} onChange={e=>setNewTitle(e.target.value)} placeholder="Новая глава" maxLength={200} /><button type="submit" data-testid="chapter-create-submit" disabled={creating} aria-label="Создать главу">＋</button></form>
 <div className="chapter-scroll">{chapters.map(c=><button className={'chapter-link '+(c.id===chapterId?'selected':'')} data-testid={'chapter-open-'+c.id} key={c.id} onClick={()=>setChapterId(c.id)}><span className="chapter-number">{String(c.number).padStart(2,'0')}</span><span><strong>{c.title}</strong><small>{labelStatus(c.status??'draft')}</small></span></button>)}{nextOffset<total&&<button className="ghost" onClick={()=>void loadChapters(true,nextOffset)}>Загрузить ещё</button>}</div><div className="chapter-list-note">Большая история.<br />Одна глава за другой.</div></aside>
 <section className="manuscript">{!chapterId?loading?<Empty title="Открываем рукопись…" text="Загружаем сохранённые главы." />:<Empty title="Первая глава начинается здесь" text="Создайте главу в списке слева. Можно писать самостоятельно или дать задание писателю." />:!draft?<Empty title="Открываем главу" text="Если текст ещё не сохранялся на устройстве, для первого открытия нужен сервер." />:<>
 <header className="manuscript-heading"><div><span className="eyebrow">ГЛАВА {chapter?.number??''}</span><h1>{chapter?.title??'Рукопись'}</h1></div><div className="editor-tools"><button className="ghost" onClick={()=>void openHistory()}>История</button><button className="ghost" disabled={lock!=='owned'} onClick={()=>void approve()}>Утвердить</button><button onClick={()=>void save().catch(notifyError)} disabled={saving||lock!=='owned'}>{saving?'Сохраняем…':'Сохранить'}</button></div></header>
 {lock!=='owned'&&<div className="notice warning">{lock==='blocked'?'Глава уже редактируется в другой вкладке. Закройте её и откройте главу снова.':lock==='unsupported'?'Для безопасного редактирования нужен современный Chrome, Edge или Firefox и HTTPS (либо localhost).':'Подготавливаем безопасное редактирование…'}</div>}
 {draft.state==='conflict'&&<div className="notice warning">Есть конфликт редакций. Ваш текст сохранён на устройстве.<button onClick={()=>setConflictOpen(true)}>Сравнить</button></div>}
 <textarea className="prose-editor" aria-label="Текст главы" data-testid="chapter-editor" spellCheck readOnly={lock!=='owned'||transitionBusy} value={text} onChange={e=>onText(e.target.value)} placeholder="Начните писать. Здесь можно свободно редактировать текст — ИИ не заменит ваши правки без принятия новой редакции." />
 <footer className="editor-status"><span>{text.length.toLocaleString('ru-RU')} знаков с пробелами</span><span className={draft.state==='synced'?'success':''}>{persisting?'Сохраняем на устройстве…':saving?'Синхронизация…':draft.state==='synced'?'✓ Синхронизировано':draft.state==='conflict'?'Конфликт · текст на устройстве':'Сохранено на устройстве'}</span></footer></>}</section>
 <aside className="assistant-panel"><header><span className="eyebrow">СОАВТОР ПОД ВАШИМ КОНТРОЛЕМ</span><h2>Мастерская главы</h2></header><label>Что должно произойти?<textarea rows={5} value={instructions} onChange={e=>setInstructions(e.target.value)} placeholder="Опишите события, настроение и ограничения свободными словами." /></label>
 <label className="inline-label">Редакторских проходов<input type="number" min={1} max={10} value={passes} onChange={e=>setPasses(Math.max(1,Math.min(10,Number(e.target.value))))} /></label>
 {ai&&!ai.ai_configured&&<p className="small warning-text">ИИ пока не подключён. Добавьте ключ OpenAI на сервере по инструкции. Редактор и сохранение доступны.</p>}
 <button className="primary full" disabled={!draft||lock!=='owned'||jobBusy||ai?.ai_configured===false} onClick={()=>void startJob('write')}>{jobBusy?'Отправляем…':'✦ Написать главу'}</button><button className="full" disabled={!draft||lock!=='owned'||!text.trim()||jobBusy||ai?.ai_configured===false} onClick={()=>void startJob('review')}>Проверить и отредактировать</button><p className="small muted">Задача выполняется на сервере. Можно закрыть приложение и вернуться к результату.</p>
 {jobs.length>0&&<label>Задачи романа<select value={job?.id??''} onChange={e=>{setJob(jobs.find(j=>j.id===e.target.value)??null);setCriticalConfirmed(false);}}>{jobs.map(j=><option key={j.id} value={j.id}>{j.kind==='write'?'Написание':'Проверка'} · {labelStatus(j.id===job?.id?job.status:j.status)} · {j.id.slice(0,6)}</option>)}</select></label>}
 {job&&<div className="job-card"><div className="section-heading"><h3>Процесс</h3><Badge tone={job.status==='failed'?'danger':''}>{labelStatus(job.status)}</Badge></div><p className="small">{job.stage||'Задача создана'}{job.review_passes!=null&&' · проходов: '+job.review_passes}</p>
 {!terminal.has(job.status)&&<button className="ghost" onClick={()=>void jobAction('cancel')}>Отменить задачу</button>}{job.errors!=null&&<details><summary>Ошибка выполнения</summary><pre>{JSON.stringify(job.errors,null,2)}</pre></details>}{job.error!=null&&<p className="error">{typeof job.error==='string'?job.error:JSON.stringify(job.error)}</p>}
 {job.usage&&<details><summary>Запросы, токены и бюджет</summary><pre>{JSON.stringify(job.usage,null,2)}</pre></details>}{job.usage&&<p className="small muted">Оценка расхода: {typeof job.usage.cost_usd==='number'?'$'+job.usage.cost_usd.toFixed(4):'см. отчёт'}<br />Фактическая оплата определяется провайдером API.</p>}{findings.length>0&&<FindingList findings={findings} />}
 {candidate!=null&&<><details className="candidate"><summary>Полученный текст · {candidate.length.toLocaleString('ru-RU')} знаков</summary><div className="candidate-prose">{candidate}</div></details>{job.artifacts?.draft_text&&job.artifacts?.revised_text&&<details><summary>Сравнить черновик и редакцию</summary><Compare left={job.artifacts.draft_text} right={job.artifacts.revised_text} leftTitle="Черновик ИИ" rightTitle="После редакторов" /></details>}
 {hasCritical&&<div className="critical-note"><strong>Требуется авторская проверка</strong><p>Текст требует авторской проверки.</p><label className="check"><input type="checkbox" checked={criticalConfirmed} onChange={e=>setCriticalConfirmed(e.target.checked)} />Принимаю текст с указанными нарушениями или неполной проверкой</label></div>}
 {belongs&&['awaiting_author','needs_author_review'].includes(job.status)&&<div className="stack-actions"><button className="primary" disabled={jobBusy||lock!=='owned'||(hasCritical&&!criticalConfirmed)} onClick={()=>void applyJob()}>Принять эту редакцию</button><button className="ghost" onClick={()=>void jobAction('reject')}>Отклонить результат</button></div>}{!belongs&&<p className="small muted">Откройте главу этой задачи, чтобы принять результат.</p>}</>}
 {job.artifacts?.limitations!=null&&<details><summary>Ограничения проверки</summary><pre>{JSON.stringify(job.artifacts.limitations,null,2)}</pre></details>}</div>}
 </aside>
 {conflictOpen&&draft&&<Modal title="Сравнение редакций" onClose={()=>setConflictOpen(false)}><p>На сервере появился другой текст. Выберите версию явно; текущий локальный вариант сохранится в резервной истории.</p>{draft.serverRevision?<Compare left={draft.serverRevision.text} right={text} />:<p className="warning-text">Для загрузки серверной версии восстановите связь и нажмите «Сохранить».</p>}<div className="actions"><button disabled={!draft.serverRevision||lock!=='owned'} onClick={()=>void chooseConflict(true)}>Использовать серверную</button><button className="primary" disabled={!draft.serverRevision||lock!=='owned'} onClick={()=>void chooseConflict(false)}>Сохранить мой текст поверх серверной</button></div><p className="small muted">Для ручного объединения закройте окно, отредактируйте свой текст и снова откройте сравнение.</p></Modal>}
 {historyOpen&&<Modal title="История главы" onClose={()=>{setHistoryOpen(false);setComparison(null);}}><div className="history-list">{revisions.map(r=><div key={r.id}><span>{r.created_at?new Date(r.created_at).toLocaleString('ru-RU'):r.id.slice(0,8)} · {r.source??'редакция'}</span><button onClick={()=>void api<Revision>('/revisions/'+r.id).then(setComparison).catch(notifyError)}>Сравнить</button></div>)}</div>{comparison&&<><Compare left={comparison.text} right={text} leftTitle="Выбранная редакция" rightTitle="Текущий текст" /><button disabled={lock!=='owned'} onClick={()=>void restore(comparison)}>Восстановить выбранную редакцию</button></>}
 {backups.length>0&&<details><summary>Локальные версии до разрешения конфликтов ({backups.length})</summary>{backups.map((b,i)=><div key={i} className="backup"><p>{new Date(b.modifiedAt).toLocaleString('ru-RU')}</p><details><summary>Посмотреть текст</summary><pre>{b.text}</pre></details><button disabled={lock!=='owned'} onClick={()=>{if(window.confirm('Поместить этот локальный вариант в редактор? Текущий текст сначала сохраняется.'))void save().then(()=>{onText(b.text);setHistoryOpen(false);}).catch(notifyError);}}>Вернуть в редактор</button></div>)}</details>}</Modal>}
 </div>;
}
function FindingList({findings}:{findings:Finding[]}){
 return <section className="findings"><h3>Замечания · {findings.length}</h3>{findings.map((f,i)=><article className={'finding '+(f.severity==='critical'?'critical':'')} key={i}><div className="section-heading"><Badge tone={f.severity==='critical'?'danger':''}>{labelStatus(f.severity)}</Badge>{f.status&&<small>{labelStatus(f.status)}</small>}</div><strong>{f.category??'Проверка'}</strong>{f.quote&&<blockquote>{f.quote}</blockquote>}<p>{f.explanation??f.message}</p>{(f.suggestion||f.replacement)&&<p className="fix"><b>Как исправить: </b>{f.replacement??f.suggestion}</p>}<small className="muted">Правило: {f.rule_id??f.rule??'см. категорию замечания'}</small></article>)}</section>;
}

import {useCallback,useEffect,useState} from 'react';
import {api,ApiError,errorMessage,post} from './api';
import {getCache,listUnsyncedNovelDrafts,putCache} from './drafts';
import type {Draft} from './drafts';
import {Empty,Modal} from './components';
import type {Chapter,Novel,Page,User} from './types';
type LocalChapter={draft:Draft;title:string};
type Confirmation={novel:Novel;blocked:LocalChapter[]};
export function Library({user,novels,select,notify,refresh,onTrashed,offline}:{user:User;novels:Novel[];select:(n:Novel)=>void;notify:(s:string)=>void;refresh:()=>Promise<void>;onTrashed:(n:Novel)=>void;offline:boolean}){
 const [creating,setCreating]=useState(false),[busy,setBusy]=useState(false),[title,setTitle]=useState(''),[genre,setGenre]=useState(''),[concept,setConcept]=useState('');
 const [trashOpen,setTrashOpen]=useState(false),[trash,setTrash]=useState<Novel[]>([]),[trashLoading,setTrashLoading]=useState(false),[trashError,setTrashError]=useState(''),[confirmation,setConfirmation]=useState<Confirmation|null>(null);
 const loadTrash=useCallback(async()=>{
  setTrashLoading(true);setTrashError('');try{const page=await api<Page<Novel>>('/trash/novels');setTrash(page.items);}catch(error){setTrashError(errorMessage(error));}finally{setTrashLoading(false);}
 },[]);
 useEffect(()=>{if(trashOpen&&!offline)void loadTrash();},[trashOpen,offline,loadTrash]);
 const localChapters=async(novelId:string):Promise<LocalChapter[]>=>Promise.all((await listUnsyncedNovelDrafts(user.id,novelId)).map(async draft=>{
  const chapter=await getCache<Chapter>(user.id,'chapter:'+draft.chapterId);return {draft,title:chapter?'Глава '+chapter.number+'. '+chapter.title:'Глава с локальными изменениями'};
 }));
 const create=async(e:React.FormEvent)=>{e.preventDefault();if(offline||busy)return;setBusy(true);try{const novel=await post<Novel>('/novels',{title:title.trim(),genre,concept});await putCache(user.id,'novel:'+novel.id,novel);await refresh();select(novel);}catch(error){notify(errorMessage(error));}finally{setBusy(false);}};
 const prepareTrash=async(novel:Novel)=>{
  if(offline||busy)return;setBusy(true);try{const current=await api<Novel>('/novels/'+novel.id);setConfirmation({novel:current,blocked:await localChapters(novel.id)});}catch(error){notify(errorMessage(error));await refresh();}finally{setBusy(false);}
 };
 const moveToTrash=async()=>{
  if(!confirmation||offline||busy)return;setBusy(true);try{
   const blocked=await localChapters(confirmation.novel.id);if(blocked.length){setConfirmation({...confirmation,blocked});return;}
   const deleted=await post<Novel>('/novels/'+confirmation.novel.id+'/trash',{base_version:confirmation.novel.version,expected_canon_version:confirmation.novel.canon_version});
   setConfirmation(null);onTrashed(deleted);await refresh();if(trashOpen)await loadTrash();notify('Роман перемещён в корзину. Его можно восстановить вместе с главами и историей.');
  }catch(error){notify(errorMessage(error));if(error instanceof ApiError&&error.status===409){setConfirmation(null);await refresh();if(trashOpen)await loadTrash();}}finally{setBusy(false);}
 };
 const restore=async(novel:Novel)=>{
  if(offline||busy)return;setBusy(true);try{
   await post<Novel>('/novels/'+novel.id+'/restore',{base_version:novel.version,expected_canon_version:novel.canon_version});setTrash(current=>current.filter(n=>n.id!==novel.id));await refresh();notify('Роман восстановлен. Прежние задачи ИИ остаются отменёнными.');
  }catch(error){notify(errorMessage(error));if(error instanceof ApiError&&error.status===409){await loadTrash();await refresh();}}finally{setBusy(false);}
 };
 const openLocalChapter=async(item:LocalChapter)=>{if(!confirmation)return;try{await putCache(user.id,'selected-chapter:'+confirmation.novel.id,item.draft.chapterId);select(confirmation.novel);}catch(error){notify(errorMessage(error));}};
 const visible=novels.filter(n=>!n.deleted_at);
 return <><header className="page-heading"><div><span className="eyebrow">ВАШИ ИСТОРИИ</span><h1>Моя библиотека</h1><p className="muted">У каждого романа — собственный мир, канон и голоса героев.</p></div><div className="actions"><button data-testid="novel-trash-toggle" disabled={offline||busy} aria-expanded={trashOpen} onClick={()=>setTrashOpen(!trashOpen)}>Корзина</button><button className="primary" disabled={offline||busy} onClick={()=>setCreating(!creating)}>＋ Новый роман</button></div></header>
 {offline&&<p className="small warning-text" role="status">Для создания, удаления и восстановления романов нужно подключение к серверу. Сохранённые главы доступны на устройстве.</p>}
 {trashOpen&&<section className="panel trash-panel" aria-label="Корзина романов"><div className="section-heading"><h2>Корзина романов</h2><button disabled={offline||busy||trashLoading} onClick={()=>void loadTrash()}>Обновить</button></div><p className="small muted">Удалённые романы скрыты из библиотеки. Главы, канон и история сохраняются для восстановления.</p>{trashError&&<p className="error" role="alert">{trashError}</p>}{trashLoading?<p role="status">Загружаем корзину…</p>:trash.length===0?<p className="small muted">Корзина пуста.</p>:<ul className="trash-list">{trash.map(n=><li key={n.id}><div><strong>{n.title}</strong><p className="small muted">{n.genre||'Жанр не указан'}</p></div><button data-testid={'novel-restore-'+n.id} disabled={offline||busy} onClick={()=>void restore(n)}>Восстановить</button></li>)}</ul>}</section>}
 {creating&&<form className="panel creation-form" onSubmit={e=>void create(e)}><h2>С чего начнётся история?</h2><div className="form-row"><label>Название<input required data-testid="novel-create-title" maxLength={200} value={title} onChange={e=>setTitle(e.target.value)} placeholder="Название вашего романа" /></label><label>Жанр<input maxLength={100} value={genre} onChange={e=>setGenre(e.target.value)} placeholder="Например, современная романтика" /></label></div><label>Идея романа<textarea rows={4} value={concept} onChange={e=>setConcept(e.target.value)} placeholder="Расскажите свободными словами о героях и основном конфликте." /></label><div className="actions"><button className="primary" data-testid="novel-create-submit" disabled={offline||busy}>{busy?'Создаём…':'Создать роман'}</button><button type="button" className="ghost" disabled={busy} onClick={()=>setCreating(false)}>Отмена</button></div></form>}
 {visible.length===0?<Empty title="Место для вашей истории" text="Создайте роман или восстановите его из корзины." />:<div className="novel-grid">{visible.map((n,index)=><article className="novel-card" key={n.id}><button className="novel-open" data-testid={'novel-open-'+n.id} onClick={()=>select(n)}><div className={'book-cover cover-'+(index%3)}><span className="cover-kicker">ВАША ИСТОРИЯ</span><h2>{n.title}</h2><span className="cover-glyph">✦</span><span className="cover-label">NOVEL STUDIO</span></div><div className="book-detail"><strong>{n.title}</strong><p>{n.genre||'Жанр ещё не определён'}</p><span>Открыть рукопись <b>↗</b></span></div></button><button className="danger-button" data-testid={'novel-delete-'+n.id} aria-label={'Удалить роман «'+n.title+'»'} disabled={offline||busy} onClick={()=>void prepareTrash(n)}>Удалить роман</button></article>)}</div>}
 <div className="library-note"><span>◇</span><p>Идея остаётся идеей, пока вы её не утвердите. ИИ предлагает — автор решает.</p></div>
 {confirmation&&<Modal title={confirmation.blocked.length?'Сначала сохраните правки':'Удалить роман?'} onClose={()=>{if(!busy)setConfirmation(null);}}><h3>{confirmation.novel.title}</h3>{confirmation.blocked.length?<div data-testid="novel-trash-blocked"><p>В этом браузере есть несинхронизированные правки. Откройте главы и сохраните их на сервере или разрешите конфликт версий, затем повторите удаление.</p><ul className="trash-list">{confirmation.blocked.map(item=><li key={item.draft.chapterId}><span>{item.title}{item.draft.state==='conflict'?' · конфликт версий':''}</span><button data-testid={'novel-blocked-chapter-'+item.draft.chapterId} disabled={busy} onClick={()=>void openLocalChapter(item)}>Открыть главу</button></li>)}</ul></div>:<><p>Роман, его главы и записи будут перемещены в корзину. Восстановить их можно в библиотеке.</p><p className="small muted">Активные задачи ИИ будут отменены. При восстановлении они не запускаются заново.</p></>}{offline&&<p className="error">Нет связи с сервером. Удаление недоступно.</p>}<div className="actions">{!confirmation.blocked.length&&<button className="danger-button" data-testid="novel-trash-confirm" disabled={offline||busy} onClick={()=>void moveToTrash()}>{busy?'Перемещаем…':'Переместить в корзину'}</button>}<button data-testid="novel-trash-cancel" disabled={busy} onClick={()=>setConfirmation(null)}>Отмена</button></div></Modal>}
 </>;
}

import {useCallback,useEffect,useRef,useState} from 'react';
import {api,errorMessage,post,setCsrfToken} from './api';
import {getCache,putCache} from './drafts';
import type {Novel,Page,Session,User} from './types';
import {Empty} from './components';
import {Editor} from './Editor';
import {Bible} from './Bible';
import {Agents} from './Agents';
import {Settings} from './Settings';
import {applyPreferences,getPreferences} from './preferences';
type Screen='library'|'editor'|'bible'|'agents'|'settings';
const screens:Array<[Screen,string,string]>=[['library','Библиотека','▦'],['editor','Главы','≡'],['bible','Библия романа','◇'],['agents','ИИ-агенты','✦'],['settings','Настройки','⚙']];
const remember=(user:User)=>localStorage.setItem('novel-studio-last-user',JSON.stringify(user));
const remembered=():User|null=>{try{return JSON.parse(localStorage.getItem('novel-studio-last-user')??'null') as User|null;}catch{return null;}};
export function App(){
 const [user,setUser]=useState<User|null>(null),[ready,setReady]=useState(false),[offline,setOffline]=useState(false),[message,setMessage]=useState('');
 const [novels,setNovels]=useState<Novel[]>([]),[novel,setNovel]=useState<Novel|null>(null),[screen,setScreen]=useState<Screen>('library'),[menu,setMenu]=useState(false);
 const ownerRef=useRef<string|null>(null);ownerRef.current=user?.id??null;
 const notify=useCallback((text:string)=>setMessage(text),[]);
 useEffect(()=>{
  let cancelled=false;
  api<Session>('/session').then(s=>{if(cancelled)return;setCsrfToken(s.csrf_token);setUser(s.user);if(s.user)remember(s.user);}).catch(error=>{if(cancelled)return;setOffline(true);setUser(remembered());notify(errorMessage(error));}).finally(()=>{if(!cancelled)setReady(true);});
  const online=()=>{void api<Session>('/session').then(s=>{setCsrfToken(s.csrf_token);setOffline(false);if(!s.user||s.user.id!==remembered()?.id){notify('Сессия истекла. Войдите снова: локальные черновики сохранены.');return;}window.dispatchEvent(new Event('novel-session-ready'));}).catch(error=>{setOffline(true);notify(errorMessage(error));});},offlineEvent=()=>setOffline(true);
  window.addEventListener('online',online);window.addEventListener('offline',offlineEvent);
  return()=>{cancelled=true;window.removeEventListener('online',online);window.removeEventListener('offline',offlineEvent);};
 },[notify]);
 const loadLibrary=useCallback(async(owner:User)=>{try{const page=await api<Page<Novel>>('/novels');if(ownerRef.current!==owner.id)return;setNovels(page.items);await putCache(owner.id,'novels',page.items);setOffline(false);}catch(error){const cached=await getCache<Novel[]>(owner.id,'novels')??[];if(ownerRef.current!==owner.id)return;setNovels(cached);notify(errorMessage(error));}},[notify]);
 useEffect(()=>{if(user){setNovels([]);applyPreferences(getPreferences(user.id));setNovel(null);setScreen('library');void loadLibrary(user);}},[user,loadLibrary]);
 const selectNovel=(selected:Novel)=>{setNovel(selected);setScreen('editor');setMenu(false);};
 const updateNovel=(updated:Novel)=>{setNovel(updated);setNovels(current=>current.map(n=>n.id===updated.id?updated:n));if(user)void putCache(user.id,'novel:'+updated.id,updated).catch(e=>notify(errorMessage(e)));};
 const signOut=async()=>{try{await post('/auth/logout');ownerRef.current=null;setNovels([]);setUser(null);setNovel(null);localStorage.removeItem('novel-studio-last-user');}catch(error){notify(errorMessage(error));}};
 if(!ready)return <div className="splash"><div className="brand-mark">N</div><p>Открываем студию…</p></div>;
 if(!user)return <Login message={message} onLogin={session=>{ownerRef.current=session.user?.id??null;setNovels([]);setCsrfToken(session.csrf_token);setUser(session.user);if(session.user)remember(session.user);setMessage('');}} />;
 return <div className="studio"><aside className={'sidebar '+(menu?'is-open':'')}><a className="brand" href="#" onClick={e=>{e.preventDefault();setScreen('library');setMenu(false);}}><span className="brand-mark">N</span><span>Novel<span className="brand-sub">STUDIO</span></span></a><p className="sidebar-caption">ПРОСТРАНСТВО АВТОРА</p>
 <nav aria-label="Главное меню">{screens.map(([key,name,icon])=><button key={key} className={'nav-link '+(screen===key?'active':'')} disabled={!novel&&key!=='library'&&key!=='settings'} onClick={()=>{setScreen(key);setMenu(false);}}><span>{icon}</span>{name}</button>)}</nav>
 {novel&&<div className="sidebar-project"><small>ТЕКУЩИЙ РОМАН</small><strong>{novel.title}</strong><span>{novel.genre||'Жанр не указан'}</span><span>Канон · версия {novel.canon_version}</span></div>}
 <div className="sidebar-bottom"><span className={'connection '+(offline?'offline':'')}>{offline?'Работа на устройстве':'Сервер доступен'}</span><small title={user.email}>{user.email}</small><button className="ghost" onClick={()=>void signOut()}>Выйти из аккаунта</button></div></aside>
 <div className="workspace"><header className="topbar"><button className="menu-button ghost" onClick={()=>setMenu(!menu)} aria-label="Открыть меню">☰</button><div className="breadcrumb">Моя студия<span>/</span><strong>{screen==='library'?'Библиотека':novel?.title??'Настройки'}</strong></div><span className="topbar-note">Творческие решения принадлежат вам</span></header>
 {message&&<div className="notice" role="status"><span>{message}</span><button className="ghost" onClick={()=>setMessage('')} aria-label="Закрыть сообщение">×</button>{message.includes('Сессия')&&<button onClick={()=>setUser(null)}>Войти снова</button>}</div>}
 <main className={screen==='editor'?'main editor-main':'main'}>{screen==='library'&&<Library user={user} novels={novels} select={selectNovel} notify={notify} refresh={()=>loadLibrary(user)} />}{screen==='editor'&&novel&&<Editor key={novel.id} user={user} novel={novel} notify={notify} />}{screen==='bible'&&novel&&<Bible key={novel.id} user={user} novel={novel} onNovel={updateNovel} notify={notify} />}{screen==='agents'&&novel&&<Agents novel={novel} notify={notify} />}{screen==='settings'&&<Settings user={user} novel={novel} onNovel={updateNovel} notify={notify} />}</main>
 <nav className="mobile-nav" aria-label="Быстрый доступ">{screens.filter(([s])=>s!=='settings').map(([key,name,icon])=><button key={key} className={screen===key?'active':''} disabled={!novel&&key!=='library'} onClick={()=>setScreen(key)}><span>{icon}</span><small>{name==='Библия романа'?'Канон':name}</small></button>)}</nav></div></div>;
}
function Login({message,onLogin}:{message:string;onLogin:(s:Session)=>void}){
 const [email,setEmail]=useState(''),[password,setPassword]=useState(''),[error,setError]=useState(message),[busy,setBusy]=useState(false);
 const submit=async(event:React.FormEvent)=>{event.preventDefault();setBusy(true);setError('');try{const session=await api<Session>('/session');setCsrfToken(session.csrf_token);onLogin(await post<Session>('/auth/login',{email,password}));}catch(e){setError(errorMessage(e));}finally{setBusy(false);}};
 return <div className="login-page"><section className="login-story"><span className="eyebrow">NOVEL STUDIO</span><h1>У каждой истории<br />есть свой автор.</h1><p>Ваши романы, идеи и решения.<br />Одна спокойная среда для большой истории.</p><div className="story-rule" /></section><section className="login-card"><div className="brand-mark">N</div><h2>Ваша писательская студия</h2><p className="muted">Войдите, чтобы открыть библиотеку.</p><form onSubmit={e=>void submit(e)}><label>Электронная почта<input type="email" data-testid="login-email" autoComplete="username" required value={email} onChange={e=>setEmail(e.target.value)} /></label><label>Пароль<input type="password" data-testid="login-password" autoComplete="current-password" required value={password} onChange={e=>setPassword(e.target.value)} /></label>{error&&<p className="error" role="alert">{error}</p>}<button className="primary" data-testid="login-submit" disabled={busy}>{busy?'Входим…':'Открыть студию'}</button></form><p className="small muted">Учётная запись создаётся при первом запуске сервера. Инструкция — в README проекта.</p></section></div>;
}
function Library({user,novels,select,notify,refresh}:{user:User;novels:Novel[];select:(n:Novel)=>void;notify:(s:string)=>void;refresh:()=>Promise<void>}){
 const [creating,setCreating]=useState(false),[busy,setBusy]=useState(false),[title,setTitle]=useState(''),[genre,setGenre]=useState(''),[concept,setConcept]=useState('');
 const create=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);try{const novel=await post<Novel>('/novels',{title:title.trim(),genre,concept});await putCache(user.id,'novel:'+novel.id,novel);await refresh();select(novel);}catch(error){notify(errorMessage(error));}finally{setBusy(false);}};
 return <><header className="page-heading"><div><span className="eyebrow">ВАШИ ИСТОРИИ</span><h1>Моя библиотека</h1><p className="muted">У каждого романа — собственный мир, канон и голоса героев.</p></div><button className="primary" onClick={()=>setCreating(!creating)}>＋ Новый роман</button></header>
 {creating&&<form className="panel creation-form" onSubmit={e=>void create(e)}><h2>С чего начнётся история?</h2><div className="form-row"><label>Название<input required data-testid="novel-create-title" maxLength={200} value={title} onChange={e=>setTitle(e.target.value)} placeholder="Название вашего романа" /></label><label>Жанр<input maxLength={100} value={genre} onChange={e=>setGenre(e.target.value)} placeholder="Например, современная романтика" /></label></div><label>Идея романа<textarea rows={4} value={concept} onChange={e=>setConcept(e.target.value)} placeholder="Расскажите свободными словами о героях и основном конфликте." /></label><div className="actions"><button className="primary" data-testid="novel-create-submit" disabled={busy}>{busy?'Создаём…':'Создать роман'}</button><button type="button" className="ghost" onClick={()=>setCreating(false)}>Отмена</button></div></form>}
 {novels.length===0?<Empty title="Место для вашей первой истории" text="Создайте роман. Затем добавьте идеи, героев и утверждённые факты — в удобном для вас порядке." />:<div className="novel-grid">{novels.map((n,index)=><button className="novel-card" data-testid={'novel-open-'+n.id} key={n.id} onClick={()=>select(n)}><div className={'book-cover cover-'+(index%3)}><span className="cover-kicker">ВАША ИСТОРИЯ</span><h2>{n.title}</h2><span className="cover-glyph">✦</span><span className="cover-label">NOVEL STUDIO</span></div><div className="book-detail"><strong>{n.title}</strong><p>{n.genre||'Жанр ещё не определён'}</p><span>Открыть рукопись <b>↗</b></span></div></button>)}</div>}
 <div className="library-note"><span>◇</span><p>Идея остаётся идеей, пока вы её не утвердите. ИИ предлагает — автор решает.</p></div></>;
}

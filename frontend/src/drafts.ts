import {ApiError} from './api';
import type {Chapter,Revision} from './types';
export type DraftState='synced'|'local'|'saving'|'conflict';
export interface PendingMutation{mutation_id:string;base_revision_id:string;text:string}
export interface Draft{owner:string;novelId:string;chapterId:string;text:string;baseRevisionId:string;syncedText:string;state:DraftState;pending?:PendingMutation;serverRevision?:Revision;modifiedAt:string}
const DB_NAME='novel-studio-v1';
let database:Promise<IDBDatabase>|undefined;
const inFlight=new Map<string,Promise<Draft>>();
const localKey=(owner:string,id:string)=>JSON.stringify([owner,id]);
function db():Promise<IDBDatabase>{
 if(!database)database=new Promise((resolve,reject)=>{
  const request=indexedDB.open(DB_NAME,1);
  request.onupgradeneeded=()=>{request.result.createObjectStore('drafts');request.result.createObjectStore('cache');};
  request.onerror=()=>reject(request.error);request.onsuccess=()=>resolve(request.result);
  request.onblocked=()=>reject(new Error('Закройте другие вкладки Novel Studio и повторите.'));
 });
 return database;
}
async function read<T>(store:string,key:string):Promise<T|undefined>{
 const database=await db();return new Promise((resolve,reject)=>{
  const tx=database.transaction(store,'readonly'),request=tx.objectStore(store).get(key);
  request.onsuccess=()=>resolve(request.result as T|undefined);request.onerror=()=>reject(request.error);
 });
}
async function write<T>(store:string,key:string,value:T):Promise<void>{
 const database=await db();return new Promise((resolve,reject)=>{
  const tx=database.transaction(store,'readwrite');tx.objectStore(store).put(value,key);
  tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
 });
}
async function change(owner:string,chapterId:string,update:(value:Draft|undefined)=>Draft):Promise<Draft>{
 const database=await db();return new Promise((resolve,reject)=>{
  const tx=database.transaction('drafts','readwrite'),store=tx.objectStore('drafts'),request=store.get(localKey(owner,chapterId));let next:Draft;
  request.onsuccess=()=>{try{next=update(request.result as Draft|undefined);store.put(next,localKey(owner,chapterId));}catch(error){reject(error);tx.abort();}};
  tx.oncomplete=()=>resolve(next!);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error??new Error('Локальное сохранение прервано.'));
 });
}
export const getDraft=(owner:string,chapterId:string)=>read<Draft>('drafts',localKey(owner,chapterId));
export const putCache=<T>(owner:string,key:string,value:T)=>write('cache',localKey(owner,key),value);
export const getCache=<T>(owner:string,key:string)=>read<T>('cache',localKey(owner,key));
export async function initialiseDraft(owner:string,chapter:Chapter,revision:Revision):Promise<Draft>{
 await putCache(owner,'chapter:'+chapter.id,{...chapter,current_revision:revision});
 return change(owner,chapter.id,current=>{
  if(current&&(current.pending||current.text!==current.syncedText)){
   if(!current.pending&&current.baseRevisionId!==revision.id)return {...current,state:'conflict',serverRevision:revision};
   return current;
  }
  return {owner,novelId:chapter.novel_id,chapterId:chapter.id,text:revision.text,syncedText:revision.text,baseRevisionId:revision.id,state:'synced',modifiedAt:new Date().toISOString()};
 });
}
export async function editDraft(owner:string,chapterId:string,text:string):Promise<Draft>{
 return change(owner,chapterId,current=>{
  if(!current)throw new Error('Глава ещё не загружена.');
  return {...current,text,state:current.state==='conflict'?'conflict':'local',modifiedAt:new Date().toISOString()};
 });
}
export async function markConflict(owner:string,chapterId:string,revision:Revision):Promise<Draft>{
 return change(owner,chapterId,current=>{
  if(!current)throw new Error('Локальный черновик не найден.');return {...current,state:'conflict',serverRevision:revision};
 });
}
/** Only after explicit author comparison; never silently resolve 409. */
export async function resolveConflict(owner:string,chapterId:string,text:string,revision:Revision):Promise<Draft>{
 const current=await getDraft(owner,chapterId);if(!current)throw new Error('Локальный черновик не найден.');
 await putCache(owner,'backup:'+chapterId+':'+crypto.randomUUID(),current);
 return change(owner,chapterId,latest=>{
  if(!latest)throw new Error('Локальный черновик не найден.');
  return {...latest,text,baseRevisionId:revision.id,syncedText:revision.text,pending:undefined,serverRevision:undefined,state:text===revision.text?'synced':'local',modifiedAt:new Date().toISOString()};
 });
}
export async function listBackups(owner:string,chapterId:string):Promise<Draft[]>{
 const database=await db();return new Promise((resolve,reject)=>{
  const tx=database.transaction('cache','readonly'),cursor=tx.objectStore('cache').openCursor(),results:Draft[]=[];
  cursor.onsuccess=()=>{const row=cursor.result;if(!row)return;const [recordOwner,key]=JSON.parse(String(row.key)) as[string,string];if(recordOwner===owner&&key.startsWith('backup:'+chapterId+':'))results.push(row.value as Draft);row.continue();};
  tx.oncomplete=()=>resolve(results.sort((a,b)=>b.modifiedAt.localeCompare(a.modifiedAt)));tx.onerror=()=>reject(tx.error);
 });
}
export type SaveTransport=(chapterId:string,mutation:PendingMutation)=>Promise<Revision>;
/** One request per chapter; exact persisted payload survives lost responses/reload. */
export function syncDraft(owner:string,chapterId:string,transport:SaveTransport):Promise<Draft>{
 const key=localKey(owner,chapterId),existing=inFlight.get(key);if(existing)return existing;
 const task=(async()=>{
  for(;;){
   const snapshot=await change(owner,chapterId,current=>{
    if(!current)throw new Error('Глава ещё не загружена.');
    if(current.state==='conflict')throw new ApiError(409,{},'Сначала разрешите конфликт версий.');
    if(!current.pending&&current.text===current.syncedText)return {...current,state:'synced'};
    return {...current,state:'saving',pending:current.pending??{mutation_id:crypto.randomUUID(),base_revision_id:current.baseRevisionId,text:current.text}};
   });
   if(!snapshot.pending)return snapshot;
   try{
    const saved=await transport(chapterId,snapshot.pending);
    const result=await change(owner,chapterId,latest=>{
     if(!latest)throw new Error('Локальный черновик не найден.');
     if(latest.pending?.mutation_id!==snapshot.pending!.mutation_id)return latest;
     return {...latest,baseRevisionId:saved.id,syncedText:snapshot.pending!.text,pending:undefined,state:latest.text===snapshot.pending!.text?'synced':'local'};
    });
    await putCache(owner,'revision:'+chapterId,saved);if(result.state==='synced')return result;
   }catch(error){
    await change(owner,chapterId,latest=>{if(!latest)throw error;return {...latest,state:error instanceof ApiError&&error.status===409?'conflict':'local'};});throw error;
   }
  }
 })();
 inFlight.set(key,task);void task.finally(()=>{if(inFlight.get(key)===task)inFlight.delete(key);}).catch(()=>{});return task;
}

/** Keep the chapter lock until all requests already started for it settle. */
export async function waitForDraftSync(owner:string,chapterId:string):Promise<void>{
 await inFlight.get(localKey(owner,chapterId))?.catch(()=>undefined);
}

/** Pending mutations include responses lost during synchronization. */
export async function listUnsyncedNovelDrafts(owner:string,novelId:string):Promise<Draft[]>{
 const database=await db();return new Promise((resolve,reject)=>{
  const tx=database.transaction('drafts','readonly'),request=tx.objectStore('drafts').openCursor(),results:Draft[]=[];
  request.onsuccess=()=>{const cursor=request.result;if(!cursor)return;const value=cursor.value as Draft;
   if(value.owner===owner&&value.novelId===novelId&&(value.pending||value.text!==value.syncedText||value.state==='conflict'))results.push(value);
   cursor.continue();
  };
  request.onerror=()=>reject(request.error);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error??new Error('Не удалось проверить локальные черновики.'));
  tx.oncomplete=()=>resolve(results.sort((a,b)=>b.modifiedAt.localeCompare(a.modifiedAt)));
 });
}

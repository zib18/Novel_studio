import 'fake-indexeddb/auto';
import {describe,expect,it} from 'vitest';
import {ApiError} from './api';
import {listUnsyncedNovelDrafts,editDraft,getCache,getDraft,initialiseDraft,listBackups,putCache,resolveConflict,syncDraft,waitForDraftSync,type PendingMutation} from './drafts';
import type {Chapter,Revision} from './types';
const fixture=async()=>{const owner=crypto.randomUUID(),id=crypto.randomUUID(),chapter:Chapter={id,novel_id:crypto.randomUUID(),number:1,title:'Глава'},revision:Revision={id:crypto.randomUUID(),chapter_id:id,text:'Исходный текст'};await initialiseDraft(owner,chapter,revision);return {owner,chapter,revision};};
function deferred<T>(){let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};}
describe('постоянные локальные черновики',()=>{
 it('разделяет тексты и метаданные разных владельцев',async()=>{const {owner,chapter,revision}=await fixture(),otherOwner=crypto.randomUUID();await initialiseDraft(otherOwner,chapter,{...revision,text:'Другой автор'});await editDraft(owner,chapter.id,'Мой черновик');await putCache(owner,'bible',['секрет']);expect((await getDraft(owner,chapter.id))?.text).toBe('Мой черновик');expect((await getDraft(otherOwner,chapter.id))?.text).toBe('Другой автор');expect(await getCache(otherOwner,'bible')).toBeUndefined();});
 it('сохраняет несинхронизированный текст после ошибки сети',async()=>{const {owner,chapter}=await fixture();await editDraft(owner,chapter.id,'Без интернета');await expect(syncDraft(owner,chapter.id,async()=>{throw new ApiError(0,{},'offline');})).rejects.toThrow('offline');const loaded=await getDraft(owner,chapter.id);expect(loaded?.text).toBe('Без интернета');expect(loaded?.pending?.text).toBe('Без интернета');expect(loaded?.state).toBe('local');});
 it('повторяет точный UUID и payload после потерянного ответа, затем сохраняет новые правки',async()=>{const {owner,chapter,revision}=await fixture();await editDraft(owner,chapter.id,'Первый вариант');let lost!:PendingMutation;await expect(syncDraft(owner,chapter.id,async(_id,mutation)=>{lost=mutation;throw new ApiError(0,{},'response lost');})).rejects.toThrow('response lost');await editDraft(owner,chapter.id,'Поздняя ручная правка');const requests:PendingMutation[]=[];let next=0;const result=await syncDraft(owner,chapter.id,async(_id,mutation)=>{requests.push({...mutation});return {id:'retry-'+ ++next,text:mutation.text};});expect(requests[0]).toEqual(lost);expect(requests[0].base_revision_id).toBe(revision.id);expect(requests[1].text).toBe('Поздняя ручная правка');expect(requests[1].base_revision_id).toBe('retry-1');expect(result.text).toBe('Поздняя ручная правка');expect(result.state).toBe('synced');});
 it('поздний ответ не затирает правку во время запроса',async()=>{const {owner,chapter}=await fixture();await editDraft(owner,chapter.id,'Отправленный текст');const entered=deferred<void>(),firstResponse=deferred<Revision>(),requests:PendingMutation[]=[];const task=syncDraft(owner,chapter.id,async(_id,mutation)=>{requests.push({...mutation});if(requests.length===1){entered.resolve();return firstResponse.promise;}return {id:'newer',text:mutation.text};});await entered.promise;await editDraft(owner,chapter.id,'Текст, набранный позднее');firstResponse.resolve({id:'older',text:'Отправленный текст'});const result=await task;expect(result.text).toBe('Текст, набранный позднее');expect(result.baseRevisionId).toBe('newer');expect(requests).toHaveLength(2);expect(requests[1].base_revision_id).toBe('older');});
 it('повторный запуск autosave не создаёт параллельный запрос для главы',async()=>{const {owner,chapter}=await fixture();await editDraft(owner,chapter.id,'Сохранить');const entered=deferred<void>(),response=deferred<Revision>();let calls=0;const transport=async()=>{calls++;entered.resolve();return response.promise;};const first=syncDraft(owner,chapter.id,transport);await entered.promise;const second=syncDraft(owner,chapter.id,transport);expect(second).toBe(first);response.resolve({id:'one',text:'Сохранить'});await Promise.all([first,second]);expect(calls).toBe(1);});
 it('при 409 оставляет обе версии и ждёт решения автора',async()=>{const {owner,chapter,revision}=await fixture();await editDraft(owner,chapter.id,'Моя редакция');await expect(syncDraft(owner,chapter.id,async()=>{throw new ApiError(409,{},'version conflict');})).rejects.toThrow('version conflict');expect((await getDraft(owner,chapter.id))?.text).toBe('Моя редакция');expect((await getDraft(owner,chapter.id))?.state).toBe('conflict');const server={...revision,id:'server-latest',text:'Правка на другом устройстве'},resolved=await resolveConflict(owner,chapter.id,'Объединённый автором текст',server);expect(resolved.baseRevisionId).toBe('server-latest');expect(resolved.text).toBe('Объединённый автором текст');expect((await listBackups(owner,chapter.id))[0].text).toBe('Моя редакция');});
 it('загрузка серверной главы не заменяет грязный локальный черновик',async()=>{const {owner,chapter,revision}=await fixture();await editDraft(owner,chapter.id,'Сохранённое на устройстве');const current=await initialiseDraft(owner,chapter,{...revision,id:'changed',text:'Серверная версия'});expect(current.text).toBe('Сохранённое на устройстве');expect(current.serverRevision?.text).toBe('Серверная версия');expect(current.state).toBe('conflict');});
 it('грязный черновик остаётся при серверном GET после потерянного ответа',async()=>{const {owner,chapter,revision}=await fixture();await editDraft(owner,chapter.id,'Первая правка');await expect(syncDraft(owner,chapter.id,async()=>{throw new ApiError(0,{},'lost');})).rejects.toThrow('lost');await editDraft(owner,chapter.id,'Более новая правка');const current=await initialiseDraft(owner,chapter,{...revision,id:'already-saved',text:'Первая правка'});expect(current.text).toBe('Более новая правка');expect(current.pending?.text).toBe('Первая правка');});
});

it('завершение синхронизации удерживает главу до подтверждения позднего запроса',async()=>{
 const {owner,chapter}=await fixture();await editDraft(owner,chapter.id,'Отправленный текст');
 const entered=deferred<void>(),response=deferred<Revision>();
 const syncing=syncDraft(owner,chapter.id,async()=>{entered.resolve();return response.promise;});
 await entered.promise;let released=false;
 const waiting=waitForDraftSync(owner,chapter.id).then(()=>{released=true;});
 await Promise.resolve();expect(released).toBe(false);
 response.resolve({id:'saved',text:'Отправленный текст'});
 await Promise.all([syncing,waiting]);expect(released).toBe(true);
});

it('проверка удаления разделяет авторов и романы, сохраняя локальные правки',async()=>{
 const {owner,chapter,revision}=await fixture(),otherOwner=crypto.randomUUID();
 const sibling:Chapter={...chapter,id:crypto.randomUUID(),novel_id:crypto.randomUUID()};
 await initialiseDraft(owner,sibling,{...revision,id:crypto.randomUUID(),chapter_id:sibling.id});
 await initialiseDraft(otherOwner,chapter,revision);
 expect(await listUnsyncedNovelDrafts(owner,chapter.novel_id)).toEqual([]);
 await editDraft(owner,chapter.id,'Моя несохранённая глава');
 await editDraft(owner,sibling.id,'Другой роман');
 await editDraft(otherOwner,chapter.id,'Другой автор');
 const blocked=await listUnsyncedNovelDrafts(owner,chapter.novel_id);
 expect(blocked.map(d=>d.chapterId)).toEqual([chapter.id]);
 expect((await getDraft(owner,chapter.id))?.text).toBe('Моя несохранённая глава');
 expect((await listUnsyncedNovelDrafts(owner,sibling.novel_id))[0].text).toBe('Другой роман');
});
it('потерянное подтверждение блокирует удаление после возврата к исходному тексту',async()=>{
 const {owner,chapter,revision}=await fixture();
 await editDraft(owner,chapter.id,'Возможно, уже сохранено сервером');
 await expect(syncDraft(owner,chapter.id,async()=>{throw new ApiError(0,{},'lost response');})).rejects.toThrow('lost response');
 const before=await getDraft(owner,chapter.id);
 await editDraft(owner,chapter.id,revision.text);
 const blocked=await listUnsyncedNovelDrafts(owner,chapter.novel_id);
 expect(blocked).toHaveLength(1);
 expect(blocked[0].pending?.mutation_id).toBe(before?.pending?.mutation_id);
 expect(blocked[0].text).toBe(revision.text);
});
it('неразрешённый конфликт блокирует удаление и сохраняет локальную версию',async()=>{
 const {owner,chapter,revision}=await fixture();
 await editDraft(owner,chapter.id,'Моя версия до конфликта');
 await expect(syncDraft(owner,chapter.id,async()=>{throw new ApiError(409,{},'conflict');})).rejects.toThrow('conflict');
 await editDraft(owner,chapter.id,revision.text);
 const blocked=await listUnsyncedNovelDrafts(owner,chapter.novel_id);
 expect(blocked).toHaveLength(1);
 expect(blocked[0].state).toBe('conflict');
 expect((await getDraft(owner,chapter.id))?.text).toBe(revision.text);
});

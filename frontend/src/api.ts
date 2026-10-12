let csrfToken='';
export function setCsrfToken(token:string){csrfToken=token;}
export class ApiError extends Error{constructor(public status:number,public detail:Record<string,unknown>,message:string){super(message);this.name='ApiError';}}
export async function api<T>(path:string,options:RequestInit={}):Promise<T>{
 const method=options.method??'GET',headers=new Headers(options.headers);
 if(options.body)headers.set('Content-Type','application/json');
 if(!['GET','HEAD','OPTIONS'].includes(method))headers.set('X-CSRF-Token',csrfToken);
 let response:Response;
 try{response=await fetch('/api/v1'+path,{...options,signal:options.signal??AbortSignal.timeout(30000),headers,credentials:'include'});}catch{throw new ApiError(0,{},'Нет связи с сервером. Текст остаётся на этом устройстве.');}
 if(!response.ok){
  let payload:unknown;try{payload=await response.json();}catch{payload=null;}
  const raw=payload&&typeof payload==='object'&&'detail'in payload?(payload as{detail:unknown}).detail:payload;
  const detail=raw&&typeof raw==='object'&&!Array.isArray(raw)?raw as Record<string,unknown>:{};
  const message=typeof raw==='string'?raw:typeof detail.message==='string'?detail.message:response.status===401?'Сессия истекла. Войдите снова: локальные черновики сохранены.':response.status===409?'Версия на сервере изменилась. Сравните тексты перед сохранением.':response.status===422?'Проверьте заполненные поля.':'Сервер вернул ошибку '+response.status+'.';
  throw new ApiError(response.status,detail,message);
 }
 if(response.status===204)return undefined as T;return response.json() as Promise<T>;
}
export function post<T>(path:string,body:unknown={},headers?:HeadersInit):Promise<T>{return api<T>(path,{method:'POST',body:JSON.stringify(body),headers});}
export function errorMessage(error:unknown):string{return error instanceof Error?error.message:String(error);}

import {readdir,readFile,writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
const root=new URL('../dist/',import.meta.url);
async function files(directory,prefix=''){const result=[];for(const entry of await readdir(directory,{withFileTypes:true})){const path=prefix+entry.name;if(entry.isDirectory())result.push(...await files(new URL(entry.name+'/',directory),path+'/'));else if(entry.name!=='sw.js')result.push('/'+path);}return result;}
const assets=(await files(root)).sort(),digest=createHash('sha256');for(const asset of assets)digest.update(await readFile(new URL('.'+asset,root)));
const version=digest.digest('hex').slice(0,16),template=await readFile(new URL('./sw.template.js',import.meta.url),'utf8');
await writeFile(new URL('sw.js',root),template.replace('__CACHE_VERSION__',version).replace('__ASSET_LIST__',JSON.stringify(assets)));

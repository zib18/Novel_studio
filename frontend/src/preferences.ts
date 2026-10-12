export interface Preferences{theme:'light'|'dark';fontSize:number}
const key=(owner:string)=>'novel-studio-preferences:'+owner;
export function getPreferences(owner:string):Preferences{try{const stored=JSON.parse(localStorage.getItem(key(owner))??'{}') as Partial<Preferences>;return {theme:stored.theme==='dark'?'dark':'light',fontSize:Math.max(16,Math.min(28,Number(stored.fontSize)||20))};}catch{return {theme:'light',fontSize:20};}}
export function applyPreferences(preferences:Preferences){document.documentElement.dataset.theme=preferences.theme;document.documentElement.style.setProperty('--prose-size',preferences.fontSize+'px');}
export function savePreferences(owner:string,preferences:Preferences){localStorage.setItem(key(owner),JSON.stringify(preferences));applyPreferences(preferences);}

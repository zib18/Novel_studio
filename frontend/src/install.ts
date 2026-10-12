export interface InstallPrompt extends Event{prompt():Promise<void>;userChoice:Promise<{outcome:'accepted'|'dismissed';platform:string}>}
let pending:InstallPrompt|null=null;
window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();pending=event as InstallPrompt;window.dispatchEvent(new Event('novel-install-ready'));});
window.addEventListener('appinstalled',()=>{pending=null;window.dispatchEvent(new Event('novel-install-ready'));});
export const getInstallPrompt=()=>pending;
export const clearInstallPrompt=()=>{pending=null;window.dispatchEvent(new Event('novel-install-ready'));};

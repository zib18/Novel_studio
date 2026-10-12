/** Optional Android wrapper for deployed HTTPS server. See docs/android.md. */
const origin=process.env.NOVEL_STUDIO_ORIGIN;
if(!origin)throw new Error('Set NOVEL_STUDIO_ORIGIN to the HTTPS Novel Studio deployment.');
const url=new URL(origin);
if(url.protocol!=='https:'||url.username||url.password||url.pathname!=='/'||url.search||url.hash)throw new Error('NOVEL_STUDIO_ORIGIN must be an HTTPS origin, without path, credentials or query.');
export default {appId:'studio.novel.personal',appName:'Novel Studio',webDir:'frontend/dist',server:{url:url.origin,cleartext:false,allowNavigation:[url.hostname]},android:{allowMixedContent:false}};

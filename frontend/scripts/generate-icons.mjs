import {mkdir,writeFile} from 'node:fs/promises';
import {deflateSync} from 'node:zlib';
const directory=new URL('../public/',import.meta.url);await mkdir(directory,{recursive:true});
function crc32(bytes){let crc=0xffffffff;for(const byte of bytes){crc^=byte;for(let i=0;i<8;i++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return (crc^0xffffffff)>>>0;}
function chunk(tag,data){const type=Buffer.from(tag,'ascii'),length=Buffer.alloc(4),crc=Buffer.alloc(4);length.writeUInt32BE(data.length);crc.writeUInt32BE(crc32(Buffer.concat([type,data])));return Buffer.concat([length,type,data,crc]);}
function distance(x,y,x1,y1,x2,y2){const dx=x2-x1,dy=y2-y1,t=Math.max(0,Math.min(1,((x-x1)*dx+(y-y1)*dy)/(dx*dx+dy*dy)));return Math.hypot(x-x1-t*dx,y-y1-t*dy);}
for(const size of [192,512]){
 const pixels=Buffer.alloc(size*(size*4+1));
 for(let y=0;y<size;y++){pixels[y*(size*4+1)]=0;for(let x=0;x<size;x++){
  const a=(x+.5)/size,b=(y+.5)/size,mark=Math.min(distance(a,b,.30,.73,.30,.27),distance(a,b,.30,.27,.70,.73),distance(a,b,.70,.73,.70,.27))<.035||distance(a,b,.29,.82,.71,.82)<.01;
  const offset=y*(size*4+1)+1+x*4,color=mark?[173,101,66,255]:[246,244,240,255];for(let channel=0;channel<4;channel++)pixels[offset+channel]=color[channel];
 }}
 const header=Buffer.alloc(13);header.writeUInt32BE(size,0);header.writeUInt32BE(size,4);header[8]=8;header[9]=6;
 const png=Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),chunk('IDAT',deflateSync(pixels)),chunk('IEND',Buffer.alloc(0))]);await writeFile(new URL('icon-'+size+'.png',directory),png);
}

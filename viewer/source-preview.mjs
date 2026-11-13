// Loopback-only synthetic preview and integration-test server. No real data,
// network calls, persistent state or production credentials are loaded.
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {resolve,extname} from 'node:path';
import {Readable} from 'node:stream';
import {createHash} from 'node:crypto';
import {fixture} from './source-fixture.mjs';
import worker from './worker.mjs';
import {canonical} from './source-schema.mjs';

const closers=[],{call,env,revision,upload,objects}=fixture({after(fn){closers.push(fn);}});
objects.set('viewer/index.json',Buffer.from('{"projects":[{"id":"demo","name":"Studio example"}]}'));
objects.set('viewer/projects/demo.json',Buffer.from(JSON.stringify({id:'demo',name:'Studio example',snapshot_id:'synthetic-preview',videos:[{id:'production:film',title:'A small documentary',files:[],metadata:{}}],raw:[],assets:[],posts:[],ads:[],relationships:[]})));
await upload();await call('publish',{revision,expected_revision:0,operation_id:'preview-first'});
const next=structuredClone(revision);next.label='Second cut';next.manifest.recipe.command=['python','scene.py'];next.manifest.dependencies.push({name:'display font',status:'missing'});next.revision_id=createHash('sha256').update(canonical(next.manifest)).digest('hex');
await call('publish',{revision:next,expected_revision:1,operation_id:'preview-second'});
env.ASSETS={async fetch(request){const root=fileURLToPath(new URL('./public/',import.meta.url)),path=resolve(root,'.'+new URL(request.url).pathname.replace(/\/$/,'/index.html'));if(!path.startsWith(root))return new Response('Not found',{status:404});try{const body=await readFile(path);return new Response(body,{headers:{'Content-Type':({'.html':'text/html','.mjs':'text/javascript','.js':'text/javascript','.css':'text/css'})[extname(path)]||'application/octet-stream'}});}catch{return new Response('Not found',{status:404});}}};
const server=http.createServer(async(req,res)=>{
  try {
    const headers=new Headers(req.headers);
    // Preview bypass is confined to this loopback test harness, never Worker code.
    if(process.env.OUTPOST_PREVIEW_UI==='1')headers.set('Authorization','Bearer private');
    const request=new Request('https://outpost.example'+req.url,{method:req.method,headers,...(!['GET','HEAD'].includes(req.method)?{body:Readable.toWeb(req),duplex:'half'}:{})});
    const response=await worker.fetch(request,env);res.writeHead(response.status,Object.fromEntries(response.headers));if(response.body)Readable.fromWeb(response.body).pipe(res);else res.end();
  }catch{res.writeHead(500);res.end('Synthetic server failure');}
});
server.listen(Number(process.env.PORT||0),'127.0.0.1',()=>console.log(JSON.stringify({port:server.address().port,revision_id:revision.revision_id,manifest:revision})));
process.on('SIGTERM',()=>server.close(()=>{closers.forEach(fn=>fn());process.exit(0);}));

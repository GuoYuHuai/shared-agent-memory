import assert from 'node:assert/strict';
import {GithubKnowledge,selectMode} from '../adapters/github.mjs';
let files={'MANIFEST.json':'{"schema_version":"1"}','POLICY.md':'Capture the fictional test facts.'},version=0;
const tool={supportsPinnedRef:false,repository:async()=>({full_name:'example/knowledge',private:true}),
 head:async()=>String(version),listFiles:async()=>({complete:true,paths:Object.keys(files)}),
 read:async(_,__,p)=>({content:files[p]}),create:async(_,__,p,c)=>{if(files[p])throw Error('exists');files[p]=c;return{commit:String(++version)}}};
const e=(id,statement)=>({schema_version:'1',id,statement,kind:'fact',scope:'garden',source:'fictional:user',
 authority:'user-stated',status:'active',created_at:'2026-10-28T00:00:00Z',source_device_id:'fixture'});
const a=new GithubKnowledge(tool,'example/knowledge'), b=new GithubKnowledge(tool,'example/knowledge');
await a.append(e('first','color blue'));assert.equal((await b.query('blue')).results.length,1);
await a.append(e('second','color green'));
await a.append({...e('edge','correct color'),kind:'supersession',old_id:'first',new_id:'second'});
assert.equal((await b.query('blue')).results.length,0);assert.equal((await b.query('green')).results[0].source,'fictional:user');
await a.append({...e('expired','past'),expires_at:'2000-01-01T00:00:00Z'});assert.equal((await b.query('past')).results.length,0);
await a.append({...e('edge2','concurrent correction'),kind:'supersession',old_id:'first',new_id:'expired'});
assert.equal((await b.query('color')).conflicts.length,1);
assert.equal((await b.query('past')).results.length,0);
assert.equal((await a.append(e('second','color green'))).duplicate,true);
await assert.rejects(()=>a.append({...e('second','conflicting content'),created_at:'2027-01-01T00:00:00Z'}));
files['MANIFEST.json']='{"schema_version":"99"}';
await assert.rejects(()=>a.append(e('future-schema','stop')));
files['MANIFEST.json']='{"schema_version":"1"}';
const receipt=await a.append(e('receipt','branch receipt'));assert.equal(receipt.version,null);
assert.equal(receipt.readback,'branch-content-verified');
const races=await Promise.allSettled([
  a.append(e('race','one')),
  b.append({...e('race','two'),created_at:'2027-01-01T00:00:00Z'})]);
assert.equal(races.filter(r=>r.status==='fulfilled').length,1);
assert.equal((await b.snapshot()).complete,true);
const ro=new GithubKnowledge({...tool,create:null},'example/knowledge');assert.equal((await ro.append(e('proposal','unsaved'))).saved,false);
assert.equal(selectMode({agent:'unknown-brand'}).mode,'guidance');
assert.equal(selectMode(Object.fromEntries(['private_repo_read','private_repo_write'].map(n=>[n,{status:'verified',evidence:'readback',checked_at:'today'}]))).mode,'github');
const incomplete=new GithubKnowledge({...tool,listFiles:async()=>({complete:false,paths:[]})},'example/knowledge');assert.equal((await incomplete.query('x')).absence_confirmed,false);
let churn=0;const changing=new GithubKnowledge({...tool,head:async()=>String(++churn)},'example/knowledge');assert.equal((await changing.snapshot()).reason,'snapshot-changed');
await assert.rejects(()=>new GithubKnowledge({...tool,repository:async()=>({full_name:'example/knowledge',private:false})},'example/knowledge').append(e('bad','bad')));
// An atomic local correction is one JSON file containing both ledger events.
await a.append(e('atomic-old','atomic theme blue'));
const replacement=e('atomic-new','atomic theme green');
const relation={...e('atomic-edge','atomic correction'),kind:'supersession',old_id:'atomic-old',new_id:'atomic-new'};
files['ledger/by-id/atomic-new.json']=JSON.stringify([replacement,relation]);version++;
const arraySnapshot=await b.snapshot();
assert.equal(arraySnapshot.events.filter(row=>['atomic-new','atomic-edge'].includes(row.id)).length,2);
assert.equal((await b.query('atomic theme blue')).results.length,0);
assert.equal((await b.query('atomic theme green')).results[0].id,'atomic-new');
assert.equal((await a.append(replacement)).duplicate,true);
files['ledger/container.json']=JSON.stringify({events:[e('wrapped','container record')]});version++;
assert.equal((await b.query('container record')).results[0].id,'wrapped');
files['ledger/duplicate-array.json']=JSON.stringify([e('atomic-new','conflicting array identity')]);
await assert.rejects(()=>b.snapshot(),/conflicting event identity/);
delete files['ledger/duplicate-array.json'];
console.log('GitHub connector contracts passed (in-memory fixture, not product certification)');

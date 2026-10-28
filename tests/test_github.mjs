import assert from 'node:assert/strict';
import {GithubKnowledge,selectMode,validateEvent} from '../adapters/github.mjs';
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
assert.equal((await b.query('color')).complete,false);
assert.equal((await b.query('past')).results.length,0);
delete files['ledger/by-id/edge2.json'];version++;
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
// Each scenario starts from an independent, fictional ledger snapshot.
const client=events=>new GithubKnowledge({...tool,supportsPinnedRef:true,
  listFiles:async()=>({complete:true,paths:['ledger/events.json']}),
  read:async()=>({content:JSON.stringify(events)})},'example/knowledge');
const resolution=(id,candidate_id,target,created_at='2026-10-28T00:00:00.000001Z')=>({...e(id,'review marker'),kind:'candidate-resolution',candidate_id,resolution:target,created_at});
const candidate={...e('candidate','candidate marker'),status:'candidate',authority:'agent-inference'};
for(const target of ['accepted','rejected']) {
  const c=client([candidate,resolution('review','candidate',target)]);
  const all=await c.query('marker',{includeHistory:true});
  assert.equal(all.results.length,1);assert.equal(all.results[0].id,'candidate');
  assert.equal(all.results[0].effective_status,target);
  assert.equal(all.results[0].current_eligible,target==='accepted');
  assert.equal((await c.query('marker')).current_count,target==='accepted'?1:0);
}
const early=resolution('z-early','candidate','accepted'),late=resolution('a-late','candidate','rejected','2026-10-28T00:00:00.000002Z');
for(const decisions of [[early,late],[late,early]]) {
  const r=await client([candidate,...decisions]).query('marker',{includeHistory:true});
  assert.equal(r.results[0].candidate_state,'rejected');assert.equal(r.current_count,0);
}
const equalTime=await client([candidate,early,{...late,created_at:early.created_at}]).query('marker');
assert.equal(equalTime.complete,false);assert.equal(equalTime.reason,'lifecycle-conflict');assert.equal(equalTime.current_count,0);
for(const [extra,status] of [[{valid_from:'2999-01-01'},'not-yet-effective'],[{expires_at:'2000-01-01'},'expired'],[{conflicts_with:['elsewhere']},'disputed']]) {
  const r=await client([{...e('temporal','temporal marker'),...extra}]).query('marker',{includeHistory:true});
  assert.equal(r.results[0].effective_status,status);assert.equal(r.current_count,0);
}
for(const field of ['created_at','source_time','observed_at','confirmed_at','expires_at','valid_from'])
  for(const value of ['nonsense','2026-02-30T00:00:00Z','2026-01-01T24:00:00Z','2026-01-01T00:00:00+24:00'])
    assert.throws(()=>validateEvent({...e('invalid','marker'),[field]:value}),new RegExp('invalid '+field));
for(const bad of [{kind:'candidate-resolution'}, {kind:'candidate-resolution',candidate_id:'candidate',resolution:42},
  {kind:'candidate-resolution',candidate_id:'candidate',resolution:'maybe'},{kind:'supersession',old_id:'x',new_id:'x'},
  {kind:'supersession',old_id:123,new_id:'x'},{conflicts_with:'other'},{source_device_id:' unknown-device '}])
  assert.throws(()=>validateEvent({...e('invalid','marker'),...bad}));
for(const authority of ['AGENT-INFERENCE',' Agent_Observation ','agent','agent recommendation'])
  for(const status of ['active',' Accepted ','verified-current'])assert.throws(()=>validateEvent({...e('inference','marker'),authority,status}),/inference/);
assert.doesNotThrow(()=>validateEvent({...e('verified-agent','marker'),authority:' Agent_Verified '}));
const keyRecords=[{...e('east','zone east'),fact_key:'region'}, {...e('west','zone west'),fact_key:'region'},
  {...e('other-project','zone south'),scope:'other-project',fact_key:'region'}];
const grouped=await client(keyRecords).query('east',{scope:'garden'});
assert.deepEqual(grouped.results.map(r=>r.id),['east','west']);assert.equal(grouped.current_count,0);
assert.equal(grouped.results.every(r=>r.disputed),true);
const isolated=await client(keyRecords).query('south',{scope:'other-project'});
assert.equal(isolated.current_count,1);assert.equal(isolated.results[0].id,'other-project');
const edge=(id,old_id,new_id)=>({...e(id,'audit'),kind:'supersession',old_id,new_id});
const branches=await client([e('a','marker'),e('b','marker'),e('c','marker'),e('d','marker'),edge('ab','a','b'),edge('ac','a','c'),edge('bd','b','d')]).query('marker');
assert.equal(branches.reason,'lifecycle-conflict');assert.equal(branches.current_count,0);assert.equal(branches.complete,false);
const cycle=await client([e('a','marker'),e('b','marker'),edge('ab','a','b'),edge('ba','b','a')]).query('marker');
assert.equal(cycle.reason,'lifecycle-cycle');assert.equal(cycle.current_count,0);assert.equal(cycle.absence_confirmed,false);
const chain=await client([e('a','marker'),e('b','marker'),e('c','marker'),edge('ab','a','b'),edge('bc','b','c')]).query('marker',{includeHistory:true});
assert.equal(chain.results.find(r=>r.id==='a').superseded_by,'c');assert.equal(chain.current_count,1);
console.log('GitHub connector contracts passed (in-memory fixture, not product certification)');

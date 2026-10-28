/** Connector-neutral reference. No shell, filesystem, credential, or network dependency.
 * Bind operations to the host's authenticated GitHub tools. See docs/github.md.
 */
export const capabilityNames = ['public_repo_read','private_repo_read','private_repo_write',
  'private_repo_create','atomic_commit','local_files','local_exec','persistent_instructions',
  'lifecycle_hooks','scheduler','unattended_model','history_access'];
export function selectMode(evidence) {
  const yes = n => evidence[n]?.status === 'verified' && Boolean(evidence[n].evidence) && Boolean(evidence[n].checked_at);
  return {mode: yes('local_files') && yes('local_exec') ? 'local' :
    yes('private_repo_read') && yes('private_repo_write') ? 'github' :
    yes('private_repo_read') ? 'read-only' : 'guidance',
    deterministic_automation:yes('scheduler'), semantic_automation:yes('scheduler') && yes('unattended_model'),
    persistence:yes('persistent_instructions') ? 'verified' : 'session-only'};
}
const validId = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
export function validateEvent(e) {
  if (String(e.schema_version) !== '1' || !validId.test(e.id ?? '')) throw Error('unsupported event identity/schema');
  for (const k of ['kind','scope','statement','source','authority','status','created_at','source_device_id'])
    if (typeof e[k] !== 'string' || !e[k].trim()) throw Error('missing '+k);
  if (!Number.isFinite(Date.parse(e.created_at))) throw Error('invalid timestamp');
  if (/^agent/.test(e.authority) && e.status === 'active') throw Error('inference must remain candidate');
  if(e.kind === 'supersession' && (!validId.test(e.old_id??'') || !validId.test(e.new_id??'') || e.old_id===e.new_id))
    throw Error('invalid supersession');
  return e;
}
const stable = o => JSON.stringify((function sort(v) {
  if(Array.isArray(v))return v.map(sort);
  if(v && typeof v==='object')return Object.fromEntries(Object.keys(v).sort().map(k=>[k,sort(v[k])]));
  return v;
})(o));
export function eventPath(e) { validateEvent(e); return `ledger/by-id/${e.id}.json`; }

export class GithubKnowledge {
  constructor(connector, repository, branch='main') { Object.assign(this,{connector,repository,branch}); }
  async identity() {
    const r=await this.connector.repository(this.repository);
    if(r.full_name.toLowerCase()!==this.repository.toLowerCase() || r.private!==true) throw Error('private repository identity unverified');
    return r;
  }
  async snapshot() {
    await this.identity();
    for(let attempt=0;attempt<2;attempt++) {
      const head=await this.connector.head(this.repository,this.branch);
      const ref=this.connector.supportsPinnedRef ? head : this.branch;
      const listing=await this.connector.listFiles(this.repository,ref,'ledger/');
      if(!listing.complete) return {complete:false,reason:'listing-incomplete',events:[]};
      const events=[];const seen=new Map();
      for(const path of listing.paths.filter(p=>/^ledger\/.*\.jsonl?$/.test(p))) {
        const file=await this.connector.read(this.repository,ref,path);
        if(!file || typeof file.content!=='string') return {complete:false,reason:'file-unreadable',events:[]};
        const rows=path.endsWith('.jsonl') ? file.content.split('\n').filter(Boolean).map(JSON.parse) : [JSON.parse(file.content)];
        for(const e of rows) {
          validateEvent(e);
          if(seen.has(e.id) && seen.get(e.id)!==stable(e)) throw Error('conflicting event identity: '+e.id);
          if(!seen.has(e.id))events.push({...e,path});seen.set(e.id,stable(e));
        }
      }
      if(this.connector.supportsPinnedRef || head===await this.connector.head(this.repository,this.branch))
        return {complete:true,version:head,events,coverage:'ledger-only; canonical Markdown must be read separately'};
    }
    return {complete:false,reason:'snapshot-changed',events:[]};
  }
  async append(event) {
    validateEvent(event);await this.identity();
    const path=eventPath(event);
    if(!this.connector.create) return {saved:false,proposal:{path,event},reason:'read-only'};
    const manifest=JSON.parse((await this.connector.read(this.repository,this.branch,'MANIFEST.json')).content);
    if(String(manifest.schema_version)!=='1')throw Error('unsupported repository schema; writes paused');
    const policy=await this.connector.read(this.repository,this.branch,'POLICY.md');
    if(!policy?.content?.trim())throw Error('user policy unavailable; writes paused');
    const snapshot=await this.snapshot();
    if(!snapshot.complete)throw Error('cannot establish event identity on incomplete snapshot');
    const previous=snapshot.events.find(e=>e.id===event.id);
    if(previous) {
      const {path:oldPath,...saved}=previous;
      if(stable(saved)!==stable(event))throw Error('conflicting event identity: '+event.id);
      return {saved:true,path:oldPath,version:snapshot.version,readback:true,duplicate:true};
    }
    // create-only semantics; existing content must never be silently overwritten.
    const result=await this.connector.create(this.repository,this.branch,path,JSON.stringify(event,null,2)+'\n');
    if(!result.commit) throw Error('write returned no commit evidence');
    const file=await this.connector.read(this.repository,this.connector.supportsPinnedRef?result.commit:this.branch,path);
    if(stable(JSON.parse(file.content))!==stable(event)) throw Error('readback mismatch');
    return {saved:true,path,write_commit:result.commit,
      version:this.connector.supportsPinnedRef?result.commit:null,
      readback:this.connector.supportsPinnedRef?'commit-verified':'branch-content-verified',
      discovery:'requires independent snapshot'};
  }
  async query(query,{scope,includeHistory=false}={}) {
    const s=await this.snapshot();if(!s.complete)return {...s,results:[],absence_confirmed:false};
    const edges=new Map();
    for(const e of s.events.filter(e=>e.kind==='supersession' && e.status==='active')) {
      if(!edges.has(e.old_id))edges.set(e.old_id,new Set());edges.get(e.old_id).add(e.new_id);
    }
    const conflicts=[...edges].filter(([,v])=>v.size>1).map(([old_id,v])=>({old_id,targets:[...v]}));
    const tokens=query.toLowerCase().split(/\s+/).filter(Boolean);
    const results=s.events.filter(e=>e.kind!=='supersession' && (!scope||e.scope===scope) &&
      tokens.every(t=>(e.statement+' '+(e.value??'')+' '+e.scope).toLowerCase().includes(t))).map(e=>{
        let status=edges.has(e.id)?'superseded':e.status;
        if(e.expires_at && Date.parse(e.expires_at)<=Date.now())status='expired';
        const disputed=conflicts.some(c=>c.old_id===e.id||c.targets.includes(e.id));
        if(disputed && status==='active')status='disputed';
        return {...e,effective_status:status,current_eligible:status==='active',disputed};
      });
    return {complete:true,version:s.version,coverage:s.coverage,conflicts,
      results:results.filter(e=>includeHistory||['active','disputed'].includes(e.effective_status)),
      absence_confirmed:false}; // ledger coverage alone cannot prove whole-library absence
  }
}

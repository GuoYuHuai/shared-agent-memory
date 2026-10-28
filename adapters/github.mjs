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
const isId = value => typeof value==='string'&&validId.test(value);
const lifecycleKinds = new Set(['supersession','candidate-resolution']);
const currentStatuses = new Set(['active','accepted','current','observed-current','verified']);
const timeFields = ['created_at','source_time','observed_at','confirmed_at','expires_at','valid_from'];
const normalized = value => value.trim().toLowerCase().replace(/[\s_]+/g,'-').replace(/^-+|-+$/g,'');
const currentStatus = value => currentStatuses.has(normalized(value)) || /^(active|accepted|verified)-/.test(normalized(value));
// Compare ISO times at Python datetime's microsecond precision. Date.parse alone
// both loses sub-millisecond review order and normalizes impossible calendar dates.
function instant(value) {
  if(typeof value!=='string')return null;
  const m=/^(\d{4})-(\d{2})-(\d{2})(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?(Z|[+-]\d{2}(?::?\d{2})?)?)?$/.exec(value.trim());
  if(!m)return null;
  const [,y,mo,d,h='0',mi='0',s='0',fraction='',zone='Z']=m;
  const year=Number(y),month=Number(mo),day=Number(d);
  const date=new Date(0);date.setUTCFullYear(year,month-1,day);date.setUTCHours(Number(h),Number(mi),Number(s),0);
  if(year<1||month<1||month>12||day<1||date.getUTCFullYear()!==year||date.getUTCMonth()!==month-1||date.getUTCDate()!==day||Number(h)>23||Number(mi)>59||Number(s)>59)return null;
  let offset=0;
  if(zone!=='Z') {
    const digits=zone.slice(1).replace(':','');const hours=Number(digits.slice(0,2)),minutes=Number(digits.slice(2)||0);
    if(hours>23||minutes>59)return null;
    offset=(hours*60+minutes)*60000*(zone[0]==='-'?-1:1);
  }
  return BigInt(date.getTime()-offset)*1000n+BigInt(fraction.slice(0,6).padEnd(6,'0'));
}
export function validateEvent(e) {
  if(!e||typeof e!=='object'||Array.isArray(e))throw Error('event must be an object');
  if (String(e.schema_version) !== '1' || !isId(e.id)) throw Error('unsupported event identity/schema');
  for (const k of ['kind','scope','statement','source','authority','status','created_at','source_device_id'])
    if (typeof e[k] !== 'string' || !e[k].trim()) throw Error('missing '+k);
  for(const field of timeFields)if((field==='created_at'||e[field]!=null&&e[field]!=='')&&instant(e[field])===null)throw Error('invalid '+field);
  if(e.source_device_id.trim()==='unknown-device')throw Error('missing source_device_id');
  const authority=normalized(e.authority);
  if(authority!=='agent-verified'&&(authority==='agent'||authority.startsWith('agent-'))&&currentStatus(e.status))throw Error('inference must remain candidate');
  if(e.kind === 'supersession' && (!isId(e.old_id) || !isId(e.new_id) || e.old_id===e.new_id))
    throw Error('invalid supersession');
  if(e.kind==='candidate-resolution'&&(!isId(e.candidate_id)||typeof e.resolution!=='string'||!['accepted','rejected'].includes(e.resolution.trim().toLowerCase())))throw Error('invalid candidate resolution');
  if(e.conflicts_with!=null&&(!Array.isArray(e.conflicts_with)||e.conflicts_with.some(id=>typeof id!=='string'||!validId.test(id))))throw Error('invalid conflicts_with');
  if(e.fact_key!=null&&typeof e.fact_key!=='string')throw Error('invalid fact_key');
  return e;
}
const stable = o => JSON.stringify((function sort(v) {
  if(Array.isArray(v))return v.map(sort);
  if(v && typeof v==='object')return Object.fromEntries(Object.keys(v).sort().map(k=>[k,sort(v[k])]));
  return v;
})(o));
export function eventPath(e) { validateEvent(e); return `ledger/by-id/${e.id}.json`; }

function lifecycle(events) {
  const direct=new Map(),resolutions=new Map(),conflicts=[];
  for(const e of events.filter(e=>e.status==='active')) {
    if(e.kind==='supersession') {
      if(!direct.has(e.old_id))direct.set(e.old_id,new Set());
      direct.get(e.old_id).add(e.new_id);
    } else if(e.kind==='candidate-resolution') {
      const target=e.resolution.trim().toLowerCase(),time=instant(e.created_at),previous=resolutions.get(e.candidate_id);
      if(previous&&time===previous.time&&target!==previous.target)
        conflicts.push({kind:e.kind,candidate_id:e.candidate_id,targets:[previous.target,target].sort()});
      if(!previous||time>previous.time)resolutions.set(e.candidate_id,{target,time});
    }
  }
  for(const [old_id,targets] of direct)if(targets.size>1)conflicts.push({kind:'supersession',old_id,targets:[...targets].sort()});
  if(conflicts.length)return {reason:'lifecycle-conflict',conflicts};
  const replacements=new Map();
  for(const start of direct.keys()) {
    const path=[],seen=new Set();let id=start;
    while(direct.has(id)&&!replacements.has(id)) {
      if(seen.has(id))return {reason:'lifecycle-cycle',conflicts:[],cycle:[...path,id]};
      seen.add(id);path.push(id);id=direct.get(id).values().next().value;
    }
    const target=replacements.get(id)??id;
    for(const old of path)replacements.set(old,target);
  }
  return {replacements,resolutions,conflicts};
}

function project(events,state) {
  const now=BigInt(Date.now())*1000n,groups=new Map();
  const rows=events.filter(e=>!lifecycleKinds.has(e.kind)).map(e=>{
    const replacement=state.replacements.get(e.id),resolution=state.resolutions.get(e.id)?.target;
    const expires=e.expires_at?instant(e.expires_at):null,valid=e.valid_from?instant(e.valid_from):null;
    let status=resolution??e.status;
    if(expires!==null&&expires<=now)status='expired';
    else if(valid!==null&&valid>now)status='not-yet-effective';
    else if(e.conflicts_with?.length)status='disputed';
    else if(replacement)status='superseded';
    const row={...e,effective_status:status,current_eligible:false,disputed:status==='disputed'};
    if(replacement)row.superseded_by=replacement;
    if(resolution)row.candidate_state=resolution;
    if(e.fact_key&&!replacement&&resolution!=='rejected'&&!['expired','not-yet-effective'].includes(status)&&
      (resolution==='accepted'||currentStatuses.has(e.status))) {
      const key=JSON.stringify([e.scope,e.fact_key]);
      if(!groups.has(key))groups.set(key,[]);groups.get(key).push(row);
    }
    return row;
  });
  for(const group of groups.values())if(group.length>1)for(const row of group) {
    row.effective_status='disputed';row.disputed=true;
    row.conflicts_with=group.filter(other=>other.id!==row.id).map(other=>other.id).sort();
  }
  for(const row of rows)row.current_eligible=currentStatus(row.effective_status)&&!row.superseded_by&&row.candidate_state!=='rejected';
  return rows;
}

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
      for(const path of listing.paths.filter(p=>/^ledger\/.*\.jsonl?$/.test(p)).sort()) {
        const file=await this.connector.read(this.repository,ref,path);
        if(!file || typeof file.content!=='string') return {complete:false,reason:'file-unreadable',events:[]};
        const parsed=path.endsWith('.jsonl') ? file.content.split('\n').filter(Boolean).map(JSON.parse) : [JSON.parse(file.content)];
        // Local atomic corrections publish [replacement, supersession] together.
        // Match the core JSON-array and {events:[...]} ledger containers.
        const rows=parsed.flatMap(value=>Array.isArray(value)?value:
          value && Array.isArray(value.events)?value.events:[value]);
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
    const state=lifecycle(s.events);
    if(state.reason)return {complete:false,version:s.version,coverage:s.coverage,...state,results:[],current_count:0,absence_confirmed:false};
    const tokens=query.toLowerCase().split(/\s+/).filter(Boolean);
    const rows=project(s.events,state).filter(e=>!scope||e.scope===scope);
    const matched=rows.filter(e=>tokens.every(t=>(e.statement+' '+(e.value??'')+' '+e.scope).toLowerCase().includes(t)));
    const ids=new Set(matched.map(e=>e.id)),identities=new Set(matched.filter(e=>e.fact_key).map(e=>JSON.stringify([e.scope,e.fact_key])));
    const results=rows.filter(e=>ids.has(e.id)||e.fact_key&&identities.has(JSON.stringify([e.scope,e.fact_key])))
      .filter(e=>includeHistory||e.current_eligible||e.effective_status==='disputed');
    return {complete:true,version:s.version,coverage:s.coverage,conflicts:state.conflicts,
      results,current_count:results.filter(e=>e.current_eligible).length,
      absence_confirmed:false}; // ledger coverage alone cannot prove whole-library absence
  }
}

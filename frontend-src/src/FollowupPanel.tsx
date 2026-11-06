import {useEffect,useRef,useState} from 'react';
import {AlertCircle,Check,ChevronDown,Clock3,MessageSquarePlus,RefreshCw,ShieldCheck,Sparkles,X} from 'lucide-react';
import {FollowupRequests,type FollowupRequest} from './followupRequests';
import './FollowupPanel.css';

type Source={kind:string;id:string;revision?:number;field_id:string;value?:string|null;unit?:string|null;origin?:string;version?:string;statement_id?:string;statement_revision?:number;source_question_ids?:string[];source_ref?:{segment_revision?:number};text_excerpt?:string;};
type Card={id:string;revision:number;field_id:string;label:string;question:string;original_question:string;reason:string;rule:string;kind:'rule'|'ai';priority:number;sources:Source[];status:'pending'|'accepted'|'deferred'|'ignored';stale:boolean;};
type Settings={enabled:boolean;revision:number;provider:string;model:string;cloud_fallback:boolean;};
type Workspace={case_id:string;fingerprint:string;active_cards:Card[];cards:Card[];material_gaps:{material_id:string;title:string;state:string;action:string}[];ai:Settings&{state:string;reason:string;accepted_count?:number;rejected_count?:number;elapsed_seconds?:number};analysis_current:boolean;analyzed_at:string|null;transcription_bridge?:{state:string;window_limit:number;observed_count:number;statement_ids:string[];issues:{code:string}[];message:string};};
type Props={caseId:string};
const stateLabels={pending:'待判断',accepted:'已采纳',deferred:'稍后处理',ignored:'已忽略'};
const materialLabels:Record<string,string>={access_denied:'无读取权限',uploaded_not_parsed:'已上传，尚未解析',parsed:'已解析',parsed_not_checked:'已解析，尚未核验',missing:'未提供',available:'可用',not_uploaded:'未上传',not_provided:'未提供'};
const errorText=(value:unknown)=>value instanceof Error?value.message:'补问未完成，请重试。';
class FollowupHttpError extends Error{
  constructor(message:string,readonly status:number){super(message);}
}

export default function FollowupPanel({caseId}:Props){
  const [workspace,setWorkspace]=useState<Workspace|null>(null),[settings,setSettings]=useState<Settings|null>(null);
  const [error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false),[loading,setLoading]=useState(true);
  const [editing,setEditing]=useState(''),[question,setQuestion]=useState('');
  const alive=useRef(true),requests=useRef(new FollowupRequests()),loadedCase=useRef(caseId),lastSources=useRef('');
  loadedCase.current=caseId;
  const base=`/api/due-diligence/cases/${encodeURIComponent(caseId)}/followups`;
  const current=(request:FollowupRequest)=>alive.current&&loadedCase.current===request.caseId&&requests.current.owns(request);
  const api=async<T,>(request:FollowupRequest,path:string,body?:unknown):Promise<T>=>{
    const response=await fetch(base+path,{signal:request.controller.signal,...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
    if(!response.ok){const value=await response.json().catch(()=>null);throw new FollowupHttpError(typeof value?.detail==='string'?value.detail:value?.detail?.message||`本地补问服务未完成请求（${response.status}）。`,response.status);}
    return response.json() as Promise<T>;
  };
  const apply=(value:Workspace)=>{
    setWorkspace(value);setSettings(value.ai);
    const bridge=value.transcription_bridge;
    if(bridge){
      const signature=JSON.stringify([bridge.state,bridge.statement_ids,bridge.issues]);
      if(signature!==lastSources.current){
        lastSources.current=signature;
        window.dispatchEvent(new CustomEvent('due-diligence:asr-sources-updated',{detail:{caseId}}));
      }
    }
  };
  const showError=(error:unknown)=>{
    setError(errorText(error));
    // Current access loss removes restricted cards and draft text; callers
    // first verify ownership so an old denied poll cannot clear a new workspace.
    if(error instanceof FollowupHttpError&&[401,403,404,410].includes(error.status)){
      setWorkspace(null);setSettings(null);setEditing('');setQuestion('');
    }
  };
  async function refresh(manual=false,retryAI=false){
    const request=requests.current.begin(caseId,manual?'manual':'background');
    if(!request)return;
    if(manual){setBusy(true);setError('');}
    try{
      let value=await api<Workspace>(request,'');
      // An aborted poll may still resolve in a transport/cache. Never start its next request.
      if(!current(request))return;
      if(!value.analysis_current||retryAI)value=await api<Workspace>(request,'/analyze',{expected_fingerprint:value.fingerprint,retry_ai:retryAI});
      if(current(request)){apply(value);setError('');if(manual)setNotice('已按当前资料更新补问；问卷原顺序保持不变。');}
    // Transient polling errors leave a person's unsaved question intact.
    }catch(e){if(current(request))showError(e);}
    finally{if(current(request)&&requests.current.finish(request)){setBusy(false);setLoading(false);}}
  }
  useEffect(()=>{
    requests.current.cancel();alive.current=true;loadedCase.current=caseId;lastSources.current='';setWorkspace(null);setSettings(null);setEditing('');setQuestion('');setError('');setNotice('');setBusy(false);setLoading(true);
    void refresh();const timer=window.setInterval(()=>void refresh(),8000);
    return()=>{alive.current=false;requests.current.cancel();window.clearInterval(timer);};
    // The parent keys this component by case ID; this also guards responses across case changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[caseId]);
  async function act(card:Card,action:'accept'|'edit'|'defer'|'ignore'){
    if(requests.current.hasManual())return;
    if(action==='edit'&&!question.trim()){setError('请填写要保留的补问内容。');return;}
    const request=requests.current.begin(caseId,'manual');
    if(!request)return;
    setBusy(true);setError('');
    try{
      const value=await api<Workspace>(request,`/${encodeURIComponent(card.id)}/actions`,{expected_revision:card.revision,action,...(action==='edit'?{text:question.trim()}:{})});
      if(current(request)){apply(value);setEditing('');setQuestion('');
        window.dispatchEvent(new Event('due-diligence:followup-changed'));setNotice(action==='ignore'?'已忽略；没有新依据时不会重复提醒。':action==='defer'?'已移入稍后处理。':'已保留补问，由你决定何时询问。');}
    }catch(e){if(current(request))showError(e);}
    finally{if(current(request)&&requests.current.finish(request)){setBusy(false);setLoading(false);}}
  }
  async function toggleAI(){
    if(!settings)return;
    const request=requests.current.begin(caseId,'manual');
    if(!request)return;
    setBusy(true);setError('');
    try{
      const next=await api<Settings>(request,'/ai-settings',{enabled:!settings.enabled,expected_revision:settings.revision});
      if(current(request)){setSettings(next);setNotice(next.enabled?'本案已启用本地AI候选补问；首次分析可能需要等待。':'本案已关闭本地AI，继续使用规则提醒。');}
    }catch(e){if(current(request))showError(e);}
    finally{if(current(request)&&requests.current.finish(request)){setBusy(false);setLoading(false);void refresh();}}
  }
  function startEditing(card:Card){
    if(requests.current.hasManual())return;
    requests.current.cancelBackground();setEditing(card.id);setQuestion(card.question);setError('');
  }
  function cancelEditing(){
    if(requests.current.hasManual())return;
    requests.current.cancelBackground();setEditing('');setQuestion('');
  }
  const renderCard=(card:Card,compact=false)=><article className={'fu-card '+(card.stale?'fu-stale':'')} key={card.id}>
    <div className="fu-card-meta"><span>{card.kind==='ai'?<Sparkles size={12}/>:<MessageSquarePlus size={12}/>} {card.kind==='ai'?'AI候选':'规则提醒'} · {card.label}</span><small>{card.stale?'依据已变化':stateLabels[card.status]}</small></div>
    {editing===card.id?<label className="fu-edit-label">调整补问内容<textarea aria-label="调整补问内容" value={question} maxLength={600} rows={3} onChange={e=>setQuestion(e.target.value)} disabled={busy}/></label>:<h4>{card.question}</h4>}
    <p className="fu-reason">{card.reason}</p>
    <details className="fu-sources"><summary>查看依据 <ChevronDown size={12}/></summary><ul>{card.sources.map(source=><li key={source.id}><strong>{source.kind==='questionnaire_gap'?'问卷待补项':source.kind==='statement'?'当前原话摘录':source.origin==='previsit'?'事前记载':'现场陈述'}</strong><span>{source.kind==='questionnaire_gap'?'尚无本字段可用记录；先判断是否适用。':source.kind==='statement'?`${source.text_excerpt||''} · 原话第${source.revision??'—'}版${source.source_ref?.segment_revision?` / 转写第${source.source_ref.segment_revision}版`:''}`:`${source.value??'未知'} ${source.unit??''} · 第${source.revision??'—'}版`}</span><small>{source.id}{source.statement_id?` / 原话第${source.statement_revision}版`:''}{source.source_question_ids?.length?` / ${source.source_question_ids.join('、')}`:''}</small></li>)}</ul></details>
    {!card.stale&&!compact&&<div className="fu-card-actions">{editing===card.id?<><button disabled={busy||!question.trim()} onClick={()=>void act(card,'edit')}><Check size={12}/>保留修改</button><button disabled={busy} onClick={cancelEditing}>取消</button></>:<><button disabled={busy} onClick={()=>void act(card,'accept')}><Check size={12}/>{card.status==='accepted'?'继续保留':'采纳'}</button><button disabled={busy} onClick={()=>startEditing(card)}>编辑</button><button disabled={busy} onClick={()=>void act(card,'defer')}><Clock3 size={12}/>稍后</button><button disabled={busy} onClick={()=>void act(card,'ignore')}><X size={12}/>忽略</button></>}</div>}
    {!card.stale&&compact&&<button className="fu-restore" disabled={busy} onClick={()=>void act(card,'accept')}>恢复到待问列表</button>}
  </article>;
  const deferred=workspace?.cards.filter(c=>!c.stale&&c.status==='deferred')||[];
  const history=workspace?.cards.filter(c=>c.stale||c.status==='ignored')||[];
  return <section className="fu-panel" aria-label="动态补问建议" data-analysis-fingerprint={workspace?.fingerprint||''} data-analysis-current={workspace?.analysis_current?'true':'false'} data-ai-state={workspace?.ai.state||'loading'}><header className="fu-heading"><div><span className="dd-eyebrow">交流中的待澄清事项</span><h3><MessageSquarePlus size={18}/>补问建议</h3><p>最多同时显示三条；由你采纳、修改或忽略，不打断访谈。</p></div><button disabled={busy||loading} onClick={()=>void refresh(true)} aria-label="刷新补问建议"><RefreshCw size={14} className={busy?'dd-spin':''}/></button></header>
    <div className="fu-ai"><div><ShieldCheck size={14}/><span>{settings?.enabled?'本案本地AI已启用':'本案使用规则提醒'}</span></div><button disabled={busy||!settings} onClick={()=>void toggleAI()}>{settings?.enabled?'关闭本地AI':'启用本地AI'}</button><p>{workspace?.ai.reason||'只在本机处理当前案件，外部模型不会自动启用。'}</p>{workspace?.ai.state==='degraded'&&<button disabled={busy} onClick={()=>void refresh(true,true)}>重试本地AI</button>}</div>
    {workspace?.transcription_bridge&&<p className="fu-message" role="status">{workspace.transcription_bridge.message}{workspace.transcription_bridge.statement_ids.length>0&&` 本次窗口已关联${workspace.transcription_bridge.statement_ids.length}段来源原话，事实仍由人员确认。`}{workspace.transcription_bridge.state==='empty'&&' 当前尚无可自动引用的稳定转写；未稳定或未写盘的文字会继续等待。'}{workspace.transcription_bridge.state==='partial'&&' 部分录音来源暂不可用或无法验证，未自动引用；已有问卷可继续使用。'}</p>}
    {error&&<p className="fu-message fu-error" role="alert"><AlertCircle size={14}/>{error}</p>}{notice&&<p className="fu-message" role="status">{notice}</p>}
    {loading?<p className="fu-empty" role="status">正在核对当前资料……</p>:workspace&&<><div className="fu-active">{workspace.active_cards.length?workspace.active_cards.map(c=>renderCard(c)):<p className="fu-empty">当前没有活动补问。已忽略或稍后的项目不会无依据重复出现。</p>}</div>
      {deferred.length>0&&<details className="fu-history"><summary>稍后处理（{deferred.length}）</summary>{deferred.map(c=>renderCard(c,true))}</details>}
      {history.length>0&&<details className="fu-history"><summary>已忽略与旧依据（{history.length}）</summary>{history.map(c=>renderCard(c,!c.stale))}</details>}
      {workspace.material_gaps.length>0&&<details className="fu-history"><summary>事前材料提示（{workspace.material_gaps.length}）</summary><p className="fu-material-note">材料缺失或未解析时，请回到上游准备流程。本应用不生成附件采集任务。</p><ul className="fu-materials">{workspace.material_gaps.map(g=><li key={g.material_id}><strong>{g.title}</strong><small>{materialLabels[g.state]||g.state}</small></li>)}</ul></details>}
    </>}
    <footer>补问不是风险结论；采纳问题不会自动确认事实或给出授信额度。</footer>
  </section>;
}

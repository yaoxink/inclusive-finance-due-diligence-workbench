import {useState,useEffect,useRef,useCallback} from 'react';
import type {FormEvent,ReactNode} from 'react';
import Markdown from 'react-markdown';
import CompanyProfileSettings from './CompanyProfileSettings';
import type {CompanyProfile} from './CompanyProfileSettings';
import {Plus,Search,Settings,Mic,Square,ArrowUpRight,FileText,FolderOpen,ChevronRight,Check,
  Sparkles,Clock,Upload,Download,RefreshCw,X,Bookmark,Lightbulb,MessageCircleQuestion,Radio,
  Pencil,Flag,ExternalLink,Headphones,Link2,NotebookPen,Volume2,LoaderCircle} from 'lucide-react';

type Segment={id:string;ordinal:number;start:number;end:number;raw_text:string;text:string;revision:number;flagged:number};
type Source={id:string;title:string;kind:string;content:string;url:string;origin_id:string;created_at:string;auto_discovered?:boolean;discovery?:string;origin_meeting_id?:string};
type Doc={id:string;kind:string;version:number;content:string;refs:string[];transcript_version:number;edited:number};
type Insight={id:string;kind:string;text:string;refs:string[];quotes:Record<string,string>;status:string;transcript_version:number};
type Job={kind:string;state:string;error:string;progress:string};
type PreparationContext={focus:string;source_count:number;matched_count:number;selected_count:number;reason:'matched'|'no_sources'|'no_matches'|'missing_focus'};
type PreparationRetrieval={id:string;status:'running'|'retrieved'|'completed'|'partial'|'failed';title:string;goal:string;focus?:string;aliases?:string[];queries?:string[];
  source_count?:number;selected_count?:number;local_scanned?:number;feishu_searched?:boolean;warnings?:string[];trace?:{stage?:string;message?:string;status?:string}[];
  research_mode?:'history'|'first_contact';history_status?:'found'|'not_found_in_scope'|'unknown';public_search_status?:string;public_searched?:boolean;
  company_profile_version?:number;company_profile_confirmed?:boolean;counterparty_status?:'verified'|'unresolved';counterparty_name?:string;counterparty_website?:string;
  selected_sources?:{ref:string;title:string;headings:string[];discovery:string}[];document_id?:string|null;created_at:string;updated_at:string};
type Meeting={id:string;title:string;goal:string;counterparty_name?:string;counterparty_website?:string;company_profile_version?:number;company_profile_confirmed?:boolean;state:string;created_at:string;updated_at:string;started_at?:string;ended_at?:string;
  segments:Segment[];sources:Source[];documents:Doc[];insights:Insight[];jobs:Job[];transcript_version:number;last_edit_version:number;
  seq:number;live:Live;auto_analysis:number;audio_available:boolean;error:string;active_meeting_id:string|null;metrics:Record<string,unknown>;preparation_context?:PreparationContext;preparation_retrieval?:PreparationRetrieval|null};
type Live={seconds?:number;level?:number;message?:string;backlog?:number};
type Candidate={kind:string;title:string;origin_id:string;url:string;summary:string;supported:boolean;doc_type?:string};
type LlmStatus={configured:boolean;model:string;base_url:string;key_configured:boolean};
type Connections={llm:LlmStatus;feishu:{ready:boolean;message:string;login_command:string};asr:{available:boolean};active_meeting_id:string|null};
const states:Record<string,string>={ready:'待开始',starting:'准备录音',recording:'录音中',finalizing:'正在保存',completed:'已归档',interrupted:'已中断',imported:'文字记录'};
const time=(s=0)=>`${Math.floor(s/60).toString().padStart(2,'0')}:${Math.floor(s%60).toString().padStart(2,'0')}`;
const date=(d:string)=>new Date(d).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'});
const sourceKinds:Record<string,string>={feishu:'飞书文档',web:'公开网页',company_profile:'我方公司资料',history:'本地历史'};
const sourceKind=(kind:string)=>sourceKinds[kind]||'参考资料';
const isActive=(m:Meeting|null)=>!!m&&['starting','recording','finalizing'].includes(m.state);
async function api<T>(path:string,body?:unknown,method='GET'):Promise<T>{
  const form=body instanceof FormData;
  const r=await fetch('/api'+path,{method,headers:body&&!form?{'Content-Type':'application/json'}:undefined,
    body:body?(form?body as FormData:JSON.stringify(body)):undefined});
  if(!r.ok){const e=await r.json().catch(()=>({detail:'无法连接本地服务'}));throw new Error(typeof e.detail==='string'?e.detail:'输入内容不符合要求，请检查。');}
  return r.json();
}

function PreparationBasis({meeting,documentId,onSourceClick}:{meeting:Meeting;documentId?:string;onSourceClick:(ref:string)=>void}){
  const report=meeting.preparation_retrieval,focus=report?.focus||meeting.counterparty_name||meeting.preparation_context?.focus||'';
  if(!report)return <section className="preparation-basis" aria-label="准备依据"><div className="preparation-basis-heading"><span>自动查找资料</span><strong>预计关键词：{focus||'将根据会议主题识别'}</strong></div>
    <p>点击生成后，会先查找相关历史会议和资料；未取得可用历史时，补查对方公开信息，并结合已确认的我方能力提出合作假设。</p>
    <small>尚无自动检索报告。你可以在设置中维护我方公司资料；也可在会议信息中补充对方官网。</small></section>;
  const subjectChanged=report.title.trim()!==meeting.title.trim()||report.goal.trim()!==meeting.goal.trim()||
    (typeof report.counterparty_website==='string'&&report.counterparty_website!==(meeting.counterparty_website||''))||
    (typeof report.counterparty_name==='string'&&report.counterparty_name!==(meeting.counterparty_name||''));
  const differentDocument=!!documentId&&!!report.document_id&&documentId!==report.document_id;
  const aliases=[...new Set((report.aliases||[]).map(value=>value.trim()).filter(value=>value&&value!==report.focus))];
  const queries=[...new Set((report.queries||[]).filter(Boolean))],warnings=report.warnings||[],trace=(report.trace||[]).filter(step=>step.message);
  const hasSourceCount=typeof report.source_count==='number'&&Number.isFinite(report.source_count);
  const hasSelectedCount=typeof report.selected_count==='number'&&Number.isFinite(report.selected_count);
  const hasLocalScanned=typeof report.local_scanned==='number'&&Number.isFinite(report.local_scanned);
  const hasFeishuStatus=typeof report.feishu_searched==='boolean';
  const profileChanged=report.research_mode==='first_contact'&&typeof report.company_profile_version==='number'&&typeof meeting.company_profile_version==='number'&&report.company_profile_version!==meeting.company_profile_version;
  const publicLabels:Record<string,string>={not_needed:'本次无需公开调研',completed:'公开资料已读取并核验',partial:'公开调研部分完成',unavailable:'公开调研未完成',failed:'公开调研未完成',no_reliable_results:'未取得可用的公开正文',identity_unresolved:'对方主体尚未核实',running:'正在检索公开资料',not_started:'尚未开始公开调研'};
  const labels={running:'查找中',retrieved:'资料已就绪',completed:'已完成',partial:'部分完成',failed:'未完成'};
  return <section className="preparation-basis" aria-label="准备依据"><div className="preparation-basis-heading"><span>最近一次检索</span><strong>关键词：{report.focus||(report.status==='running'?'正在识别':'尚未确定')}</strong><span className={'retrieval-status '+report.status}>{labels[report.status]}</span></div>
    {subjectChanged&&<p className="retrieval-warning">会议信息已修改。以下是“{report.title}”的上次检索结果，再次生成会按新的内容重新查找。</p>}
    {report.research_mode&&<div className="research-mode"><span>{report.research_mode==='first_contact'?'按首次接触准备':'接续历史讨论'}</span>{report.history_status==='found'?<small>已找到相关历史会议</small>:report.history_status==='not_found_in_scope'?<small>本次检索范围内未取得相关历史会议</small>:report.history_status==='unknown'?<small>历史检索尚有未完成项，无法确认是否有往来记录</small>:null}</div>}
    {report.research_mode==='first_contact'&&<div className="research-context"><p>公开调研：{report.public_search_status?publicLabels[report.public_search_status]||'状态待确认':report.public_searched?'已执行公开检索，读取状态待确认':'尚未确认检索状态'}</p>
      <p>我方依据：{report.company_profile_version?`公司资料 v${report.company_profile_version} · ${report.company_profile_confirmed?'已确认':'待确认'}`:'尚无已确认的公司资料'}</p>
      {!report.company_profile_confirmed&&<small>可先查看对方概况；确认我方能力后，再生成个性化合作切入点。</small>}
      {report.counterparty_status==='unresolved'&&<small>对方主体尚未核实，请补充公司全称或官网后再生成。</small>}</div>}
    {profileChanged&&<p className="retrieval-warning">我方公司资料已更新为 v{meeting.company_profile_version}；本次检索使用 v{report.company_profile_version}，已有稿件仍保留当时依据。重新生成可使用新资料。</p>}
    {differentDocument&&<p className="retrieval-warning">下方正在查看其他版本；这里展示的是最近一次检索，不代表所选旧稿的依据。</p>}
    {!!documentId&&!report.document_id&&<p className="retrieval-warning">本次检索尚未保存关联草稿，下方仍为已有版本。</p>}
    {!!aliases.length&&<div className="retrieval-terms"><span>候选检索词</span>{aliases.map(alias=><span className="retrieval-term" key={alias}>{alias}</span>)}</div>}
    <p>{report.status==='running'?'正在查找和读取资料。':report.status==='failed'?'此次检索未完成。':''}
      {hasSourceCount&&<>已读取并选入 <strong>{report.source_count}</strong> 份资料。</>}{hasSelectedCount&&<>已提取 <strong>{report.selected_count}</strong> 段相关正文。</>}
      {(!hasSourceCount||!hasSelectedCount)&&'检索尚未完成，统计待确认。'}</p>
    <small>{hasLocalScanned?`已检查 ${report.local_scanned} 项本地内容`:'本地检查数量待确认'} · {hasFeishuStatus?(report.feishu_searched?'已执行飞书检索':'本次尚未检索飞书'):'飞书检索状态待确认'}</small>
    {!!warnings.length&&<details className="retrieval-warnings"><summary>{warnings.length} 条检索提示</summary><ul>{warnings.map((warning,index)=><li key={index}>{warning}</li>)}</ul></details>}
    {!!report.selected_sources?.length&&<details className="retrieval-details"><summary>查看已读取的来源（{report.selected_sources.length}）</summary><div className="retrieval-source-list">{report.selected_sources.map(source=><button key={source.ref} className="retrieval-source" disabled={!meeting.sources.some(item=>item.id===source.ref)} onClick={()=>onSourceClick(source.ref)}><FileText size={15}/><span><strong>{source.title}</strong>{!!source.headings?.length&&<small>{source.headings.join(' · ')}</small>}</span><ChevronRight size={14}/></button>)}</div></details>}
    {(!!trace.length||!!queries.length)&&<details className="retrieval-details"><summary>查看检索经过</summary>{!!queries.length&&<p>实际检索词：{queries.join('、')}</p>}<ol>{trace.map((step,index)=><li key={index}>{step.message}</li>)}</ol></details>}
  </section>;
}

function Modal({title,close,children,initialFocus}:{title:string;close:()=>void;children:ReactNode;initialFocus?:'close'}){
  const ref=useRef<HTMLDivElement>(null);
  useEffect(()=>{const previous=document.activeElement as HTMLElement;const first=(initialFocus==='close'?null:ref.current?.querySelector<HTMLElement>('input:not(:disabled),textarea:not(:disabled),select:not(:disabled)'))||ref.current?.querySelector<HTMLElement>('button');first?.focus();
    const key=(e:KeyboardEvent)=>{if(e.key==='Escape')close();if(e.key==='Tab'){const nodes=Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href],summary')||[]).filter(node=>node.getClientRects().length>0);if(!nodes.length)return;const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}};
    document.addEventListener('keydown',key);return()=>{document.removeEventListener('keydown',key);previous?.focus();};},[]);
  return <div className="overlay" onClick={e=>{if(e.target===e.currentTarget)close();}}><div className="modal" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
    <header><h2>{title}</h2><button className="icon" onClick={close} aria-label="关闭"><X size={20}/></button></header>{children}</div></div>;
}

function CloudModelSettings({onLoaded,onSaved}:{onLoaded:(value:Connections)=>void;onSaved:(value:LlmStatus)=>void}){
  const [saved,setSaved]=useState<LlmStatus|null>(null),[baseUrl,setBaseUrl]=useState(''),[model,setModel]=useState('');
  const [apiKey,setApiKey]=useState(''),[clearKey,setClearKey]=useState(false),[loading,setLoading]=useState(true);
  const [busy,setBusy]=useState<'save'|'test'|''>(''),[feedback,setFeedback]=useState<{kind:'success'|'error';text:string}|null>(null);
  const active=useRef(true),loadRequest=useRef(0);
  async function loadSettings(){
    const request=++loadRequest.current;setLoading(true);setFeedback(null);
    try{const value=await api<Connections>('/integrations');if(!active.current||loadRequest.current!==request)return;
      setSaved(value.llm);setBaseUrl(value.llm.base_url);setModel(value.llm.model);onLoaded(value);
    }catch{if(active.current&&loadRequest.current===request)setFeedback({kind:'error',text:'未能读取当前配置，请重新加载后再编辑。'});}
    finally{if(active.current&&loadRequest.current===request)setLoading(false);}
  }
  useEffect(()=>{active.current=true;loadSettings();return()=>{active.current=false;loadRequest.current++;};},[]);
  const normalizeUrl=(value:string)=>value.trim().replace(/\/+$/,'');
  const dirty=!!saved&&(normalizeUrl(baseUrl)!==normalizeUrl(saved.base_url)||model.trim()!==saved.model||!!apiKey.trim()||clearKey);
  const canSave=!!saved&&!loading&&!busy&&dirty&&!!baseUrl.trim()&&(clearKey||!!model.trim());
  const edit=()=>setFeedback(null);
  async function saveSettings(e:FormEvent){
    e.preventDefault();if(!canSave)return;setBusy('save');setFeedback(null);
    try{const value=await api<LlmStatus>('/integrations/llm',{base_url:baseUrl.trim(),model:model.trim(),
      ...(apiKey.trim()?{api_key:apiKey.trim()}:{}),...(clearKey?{clear_api_key:true}:{})},'PUT');
      if(!active.current)return;setSaved(value);setBaseUrl(value.base_url);setModel(value.model);setApiKey('');setClearKey(false);onSaved(value);
      setFeedback({kind:'success',text:value.key_configured?'配置已保存并立即生效，可点击“测试连接”核对。':'配置已保存，API Key 未配置。'});
    }catch(e){if(active.current)setFeedback({kind:'error',text:(e as Error).message});}
    finally{if(active.current)setBusy('');}
  }
  async function testSettings(){
    if(!saved?.configured||dirty||loading||busy)return;setBusy('test');setFeedback(null);
    try{await api('/integrations/llm/test',{},'POST');if(active.current)setFeedback({kind:'success',text:'连接成功，模型可以按要求返回 JSON。'});}
    catch(e){if(active.current)setFeedback({kind:'error',text:(e as Error).message});}
    finally{if(active.current)setBusy('');}
  }
  return <section className="settings-section cloud-settings" aria-label="云端模型配置"><h3><Sparkles size={17}/> 云端模型 <span className="badge">{loading?'读取中':saved?.configured?'已配置':'待配置'}</span></h3>
    <p className="muted">连接支持 OpenAI 兼容接口的模型，生成准备稿、会中提示和纪要。</p>
    {loading&&<p className="settings-loading" role="status"><LoaderCircle size={15} className="spin"/> 正在读取本机配置……</p>}
    <form onSubmit={saveSettings} autoComplete="off"><fieldset disabled={!saved||loading||!!busy}>
      <label>API 地址<input type="url" autoComplete="off" spellCheck={false} required maxLength={2000} placeholder="https://api.openai.com/v1" value={baseUrl} onChange={e=>{setBaseUrl(e.target.value);edit();}} aria-describedby="llm-url-help"/></label>
      <p className="settings-help" id="llm-url-help">填写服务商提供的 API 根地址，例如以 /v1 结尾；不含 /chat/completions。</p>
      <label>模型名称<input autoComplete="off" spellCheck={false} maxLength={200} placeholder="填写服务商提供的模型名称" value={model} onChange={e=>{setModel(e.target.value);edit();}}/></label>
      <label>API Key <span className="key-state">{saved?.key_configured?'已保存，留空保持原 Key':'尚未保存'}</span><input type="password" autoComplete="new-password" spellCheck={false} maxLength={4096} placeholder={saved?.key_configured?'输入新 Key 可替换已保存的 Key':'粘贴你的 API Key'} disabled={clearKey} value={apiKey} onChange={e=>{setApiKey(e.target.value);edit();}} aria-describedby="llm-key-help"/></label>
      <p className="settings-help" id="llm-key-help">密钥保存在本机后端，保存后不会回显。更换 API 地址时，请重新填写对应的 Key。</p>
      {!!saved?.key_configured&&<label className="key-clear"><input type="checkbox" checked={clearKey} onChange={e=>{setClearKey(e.target.checked);setApiKey('');edit();}}/><span>清除已保存的 API Key</span></label>}
    </fieldset>
    {feedback&&<div className={'settings-feedback '+feedback.kind} role={feedback.kind==='error'?'alert':'status'}>{feedback.text}</div>}
    {!saved&&!loading?<button type="button" onClick={loadSettings}><RefreshCw size={14}/> 重新加载配置</button>:<div className="settings-actions"><button type="submit" className="primary" disabled={!canSave}>{busy==='save'?<LoaderCircle size={15} className="spin"/>:<Check size={15}/>} {busy==='save'?'正在保存':'保存配置'}</button><button type="button" disabled={!saved?.configured||dirty||loading||!!busy} onClick={testSettings}>{busy==='test'?<LoaderCircle size={15} className="spin"/>:<RefreshCw size={15}/>} {busy==='test'?'正在测试':'测试连接'}</button><span>{dirty?'有未保存的修改，请先保存配置':'保存后立即生效，无需重启'}</span></div>}
    <p className="settings-help test-disclosure">测试连接会向已保存的 API 地址发送简短测试文本，不包含会议内容。</p></form>
  </section>;
}

export default function App(){
  const [list,setList]=useState<Meeting[]>([]),[query,setQuery]=useState(''),[selected,setSelected]=useState(localStorage.getItem('meeting-id')||'');
  const [meeting,setMeeting]=useState<Meeting|null>(null),[tab,setTab]=useState('prepare'),[live,setLive]=useState<Live>({});
  const [loading,setLoading]=useState(!!selected);
  const [recordPending,setRecordPending]=useState(false);
  const [activeMeetingId,setActiveMeetingId]=useState<string|null>(null);
  const [connections,setConnections]=useState<Connections|null>(null),[devices,setDevices]=useState<{index:number;name:string;default:boolean;hostapi:string}[]>([]),[device,setDevice]=useState(-1);
  const [showNew,setShowNew]=useState(false),[showEdit,setShowEdit]=useState(false),[showSettings,setShowSettings]=useState(false);
  const [title,setTitle]=useState(''),[goal,setGoal]=useState(''),[counterpartyName,setCounterpartyName]=useState(''),[counterpartyWebsite,setCounterpartyWebsite]=useState(''),[message,setMessage]=useState(''),[pending,setPending]=useState('');
  const [searchKind,setSearchKind]=useState('history'),[sourceQuery,setSourceQuery]=useState(''),[results,setResults]=useState<Candidate[]>([]),[searched,setSearched]=useState(false);
  const [selectedCandidates,setSelectedCandidates]=useState<string[]>([]),[sourceDetail,setSourceDetail]=useState<Source|null>(null),[historyInsights,setHistoryInsights]=useState(false);
  const [editingSegment,setEditingSegment]=useState<Segment|null>(null),[segmentText,setSegmentText]=useState('');
  const [editingDoc,setEditingDoc]=useState<Doc|null>(null),[docText,setDocText]=useState(''),[docChoice,setDocChoice]=useState<Record<string,string>>({});
  const [sourceFocus,setSourceFocus]=useState('');
  const mainPanel=useRef<HTMLElement>(null);
  const contextPanel=useRef<HTMLElement>(null);
  const connectionsRequest=useRef(0);
  const queryRef=useRef(query);
  const current=useRef(selected),seq=useRef(-1),audio=useRef<HTMLAudioElement>(null),toastTimer=useRef<ReturnType<typeof setTimeout>|null>(null);
  const snapshotRequest=useRef(0),acceptedSnapshotRequest=useRef(0);
  const latestDocuments=useRef<{meetingId:string;ids:Record<string,string>}>({meetingId:'',ids:{}});
  const notify=useCallback((s:string)=>{setMessage(s);if(toastTimer.current)clearTimeout(toastTimer.current);toastTimer.current=setTimeout(()=>setMessage(''),6500);},[]);
  const loadList=useCallback(async()=>{try{setList(await api<Meeting[]>('/meetings?q='+encodeURIComponent(queryRef.current)));}catch(e){notify((e as Error).message);}},[]);
  const load=useCallback(async(mid:string)=>{const request=++snapshotRequest.current;try{const snap=await api<Meeting>('/meetings/'+mid);
    if(current.current!==mid||snap.seq<seq.current||(snap.seq===seq.current&&request<acceptedSnapshotRequest.current))return;
    const newest:Record<string,string>={};for(const doc of snap.documents)newest[doc.kind]=doc.id;
    if(latestDocuments.current.meetingId===mid){const changed=Object.keys(newest).filter(kind=>latestDocuments.current.ids[kind]!==newest[kind]);
      if(changed.length)setDocChoice(choice=>({...choice,...Object.fromEntries(changed.map(kind=>[kind,newest[kind]]))}));}
    latestDocuments.current={meetingId:mid,ids:newest};acceptedSnapshotRequest.current=request;
    setMeeting(snap);setLive(snap.live);seq.current=snap.seq;
  }catch(e){if(current.current===mid)notify((e as Error).message);}finally{if(current.current===mid)setLoading(false);}},[]);
  const refreshConnections=async()=>{const request=++connectionsRequest.current;try{const value=await api<Connections>('/integrations');if(connectionsRequest.current===request)setConnections(value);}catch(e){if(connectionsRequest.current===request)notify((e as Error).message);}};
  const refreshDevices=async()=>{try{const d=await api<typeof devices>('/devices');setDevices(d);setDevice(old=>d.some(x=>x.index===old)?old:(d.find(x=>x.default)?.index??d[0]?.index??-1));}catch(e){notify((e as Error).message);}};
  const refreshActivity=useCallback(async()=>{try{const status=await api<{active_meeting_id:string|null}>('/health');setActiveMeetingId(status.active_meeting_id);}catch{/* Connection errors are surfaced by the meeting request; avoid repeating a background toast. */}},[]);
  useEffect(()=>{queryRef.current=query;loadList();},[query,loadList]);
  useEffect(()=>{refreshConnections();refreshDevices();},[]);
  useEffect(()=>{refreshActivity();const timer=setInterval(refreshActivity,5000);window.addEventListener('focus',refreshActivity);return()=>{clearInterval(timer);window.removeEventListener('focus',refreshActivity);};},[refreshActivity]);
  useEffect(()=>{current.current=selected;seq.current=-1;acceptedSnapshotRequest.current=0;latestDocuments.current={meetingId:'',ids:{}};setMeeting(null);setLive({});setResults([]);setSearched(false);setSelectedCandidates([]);setDocChoice({});
    if(!selected){setLoading(false);return;}setLoading(true);localStorage.setItem('meeting-id',selected);load(selected);
    const events=new EventSource('/api/meetings/'+selected+'/events');
    events.onmessage=e=>{if(current.current!==selected)return;const d=JSON.parse(e.data);if(d.seq<seq.current)return;setLive(d.live||{});if(d.seq!==seq.current){load(selected);loadList();}};
    return()=>events.close();},[selected]);
  useEffect(()=>{if(meeting)setSourceQuery((meeting.counterparty_name||meeting.preparation_context?.focus||meeting.title).slice(0,30));},[meeting?.id,meeting?.title,meeting?.counterparty_name,meeting?.preparation_context?.focus]);
  async function action(key:string,fn:()=>Promise<unknown>){setPending(key);try{await fn();if(selected)await load(selected);await loadList();}catch(e){notify((e as Error).message);}finally{setPending('');}}
  async function recordingAction(key:string,fn:()=>Promise<unknown>){setRecordPending(true);try{await action(key,fn);}finally{setRecordPending(false);refreshActivity();}}
  const choose=(id:string)=>{setSelected(id);setTab('prepare');setSourceFocus('');setHistoryInsights(false);};
  function sourceClick(ref:string){const s=meeting?.sources.find(s=>s.id===ref);if(s){setSourceDetail(s);return;}
    if(meeting?.segments.some(s=>s.id===ref)){setTab('live');setSourceFocus(ref);}}
  useEffect(()=>{mainPanel.current?.scrollTo({top:0,behavior:'instant'});contextPanel.current?.scrollTo({top:0,behavior:'instant'});},[tab,meeting?.id]);
  useEffect(()=>{if(tab!=='live'||!sourceFocus)return;const node=document.getElementById(sourceFocus),panel=mainPanel.current;
    if(node&&panel){const top=node.getBoundingClientRect().top-panel.getBoundingClientRect().top+panel.scrollTop-Math.max(24,(panel.clientHeight-node.clientHeight)/2);
      panel.scrollTo({top:Math.max(0,top),behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});node.focus({preventScroll:true});
      const segment=meeting?.segments.find(s=>s.id===sourceFocus);if(segment&&audio.current)audio.current.currentTime=segment.start;setSourceFocus('');}
  },[tab,sourceFocus,meeting?.id]);
  const md=(text:string)=><Markdown components={{img:({alt})=><span>[图片：{alt||'未展示'}]</span>,a:({href,children})=>href?.startsWith('#')?<button className="citation" onClick={()=>sourceClick(href.slice(1))}>{children}</button>:<a href={href} target="_blank" rel="noreferrer">{children}</a>}}>{text}</Markdown>;
  const job=(kind:string)=>meeting?.jobs.find(j=>j.kind===kind);
  const generating=(kind:string)=>job(kind)?.state==='running';
  function startGenerate(kind:string){action(kind,()=>api(`/meetings/${selected}/generate/${kind}`,{},'POST'));}
  function docPanel(kind:string){
    const docs=meeting!.documents.filter(d=>d.kind===kind),latest=docs[docs.length-1],doc=docs.find(d=>d.id===docChoice[kind])||latest;
    const running=generating(kind),submitting=pending===kind;
    const stale=!!doc&&kind==='minutes'&&doc.transcript_version<meeting!.last_edit_version;
    return <><div className="section-heading"><div><span className="eyebrow">{kind==='prepare'?'本次会议 · 准备资料':'本次会议 · 讨论成果'}</span><h2>{kind==='prepare'?'带着上下文开始':'让讨论有所着落'}</h2></div>
      <button className="primary" disabled={!!pending||generating(kind)||(kind==='minutes'&&(isActive(meeting)||!meeting!.segments.length))} onClick={()=>startGenerate(kind)}>
        {running||submitting?<LoaderCircle className="spin" size={16}/>:<Sparkles size={16}/>} {running?(kind==='prepare'?'查找与生成中':'正在生成'):submitting?'正在提交':kind==='prepare'?(doc?'重新检索并生成新稿':'查找资料并生成'):doc?'生成新草稿':'生成纪要'}</button></div>
      {kind==='prepare'&&<PreparationBasis meeting={meeting!} documentId={doc?.id} onSourceClick={sourceClick}/>}
      {running?<div className="notice generation-progress" role="status" aria-live="polite"><LoaderCircle className="spin" size={16}/><span>{job(kind)?.progress||'正在等待模型响应…'}</span></div>
        :submitting?<div className="notice generation-progress" role="status" aria-live="polite"><LoaderCircle className="spin" size={16}/><span>正在提交生成请求…</span></div>
        :job(kind)?.state==='failed'?<div className="notice error" role="alert">{job(kind)?.error}<button disabled={!!pending} onClick={()=>startGenerate(kind)}>重试</button></div>
        :job(kind)?.state==='succeeded'&&latest?<div className="notice generation-success" role="status" aria-live="polite"><Check size={16}/><span>{job(kind)?.progress||`最新草稿 v${latest.version} 已保存，可在下方查看。`}</span></div>:null}
      {stale&&<div className="notice">文字稿已修改，这份纪要基于旧版本。请生成新草稿后核对。</div>}
      {doc?<><div className="document-tools"><select aria-label="文档版本" value={doc.id} onChange={e=>setDocChoice({...docChoice,[kind]:e.target.value})}>{docs.map(d=><option key={d.id} value={d.id}>草稿 v{d.version}{d.edited?' · 已编辑':''}</option>)}</select>
        <span>重新生成会保留已有版本</span><button onClick={()=>{setEditingDoc(doc);setDocText(doc.content);}}><Pencil size={14}/> 编辑</button><a className="button" href={`/api/meetings/${selected}/export?kind=${kind}&document_id=${doc.id}`}><Download size={14}/> 导出</a></div>
        <article className="document">{md(doc.content)}</article></>:<div className="blank-document"><div className="document-illustration"><NotebookPen size={40} strokeWidth={1.2}/></div><h3>{kind==='prepare'?'先理解主题，再接续历史':'会议结束后，纪要会自动生成'}</h3>
        <p>{kind==='prepare'?'点击“查找资料并生成”，自动找回相关会议与资料，整理成一份精简准备稿。你可以查看检索经过、核对来源，也可以手动补充参考资料。':'记录会被整理为讨论内容、明确结论、行动项和未解决问题。你可以核对原文，再编辑和导出。'}</p>
        <div className="output-outline">{(kind==='prepare'?(meeting!.preparation_retrieval?.research_mode==='first_contact'?['对方概况','合作切入点','建议议程','待确认问题']:['背景与已有结论','历史待跟进','建议议程','待确认问题']):['主要讨论','决定与结论','行动项','未解决问题']).map((t,i)=><div key={t}><span>0{i+1}</span>{t}</div>)}</div></div>}
    </>;
  }

  const modalOpen=showNew||showEdit||showSettings||!!sourceDetail||!!editingSegment||!!editingDoc;
  const activeId=activeMeetingId;
  const otherRecording=!!activeId&&activeId!==selected;
  const sourceMeetingId=sourceDetail?.origin_meeting_id||(sourceDetail?.kind==='history'?sourceDetail.origin_id:'');
  const stages=[{id:'prepare',label:'会前准备',number:'01'},{id:'live',label:'实时记录',number:'02'},{id:'minutes',label:'会议纪要',number:'03'}];
  const openNew=()=>{setTitle('');setGoal('');setCounterpartyName('');setCounterpartyWebsite('');setShowNew(true);};
  const openSettings=()=>setShowSettings(true);

  return <div className="app-shell"><header className="app-header" inert={modalOpen}><div className="brand"><div className="brand-mark"><Radio size={24}/></div><strong>会间</strong><span>让讨论有所延续</span></div><div className="header-actions"><span className="demo">本机工作空间</span><button className="connection-pill" onClick={openSettings}><i className={connections?.llm.configured?'connected':''}/>{connections?.llm.configured?'AI 已配置':'连接 AI 服务'}<ArrowUpRight size={14}/></button></div></header>
    <aside className="sidebar" inert={modalOpen}>
    <button className="new-meeting" onClick={openNew}><Plus size={18}/> 新建会议 <span>＋</span></button>
    <label className="search-box"><Search size={16}/><input aria-label="搜索历史会议" placeholder="搜索会议或文字内容" value={query} onChange={e=>setQuery(e.target.value)}/></label>
    <div className="sidebar-label"><span>会议记录</span><span>{list.length.toString().padStart(2,'0')}</span></div>
    <nav className="meeting-list" aria-label="历史会议">{list.map(m=><button className={'meeting-link '+(m.id===selected?'selected':'')} key={m.id} onClick={()=>choose(m.id)}><FileText size={17}/><div><strong>{m.title}</strong><small>{date(m.created_at)} <span>·</span> {states[m.state]||m.state}</small></div>{['recording','starting'].includes(m.state)&&<i className="record-dot"/>}</button>)}{!list.length&&<div className="sidebar-empty">{query?'没有找到相关记录':'你的会议会保存在这里。'}</div>}</nav>
    <div className="sidebar-connection"><div><FileText size={16}/><strong>飞书资料</strong><span>{connections?.feishu.ready?'已连接':'待连接'}</span></div><button onClick={openSettings}>{connections?.feishu.ready?'查看连接':'设置资料连接'}<ArrowUpRight size={13}/></button></div>
    <footer><span><i/> 录音与记录保存在本机</span><button onClick={openSettings}><Settings size={17}/> 设置与连接</button></footer></aside>
    <main className="workspace" inert={modalOpen}><div className="topbar"><span><FolderOpen size={15}/> 我的工作空间 <ChevronRight size={14}/> {meeting?.title||'会议助手'}</span><span className="topbar-status"><span className="status-dot"/> 本机保存</span></div>
    {otherRecording&&<div className="session-alert" role="status"><Radio size={16}/><span>另一场会议正在后台记录</span><button onClick={()=>{setSelected(activeId!);setTab('live');}}>返回录制会议 <ArrowUpRight size={14}/></button></div>}
    {!meeting&&loading?<div className="welcome"><LoaderCircle className="spin" size={24}/><p>正在恢复会议记录……</p></div>:!meeting?<div className="welcome"><span className="eyebrow">会前有准备 · 会中有记录 · 会后有行动</span><h1>让每次讨论，<br/>都有下文。</h1><p>找回上次的上下文，留住这次的好想法。<br/>你的会议记录，会在这里慢慢积累。</p><button className="primary" onClick={openNew}><Plus size={18}/> {list.length?'新建一场会议':'创建第一场会议'}</button><div className="welcome-flow"><button onClick={openNew}><FolderOpen/><strong>准备资料</strong><small>带上历史记录与待确认问题</small></button><button onClick={openSettings}><Mic/><strong>检查麦克风</strong><small>准备好设备，再开始记录</small></button><button onClick={openSettings}><NotebookPen/><strong>连接 AI 助手</strong><small>生成准备稿、提示与纪要</small></button></div></div>:
    <><header className="meeting-header"><div className="heading-meta"><span className={'status-tag '+meeting.state}><i/>{states[meeting.state]}</span><span>{new Date(meeting.created_at).toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric'})}</span></div>
      <div className="title-row"><h1>{meeting.title}</h1><button className="icon" aria-label="编辑会议信息" onClick={()=>{setTitle(meeting.title);setGoal(meeting.goal);setCounterpartyName(meeting.counterparty_name||'');setCounterpartyWebsite(meeting.counterparty_website||'');setShowEdit(true);}}><Pencil size={16}/></button></div>
      <p className="meeting-goal">{meeting.goal||'还没有填写会议目标，随时可以补充。'}</p></header>
      <div className="meeting-toolbar"><nav className="tabs" role="tablist" aria-label="会议阶段">{stages.map((stage,index)=><button key={stage.id} id={'tab-'+stage.id} role="tab" aria-selected={tab===stage.id} aria-controls="meeting-panel" tabIndex={tab===stage.id?0:-1} className={tab===stage.id?'active':''} onClick={()=>setTab(stage.id)} onKeyDown={e=>{let next=index;if(e.key==='ArrowRight')next=(index+1)%stages.length;else if(e.key==='ArrowLeft')next=(index+stages.length-1)%stages.length;else if(e.key==='Home')next=0;else if(e.key==='End')next=stages.length-1;else return;e.preventDefault();setTab(stages[next].id);document.getElementById('tab-'+stages[next].id)?.focus();}}><span>{stage.number}</span>{stage.label}</button>)}</nav><span className="toolbar-note">{tab==='prepare'?'从资料开始，让讨论接续':tab==='live'?'专注讨论，随时核对原文':'留下结论，也留下下一步'}</span></div>
      {meeting.error&&<div className="meeting-error notice error">{meeting.error}</div>}
      <div className="content-grid"><section className="main-panel" ref={mainPanel} id="meeting-panel" role="tabpanel" aria-labelledby={'tab-'+tab} tabIndex={0}>{tab==='prepare'&&docPanel('prepare')}{tab==='minutes'&&docPanel('minutes')}
        {tab==='live'&&<><div className="section-heading"><div><span className="eyebrow">{isActive(meeting)?'普通话 · 实时转写':'会议原文 · 可核对修正'}</span><h2>把注意力留给讨论</h2></div><a className="button" href={`/api/meetings/${selected}/export?kind=transcript`}><Download size={15}/> 导出文字稿</a></div>
          <div className="recording-info"><div><Volume2 size={16}/><span>{isActive(meeting)?live.message||'准备接收语音':meeting.state==='ready'?'准备好麦克风后，即可开始记录':meeting.state==='imported'?'导入的文字记录，可逐段修正':meeting.state==='interrupted'?'会议曾中断，以下为已恢复的内容':'录音已结束，可以逐段修正文字'}</span></div>{isActive(meeting)&&<div className="meter" aria-label={`收音音量 ${Math.round(live.level||0)}`}><span style={{transform:`scaleX(${Math.max(.02,(live.level||0)/100)})`}}/></div>}</div>
          <p className="approx-label">{meeting.state==='imported'?'导入文字 · 未关联录音':'时间为近似音频范围 · 不自动区分发言人'} · {meeting.segments.length} 个文字片段</p>
          <div className="transcript">{meeting.segments.map(s=><div key={s.id} id={s.id} tabIndex={-1} className={'segment '+(s.flagged?'flagged':'')}><div className="segment-index"><button className="segment-time" disabled={!meeting.audio_available||isActive(meeting)} title="定位到近似录音位置" onClick={()=>{if(audio.current){audio.current.currentTime=s.start;audio.current.play().catch(()=>{});}}}>{meeting.state==='imported'?'文字':time(s.start)}</button><small>#{String(s.ordinal+1).padStart(2,'0')}</small></div><div className="segment-text"><p>{s.text}</p>{s.revision>1&&<small>已修订 · 原始文字保留</small>}{!!s.flagged&&<small className="stale">已标记，待修正</small>}</div><button className="icon segment-action" aria-label={isActive(meeting)?'标记需修正':'修正这段文字'} onClick={()=>isActive(meeting)?action('flag',()=>api(`/meetings/${selected}/segments/${s.id}`,{flagged:!s.flagged},'PATCH')):(setEditingSegment(s),setSegmentText(s.text))}>{isActive(meeting)?<Flag size={14}/>:<Pencil size={14}/>}</button></div>)}
          {!meeting.segments.length&&<div className="transcript-empty"><Mic size={34} strokeWidth={1.2}/><h3>{isActive(meeting)?'正在等待你的声音':'还没有文字记录'}</h3><p>{isActive(meeting)?'本地模型就绪后，文字会随讲话分段出现。':meeting.state==='ready'?'先在设置中选择麦克风，再点击下方“开始记录”。':'这场会议没有可展示的转写片段。'}</p></div>}
          {meeting.state==='recording'&&<div className="listening"><span/><span/><span/><small>正在聆听</small></div>}</div>
          {meeting.audio_available&&!isActive(meeting)&&<div className="audio-player"><Headphones size={17}/><audio controls ref={audio} src={`/api/meetings/${selected}/audio`}>浏览器不支持音频播放</audio></div>}
          {isActive(meeting)&&<p className="footnote">刷新或关闭页面不会停止后台录音，请点击“结束会议”保存。</p>}
        </>}
      </section><aside className="context-panel" ref={contextPanel} aria-label={tab==='prepare'?'会议资料':'会中提示'}>{tab==='prepare'?<><div className="context-title"><Link2 size={17}/><h3>会议上下文</h3><span>{meeting.sources.length}</span></div><p className="muted">生成时会自动查找资料，也可在这里手动补充。</p><div className="manual-source-label">手动补充资料</div><div className="source-tabs"><button className={searchKind==='history'?'active':''} onClick={()=>{setSearchKind('history');setResults([]);setSelectedCandidates([]);setSearched(false);}}>历史会议</button><button className={searchKind==='feishu'?'active':''} onClick={()=>{setSearchKind('feishu');setResults([]);setSelectedCandidates([]);setSearched(false);}}>飞书文档</button></div>
        <form className="source-search" onSubmit={e=>{e.preventDefault();action('search',async()=>{const d=await api<{items:Candidate[]}>(`/sources/search?q=${encodeURIComponent(sourceQuery)}&kind=${searchKind}&exclude=${selected}`);setResults(d.items);setSearched(true);setSelectedCandidates([]);});}}><input aria-label="资料关键词" placeholder="输入主题关键词" value={sourceQuery} maxLength={searchKind==='feishu'?30:200} onChange={e=>setSourceQuery(e.target.value)}/><button aria-label="检索资料" disabled={!!pending||!sourceQuery.trim()}><Search size={17}/></button></form>
        {searchKind==='feishu'&&!connections?.feishu.ready&&<div className="small-notice">飞书需要用户登录。<button onClick={openSettings}>查看连接</button></div>}
        {searched&&!results.length&&<p className="empty-note">没有找到相关资料，试试其他关键词。</p>}
        {results.map((r,i)=><label className="candidate" key={`${r.origin_id}-${i}`}><input type="checkbox" disabled={!r.supported} checked={selectedCandidates.includes(String(i))} onChange={e=>setSelectedCandidates(e.target.checked?[...selectedCandidates,String(i)]:selectedCandidates.filter(x=>x!==String(i)))}/><div><strong>{r.title}</strong><p>{r.summary.replace(/<[^>]+>/g,'').slice(0,100)}</p>{!r.supported&&<small>此类型仅可查看链接</small>}{r.url&&<a href={r.url} target="_blank" rel="noreferrer">原文 <ExternalLink size={11}/></a>}</div></label>)}
        {!!selectedCandidates.length&&<button className="secondary full" disabled={!!pending} onClick={()=>action('sources',async()=>{for(const i of selectedCandidates){const r=results[Number(i)];await api(`/meetings/${selected}/sources`,r,'POST');}setSelectedCandidates([]);setResults([]);setSearched(false);notify('所选正文已读取并保存为资料快照。');})}><Plus size={15}/> 加入所选资料</button>}
        <div className="source-divider"><span>已保存的资料</span><label className="upload-button"><Upload size={13}/> 导入文字<input type="file" accept=".txt,.md" onChange={e=>{const f=e.target.files?.[0];if(f)action('import',async()=>{const form=new FormData();form.append('file',f);await api(`/meetings/${selected}/import`,form,'POST');notify('已导入，历史列表中也可再次找到。');});e.target.value='';}}/></label></div>
        {meeting.sources.map(s=><button className="source-row" key={s.id} onClick={()=>setSourceDetail(s)}><FileText size={17}/><div><strong>{s.title}</strong><small>{s.auto_discovered?'自动找到 · ':''}{sourceKind(s.kind)} · 已保存快照</small></div><ChevronRight size={14}/></button>)}
        {!meeting.sources.length&&<div className="context-empty"><FolderOpen size={25}/><p>尚无已保存的参考资料</p><small>生成时会自动查找；也可在上方补充。</small></div>}
      </>:<><div className="context-title"><Sparkles size={17}/><h3>会中提示</h3><span>AI</span></div><p className="muted">留住重点，也给想法一点空间。</p><label className="toggle"><input type="checkbox" checked={!!meeting.auto_analysis} onChange={e=>action('auto',()=>api(`/meetings/${selected}`,{auto_analysis:e.target.checked},'PATCH'))}/><span>自动分析</span><small>30秒 / 新增80字</small></label>
        <button className="secondary full" disabled={!!pending||generating('insights')||!meeting.segments.length} onClick={()=>startGenerate('insights')}>{generating('insights')?<LoaderCircle className="spin" size={15}/>:<RefreshCw size={15}/>} {generating('insights')?'正在分析':'立即分析'}</button>
        {job('insights')?.state==='failed'&&<div className="small-notice error">{job('insights')?.error}</div>}
        {(historyInsights?meeting.insights:[...meeting.insights].reverse().filter(i=>i.status!=='ignored').slice(0,3)).map(i=><article className={'insight '+(i.kind==='重点'?'kind-key':i.kind==='灵感'?'kind-idea':'kind-question')+' '+(i.status==='ignored'?'ignored':'')} key={i.id}><div className="insight-type">{i.kind==='灵感'?<Lightbulb size={15}/>:i.kind==='重点'?<Bookmark size={15}/>:<MessageCircleQuestion size={15}/>}<span>{i.kind}{i.kind==='灵感'?' · AI 建议':''}</span>{i.status==='kept'&&<Check size={14}/>}</div><p>{i.text}</p>{i.transcript_version<meeting.last_edit_version&&<small className="stale">文字已修改，需重新核对</small>}<div className="insight-quotes">{i.refs.map(ref=>{const segment=meeting.segments.find(s=>s.id===ref);return <button key={ref} onClick={()=>sourceClick(ref)}>{segment?`原文 #${String(segment.ordinal+1).padStart(2,'0')}${meeting.state==='imported'?'':` · 约 ${time(segment.start)}`}`:'参考资料'} <ArrowUpRight size={12}/></button>;})}</div><div className="insight-actions"><button onClick={()=>action('insight',()=>api(`/meetings/${selected}/insights/${i.id}`,{status:i.status==='kept'?'new':'kept'},'PATCH'))}>{i.status==='kept'?'取消保留':'保留'}</button><button onClick={()=>action('insight',()=>api(`/meetings/${selected}/insights/${i.id}`,{status:i.status==='ignored'?'new':'ignored'},'PATCH'))}>{i.status==='ignored'?'恢复':'忽略'}</button></div></article>)}
        {!meeting.insights.length&&<div className="context-empty"><Lightbulb size={28}/><p>灵感会在这里出现</p><small>积累一段讨论后，提炼重点、待澄清问题和可选思路。</small></div>}
        {!!meeting.insights.length&&<button className="text-button" onClick={()=>setHistoryInsights(!historyInsights)}>{historyInsights?'仅看当前提示':`查看全部 ${meeting.insights.length} 条提示`}</button>}
      </>}
      <div className="context-footnote"><Link2 size={13}/><span>事实有原文，建议有标识。</span></div></aside></div>
      <div className={'record-dock state-'+meeting.state} role="region" aria-label="会议记录控制"><div className="record-summary"><div className="record-emblem">{isActive(meeting)?<Radio size={23}/>:meeting.state==='ready'?<Mic size={22}/>:meeting.state==='interrupted'?<Flag size={22}/>:<Check size={22}/>}</div><div className="record-copy"><strong>{isActive(meeting)?time(live.seconds):meeting.state==='ready'?'准备开始记录':meeting.state==='interrupted'?'会议记录已中断':meeting.state==='imported'?'已导入文字记录':'会议记录已保存'}</strong><small>{isActive(meeting)?meeting.state==='finalizing'?'正在补齐尾音并保存，请稍候':meeting.state==='starting'?'正在准备本地识别环境':'后台持续记录 · 文字随讲话保存':meeting.state==='ready'?(device<0?'请先在设置中选择可用麦克风':'使用电脑麦克风 · 普通话转写'):meeting.state==='interrupted'?'仅保留实际恢复的内容，请核对记录缺口':`${meeting.segments.length} 个文字片段 · ${meeting.audio_available?'包含本地录音':'保存在本机'}`}</small></div></div>
        {isActive(meeting)&&<div className="dock-meter"><Volume2 size={16}/><div className="meter" aria-label={`收音音量 ${Math.round(live.level||0)}`}><span style={{transform:`scaleX(${Math.max(.02,(live.level||0)/100)})`}}/></div></div>}
        <div className="record-controls"><button className="icon record-settings" aria-label="录音设备与连接设置" onClick={openSettings}><Settings size={18}/></button>{isActive(meeting)?<button className="stop" disabled={meeting.state==='finalizing'||recordPending} onClick={()=>recordingAction('stop',()=>api(`/meetings/${selected}/stop`,{},'POST'))}>{meeting.state==='finalizing'?<LoaderCircle className="spin" size={15}/>:<Square size={13} fill="currentColor"/>}{meeting.state==='finalizing'?'正在保存':'结束会议'}</button>:meeting.state==='ready'?<button className="primary" disabled={recordPending||device<0||otherRecording} onClick={()=>recordingAction('start',async()=>{await api(`/meetings/${selected}/start`,{device},'POST');setTab('live');})}><Mic size={16}/> 开始记录</button>:<span className="saved-label"><Check size={15}/>{meeting.state==='interrupted'?'已恢复部分内容':'已保存到本机'}</span>}</div></div>
      </>}
    </main>
    {message&&<div className="toast" role="status"><span>{message}</span><button className="icon" onClick={()=>setMessage('')} aria-label="关闭通知"><X size={16}/></button></div>}
    {(showNew||showEdit)&&<Modal title={showNew?'创建一场新会议':'编辑会议信息'} close={()=>{setShowNew(false);setShowEdit(false);}}><form onSubmit={(e:FormEvent)=>{e.preventDefault();action('meeting',async()=>{const fields={title,goal,counterparty_name:counterpartyName.trim(),counterparty_website:counterpartyWebsite.trim()};if(showNew){const d=await api<{id:string}>('/meetings',fields,'POST');choose(d.id);}else await api('/meetings/'+selected,fields,'PATCH');setShowNew(false);setShowEdit(false);});}}><label>会议主题<input autoFocus required maxLength={200} placeholder="这次准备讨论什么？" value={title} onChange={e=>setTitle(e.target.value)}/></label><label>会议目标 <span className="muted">选填</span><textarea rows={4} placeholder="希望解决的问题、需要做出的决定……" value={goal} onChange={e=>setGoal(e.target.value)}/></label><label>对方公司全称／名称 <span className="muted">选填</span><input maxLength={200} placeholder="明确本次沟通的公司或机构" value={counterpartyName} onChange={e=>setCounterpartyName(e.target.value)}/></label><label>对方官网 <span className="muted">选填</span><input type="url" maxLength={2000} placeholder="https://…" value={counterpartyWebsite} onChange={e=>setCounterpartyWebsite(e.target.value)}/></label><p className="meeting-website-help">用于核对同名公司；填写官网可提高公开调研命中。</p><div className="modal-actions"><button type="button" onClick={()=>{setShowNew(false);setShowEdit(false);}}>取消</button><button className="primary" disabled={!!pending||!title.trim()}>{showNew?'创建会议':'保存修改'}<ChevronRight size={16}/></button></div></form></Modal>}
    {showSettings&&<Modal title="设置与连接" close={()=>setShowSettings(false)} initialFocus="close">
      <CompanyProfileSettings loadProfile={()=>api<CompanyProfile>('/company-profile')} saveProfile={value=>api<CompanyProfile>('/company-profile',value,'PUT')} onSaved={profile=>{setMeeting(previous=>previous?{...previous,company_profile_version:profile.version,company_profile_confirmed:profile.confirmed}:previous);if(current.current)void load(current.current);}}/>
      <CloudModelSettings onLoaded={value=>{connectionsRequest.current++;setConnections(value);}} onSaved={value=>{connectionsRequest.current++;setConnections(old=>old?{...old,llm:value}:old);}}/>
      <div className="settings-section"><h3><Mic size={17}/> 麦克风</h3><label>输入设备<select value={device} onChange={e=>setDevice(Number(e.target.value))}>{!devices.length&&<option value={-1}>没有可用设备</option>}{devices.map(d=><option value={d.index} key={d.index}>{d.name} · {d.hostapi}{d.default?'（默认）':''}</option>)}</select></label><button onClick={refreshDevices}><RefreshCw size={14}/> 刷新设备</button><p className="muted">仅在点击“开始记录”后开启麦克风。当前识别环境：{connections?.asr.available?'可用':'未找到'}</p></div>
      <div className="settings-section"><h3><FileText size={17}/> 飞书文档 <span className="badge">{connections?.feishu.ready?'用户身份可用':'待连接'}</span></h3><p className="muted">{connections?.feishu.message||'正在检查连接……'}</p><p>在本机终端执行以下命令，并按提示完成用户授权：</p><code>{connections?.feishu.login_command||'lark-cli auth login --domain docs --domain drive'}</code><button onClick={refreshConnections}><RefreshCw size={14}/> 重新检查连接</button></div>
    </Modal>}
    {sourceDetail&&<Modal title={sourceDetail.title} close={()=>setSourceDetail(null)}><p className="muted">{sourceDetail.auto_discovered?'自动找到 · ':''}{sourceKind(sourceDetail.kind)} · 资料快照 · {new Date(sourceDetail.created_at).toLocaleString('zh-CN')}</p>{sourceDetail.url&&<a href={sourceDetail.url} target="_blank" rel="noreferrer">{sourceDetail.kind==='feishu'?'打开飞书原文':'打开网页原文'} <ExternalLink size={14}/></a>}{sourceMeetingId&&sourceMeetingId!==selected&&<button onClick={()=>{choose(sourceMeetingId);setSourceDetail(null);}}>{sourceDetail.kind==='history'?'打开历史会议':'查看资料保存位置'} <ArrowUpRight size={14}/></button>}<article className="source-content">{md(sourceDetail.content)}</article></Modal>}
    {editingSegment&&<Modal title="修正文字片段" close={()=>setEditingSegment(null)}><p className="muted">{meeting?.state==='imported'?'导入文字，原始内容会保留。':`约 ${time(editingSegment.start)}—${time(editingSegment.end)}，原始转写会保留。`}</p><label>修正后的文字<textarea rows={7} value={segmentText} onChange={e=>setSegmentText(e.target.value)}/></label><details><summary>查看原始转写</summary><p>{editingSegment.raw_text}</p></details><div className="modal-actions"><button onClick={()=>setEditingSegment(null)}>取消</button><button className="primary" disabled={!!pending} onClick={()=>action('edit',async()=>{await api(`/meetings/${selected}/segments/${editingSegment.id}`,{text:segmentText},'PATCH');setEditingSegment(null);})}>保存修正</button></div></Modal>}
    {editingDoc&&<Modal title="编辑文档 · Markdown" close={()=>setEditingDoc(null)}><label>文档内容<textarea className="document-editor" rows={18} value={docText} onChange={e=>setDocText(e.target.value)}/></label><div className="modal-actions"><button onClick={()=>setEditingDoc(null)}>取消</button><button className="primary" disabled={!!pending} onClick={()=>action('document',async()=>{await api(`/meetings/${selected}/documents/${editingDoc.id}`,{content:docText},'PATCH');setEditingDoc(null);})}>保存修改</button></div></Modal>}
  </div>;
}

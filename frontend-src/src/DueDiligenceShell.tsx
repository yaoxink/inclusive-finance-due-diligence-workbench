import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import type {FormEvent, ReactNode} from 'react';
import {AlertCircle, ArrowRight, BookOpen, Calculator, Check, ChevronRight, ClipboardList,
  FileCheck2, FileText, FolderOpen, Layers3, LoaderCircle, LockKeyhole, Mic, Plus,
  RefreshCw, ShieldCheck, SquarePen, Users, X, Square, Pause, Play, Headphones, Radio, Upload} from 'lucide-react';
import './DueDiligenceShell.css';
import S3Workspace from './S3Workspace';
import ReportsWorkspace from './ReportsWorkspace';
import CalculationPanel from './CalculationPanel';
import RetentionPanel from './RetentionPanel';
import MaintenancePanel from './MaintenancePanel';
import QuestionnaireAdmin from './QuestionnaireAdmin';
import AnswerBoard from './AnswerBoard';
import CustomerInfoCard from './CustomerInfoCard';
import CreditSummary from './CreditSummary';
import CaseHeaderBadges from './CaseHeaderBadges';

type CaptureState='starting'|'recording'|'stopping'|'saved'|'interrupted'|'failed';
type AsrState='not_started'|'loading'|'running'|'draining'|'completed'|'failed'|'disabled';
type Recording={id:string;case_id:string;interview_id:string;device_id:number;variant?:'mandarin'|'sichuan';state:CaptureState;capture_state:CaptureState;
  saved_frames:number;saved_seconds:number;sample_rate:number;started_at:string|null;stopped_at:string|null;last_saved_at?:string|null;
  audio_available:boolean;error?:string|null;data_error?:string|null;transcript_is_from_previous_run?:boolean;punctuated_text?:string|null;
  capture_diagnostics?:{unconfirmed_tail_frames_exact?:number|null;unconfirmed_tail_upper_bound_frames?:number|null;
    unsaved_output_frames?:number|null;unsaved_output_frames_upper?:number|null;received_device_frames?:number|null;counters_exact_available?:boolean|null};
  asr:{state:AsrState;message?:string;error?:string;transcribed_frames?:number;transcribed_seconds?:number;
    current_run_transcribed_frames?:number;current_run_transcribed_seconds?:number;checkpoint_run_id?:string|null;retry_queued?:boolean};
  segments:{id:string;ordinal:number;start_seconds:number;end_seconds:number;text:string;stable:boolean;revision?:number;
    source_run_id?:string|null;verified_run_id?:string|null}[];timing:'approximate'};
type Device={id:number;name:string;is_default:boolean;hostapi:string};
type Interview={id:string;case_id:string;title:string;state:string;created_at:string;capture_state:string;asr_state:string;
  recordings?:{id:string;state?:CaptureState;created_at?:string}[];active_recording_id?:string|null};
type Case={id:string;title:string;subject_type:string;product_id:string;status:'preparing';data_class:'synthetic';
  created_at:string;updated_at:string;questionnaire_version:string;model_version:string|null;interviews?:Interview[]};
type Capability={id:string;label:string;status:string;reason:string};
type Health={app:string;version:string;pid:number};
type Policy={local_only:boolean;data_class:string;external_services_enabled:boolean};
type Lease={occupied:boolean;owner:{app_id:string;instance_id:string;session_id:string;worker_pid:number}|null};
type Tab='prepare'|'interview'|'calculate';
const API='/api/due-diligence';
const tabs:{id:Tab;label:string;number:string}[]=[{id:'prepare',label:'准备',number:'01'},{id:'interview',label:'现场',number:'02'},
  {id:'calculate',label:'报告',number:'03'}];
const errorMessage=(value:unknown)=>value instanceof Error?value.message:'操作未完成，请重试。';
const dateLabel=(value:string)=>{const date=new Date(value);return Number.isNaN(date.getTime())?'时间待确认':date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});};
const initialSelection=()=>{try{return localStorage.getItem('due-diligence:selected-case:v1')||'';}catch{return '';}};
const captureActive=(recording:Recording)=>['starting','recording','stopping'].includes(recording.capture_state);
const asrActive=(recording:Recording)=>!!recording.asr.retry_queued||['loading','running','draining'].includes(recording.asr.state);
const canRetryTranscription=(recording:Recording)=>recording.saved_frames>0&&!recording.asr.retry_queued&&['failed','disabled'].includes(recording.asr.state);
const recordingPath=(recording:Pick<Recording,'case_id'|'interview_id'|'id'>)=>`/cases/${encodeURIComponent(recording.case_id)}/interviews/${encodeURIComponent(recording.interview_id)}/recordings/${encodeURIComponent(recording.id)}`;
const captureLabels:Record<CaptureState,string>={starting:'正在启动收音',recording:'正在录音',stopping:'正在停止并保存',saved:'本段已落盘',interrupted:'本段已中断',failed:'录音未完成'};
const asrLabels:Record<AsrState,string>={not_started:'尚未开始',loading:'模型加载中',running:'正在转写',draining:'正在补齐转写',completed:'本段转写已结束',failed:'转写失败',disabled:'本次未启用转写'};
const audioTime=(seconds:number)=>Number.isFinite(seconds)?`${Math.floor(Math.max(0,seconds)/60).toString().padStart(2,'0')}:${(Math.max(0,seconds)%60).toFixed(1).padStart(4,'0')}`:'尚未取得';
const frameCount=(value:number|undefined)=>value==null||!Number.isFinite(value)?'尚未取得':Math.floor(value).toLocaleString('zh-CN');
const asrErrorLabel=(value:string)=>/[\u4e00-\u9fff]/.test(value)?value:'本次转写未完成，请检查本地语音环境或重新转写本段。';
async function api<T>(path:string,options:RequestInit={}):Promise<T>{
  const response=await fetch(API+path,options);
  if(!response.ok){const body=await response.json().catch(()=>null);throw new Error(typeof body?.detail==='string'?body.detail:`本地尽调服务暂未完成请求（${response.status}），请重试。`);}
  return response.json() as Promise<T>;
}
const post=(value:unknown):RequestInit=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
function Notice({children,kind='error'}:{children:ReactNode;kind?:'error'|'info'|'success'}){
  return <div className={'dd-notice '+kind} role={kind==='error'?'alert':'status'}>{kind==='success'?<Check size={16}/>:<AlertCircle size={16}/>}<span>{children}</span></div>;
}
function CaptureRecoveryNotice({recording}:{recording:Recording}){
  if(!['failed','interrupted'].includes(recording.capture_state))return null;
  const exact=recording.capture_diagnostics?.unconfirmed_tail_frames_exact;
  const upper=recording.capture_diagnostics?.unconfirmed_tail_upper_bound_frames;
  const hasExact=typeof exact==='number'&&Number.isFinite(exact)&&exact>=0;
  const hasUpper=typeof upper==='number'&&Number.isFinite(upper)&&upper>=0;
  const validRate=Number.isFinite(recording.sample_rate)&&recording.sample_rate>0;
  const secondsCeil=(frames:number)=>(Math.ceil(frames*100/recording.sample_rate)/100).toFixed(2);
  return <Notice kind="info"><strong>已确认保存：{frameCount(recording.saved_frames)} 帧（{audioTime(recording.saved_seconds)}）。</strong><br/>
    {hasExact?<>未确认尾部计数为 {frameCount(exact)} 帧{validRate?`（换算时长向上保留两位：${secondsCeil(exact)} 秒）`:'，换算时长尚未确认'}。请保留清单并回听核对，不能据此直接认定实际录音损失。</>:
      hasUpper&&validRate?<>未确认尾部确切长度未知；缓存约束估计上限 {secondsCeil(upper)} 秒。这个上限不是实测损失，请保留清单并回听核对。</>:
      <>未确认尾部的确切长度与上限尚未确认，请保留清单核对。</>}
  </Notice>;
}
function EmptyState({icon,title,children}:{icon:ReactNode;title:string;children:ReactNode}){
  return <div className="dd-empty"><span className="dd-empty-icon">{icon}</span><h3>{title}</h3><p>{children}</p></div>;
}
function NewCaseDialog({busy,onClose,onSubmit,error}:{busy:boolean;onClose:()=>void;onSubmit:(title:string)=>void;error:string}){
  const [title,setTitle]=useState('');const dialog=useRef<HTMLDivElement>(null),input=useRef<HTMLInputElement>(null);
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null;input.current?.focus();
    const key=(event:KeyboardEvent)=>{
      if(event.key==='Escape'&&!busy)onClose();
      if(event.key==='Tab'){
        const controls=Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')||[]);
        const first=controls[0],last=controls[controls.length-1];
        if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
        else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
      }
    };
    document.addEventListener('keydown',key);return()=>{document.removeEventListener('keydown',key);previous?.focus();};
  },[busy,onClose]);
  const submit=(event:FormEvent)=>{event.preventDefault();if(title.trim()&&!busy)onSubmit(title.trim());};
  return <div className="dd-overlay" onClick={event=>{if(event.target===event.currentTarget&&!busy)onClose();}}><div className="dd-modal" ref={dialog} role="dialog" aria-modal="true" aria-labelledby="dd-new-case-title">
    <header><div><span className="dd-eyebrow">独立尽调工作空间</span><h2 id="dd-new-case-title">新建尽调案件</h2></div><button type="button" className="dd-icon-button" aria-label="关闭新建案件" onClick={onClose} disabled={busy}><X size={19}/></button></header>
    <p>请输入客户姓名与业务类型，便于归档检索。</p>
    <form onSubmit={submit}><label htmlFor="dd-case-title">案件名称<input id="dd-case-title" ref={input} value={title} maxLength={120} required disabled={busy} autoComplete="off" placeholder="例如：王建国-柑橘种植贷" onChange={event=>setTitle(event.target.value)}/></label>
      <div className="dd-modal-context"><span><Users size={15}/> 农户场景</span><span><ShieldCheck size={15}/> 农户贷款尽调</span></div>
      {error&&<Notice>{error}</Notice>}<footer><button type="button" onClick={onClose} disabled={busy}>取消</button><button className="dd-primary" type="submit" disabled={!title.trim()||busy}>{busy?<LoaderCircle size={15} className="dd-spin"/>:<Plus size={15}/>} {busy?'正在建立':'建立案件'}</button></footer>
    </form></div></div>;
}

export default function DueDiligenceShell(){
  const [cases,setCases]=useState<Case[]>([]),[casesLoaded,setCasesLoaded]=useState(false),[selectedId,setSelectedId]=useState(initialSelection);
  const [currentCase,setCurrentCase]=useState<Case|null>(null),[tab,setTab]=useState<Tab>('prepare'),[selectedInterview,setSelectedInterview]=useState(''),[caseReload,setCaseReload]=useState(0);
  const [health,setHealth]=useState<Health|null>(null),[policy,setPolicy]=useState<Policy|null>(null),[capabilities,setCapabilities]=useState<Capability[]>([]),[lease,setLease]=useState<Lease|null>(null);
  const [loading,setLoading]=useState(true),[caseLoading,setCaseLoading]=useState(false),[loadErrors,setLoadErrors]=useState<string[]>([]),[caseError,setCaseError]=useState('');
  const [showNew,setShowNew]=useState(false),[createError,setCreateError]=useState(''),[busy,setBusy]=useState(''),[actionError,setActionError]=useState(''),[success,setSuccess]=useState('');
  const [devices,setDevices]=useState<Device[]>([]),[deviceId,setDeviceId]=useState<number|null>(null),[devicesLoading,setDevicesLoading]=useState(false),[devicesError,setDevicesError]=useState('');
  const [recordings,setRecordings]=useState<Record<string,Recording>>({}),[selectedRecordingId,setSelectedRecordingId]=useState('');
  const [activeCaptureId,setActiveCaptureId]=useState<string|null>(null),[activeCaptureKnown,setActiveCaptureKnown]=useState(false),[activeStatusError,setActiveStatusError]=useState('');
  const [recordingsLoading,setRecordingsLoading]=useState(false),[recordingsReady,setRecordingsReady]=useState(false),[recordingsError,setRecordingsError]=useState(''),[recordingReload,setRecordingReload]=useState(0);
  const [startPending,setStartPending]=useState(false),[stopPendingId,setStopPendingId]=useState(''),[captureError,setCaptureError]=useState(''),[stopError,setStopError]=useState('');
  const [asrVariant,setAsrVariant]=useState<'mandarin'|'sichuan'>('mandarin');
  // Hermes transcript repair: pure-frontend switch + batched proxy calls.
  // One in-flight batch at a time, each ≤15 segments / ≤2600 chars — the
  // proxy caps 60 segments / 8000 chars per request and the agent contract
  // wants 10~15 segments per call, never per-segment requests. A full
  // transcript (200+ segments) drains chunk by chunk; a failed batch goes
  // back to the queue front and retries after 30s.
  const [repairOn,setRepairOn]=useState(false),[repairedMap,setRepairedMap]=useState<Record<string,string>>({}),
        [repairBusy,setRepairBusy]=useState(false),[repairNote,setRepairNote]=useState(''),
        [repairPending,setRepairPending]=useState(0);
  const repairSeen=useRef<Set<string>>(new Set()),repairQueue=useRef<{id:string;text:string}[]>([]),
        repairBusyRef=useRef(false),repairTimer=useRef<number|undefined>(undefined),
        repairedCount=useRef(0);
  const pumpRepair=useCallback(async()=>{
    if(repairBusyRef.current)return;
    const queue=repairQueue.current;
    if(!queue.length)return;
    const batch:{id:string;text:string}[]=[];let chars=0;
    while(queue.length&&batch.length<15&&chars+queue[0].text.length<=2600){
      const item=queue.shift()!;batch.push(item);chars+=item.text.length;}
    if(!batch.length)batch.push(queue.shift()!); // a single oversized segment still goes alone
    repairBusyRef.current=true;setRepairBusy(true);setRepairPending(queue.length);
    try{
      const controller=new AbortController();
      const timeout=window.setTimeout(()=>controller.abort(),100000); // never wedge the pump on a hung request
      const response=await fetch('/api/due-diligence/transcript-repair',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({segments:batch}),signal:controller.signal});
      window.clearTimeout(timeout);
      if(!response.ok)throw new Error(String(response.status));
      const data=await response.json() as {repaired:{id:string;text:string}[]};
      setRepairedMap(previous=>{const next={...previous};for(const item of data.repaired)next[item.id]=item.text;return next;});
      repairedCount.current+=data.repaired.length;
      setRepairNote(`已整理 ${repairedCount.current} 段`);
    }catch{
      repairQueue.current=[...batch,...repairQueue.current]; // failed batch returns to the queue front
      setRepairPending(repairQueue.current.length);
      setRepairNote('修复服务暂不可用，稍后自动重试；原文字记录不受影响。');
      repairBusyRef.current=false;setRepairBusy(false);
      if(repairTimer.current)window.clearTimeout(repairTimer.current);
      repairTimer.current=window.setTimeout(()=>void pumpRepair(),30000);
      return;
    }
    repairBusyRef.current=false;setRepairBusy(false);
    if(repairQueue.current.length)void pumpRepair(); // drain the next chunk immediately
  },[]);
  const scheduleRepair=useCallback(()=>{
    if(repairTimer.current)window.clearTimeout(repairTimer.current);
    if(repairQueue.current.length>=10){void pumpRepair();return;} // reach the design batch size first
    repairTimer.current=window.setTimeout(()=>void pumpRepair(),30000); // idle 30s then flush the small tail
  },[pumpRepair]);
  const [pollErrors,setPollErrors]=useState<Record<string,string>>({}),[pauseRecordingId,setPauseRecordingId]=useState(''),[playbackError,setPlaybackError]=useState('');
  const [retryPending,setRetryPending]=useState<Record<string,boolean>>({}),[retryErrors,setRetryErrors]=useState<Record<string,string>>({});
  const mounted=useRef(false),caseRequest=useRef(0),selection=useRef(selectedId),mutation=useRef(false);
  const deletedCaseIds=useRef(new Set<string>());
  const captureStartRequest=useRef(false),captureStopRequests=useRef(new Set<string>()),recordingRequestSeq=useRef(0),recordingAccepted=useRef<Record<string,number>>({});
  const transcriptionRetryRequests=useRef(new Set<string>());
  const audioPlayer=useRef<HTMLAudioElement>(null),deviceRequestSeq=useRef(0),listRequestSeq=useRef(0);
  const activeAcceptedSequence=useRef(0);
  const casePanel=useRef<HTMLDivElement>(null);
  useEffect(()=>{casePanel.current?.scrollTo({top:0,left:0});},[tab,currentCase?.id]);
  // one-click limit generation lives HERE (never unmounts), the report page only renders
  const [limitGenerating,setLimitGenerating]=useState(false),[limitStep,setLimitStep]=useState(0),[limitError,setLimitError]=useState('');
  async function runLimitGeneration(){
    if(!currentCase||limitGenerating)return;
    setLimitGenerating(true);setLimitError('');setLimitStep(0);
    const steps=['正在带入确认答案…','正在运行评分模型…','正在汇总生成额度…'];
    const ticker=window.setInterval(()=>setLimitStep(previous=>Math.min(previous+1,steps.length-1)),1400);
    const base=`/cases/${encodeURIComponent(currentCase.id)}`;
    try{
      // 0) the model needs the prepared-snapshot frame; load the synthetic one if missing
      const diligence=await api<{previsit:{state?:string}}>(`${base}/diligence`);
      if(diligence.previsit?.state!=='available'){
        await api(`${base}/previsit/load`,post({fixture_id:'SYN-S0-001',variant:'base'}));
      }
      // 1) material/registry side first: the synthetic example lays down the
      //    scope frame (accounting period, subjects) that answer alignment needs
      const filled=await api<{draft:{revision:number}}>(`${base}/calculation/v2/fill-from-example`,post({overwrite:true}));
      // 2) questionnaire answers overlay the material defaults, then run
      const applied=await api<{draft:{revision:number}}>(`${base}/calculation/v2/from-answers`,post({}));
      await api(`${base}/calculation/v2/run`,post({expected_revision:applied.draft.revision}));
    }catch(exc){setLimitError(exc instanceof Error?exc.message:'生成额度失败，请重试。');}
    finally{window.clearInterval(ticker);setLimitGenerating(false);
      window.dispatchEvent(new Event('due-diligence:limit-generated'));}
  }
  useEffect(()=>{
    const goReport=()=>setTab('calculate');
    const goGenerate=()=>{setTab('calculate');void runLimitGeneration();};
    window.addEventListener('due-diligence:go-report',goReport);
    window.addEventListener('due-diligence:go-generate',goGenerate);
    return ()=>{window.removeEventListener('due-diligence:go-report',goReport);
                window.removeEventListener('due-diligence:go-generate',goGenerate);};
  },[currentCase,limitGenerating]);
  const knownRecordings=useRef(recordings);knownRecordings.current=recordings;
  const allowed=health?.app==='due-diligence'&&policy?.local_only===true&&policy?.data_class==='synthetic'&&policy?.external_services_enabled===false;
  const interviews=currentCase?.interviews||[];
  const interview=interviews.find(item=>item.id===selectedInterview)||interviews[0];
  const currentRecordings=Object.values(recordings).filter(item=>item.case_id===currentCase?.id&&item.interview_id===interview?.id)
    .sort((a,b)=>(b.started_at||'').localeCompare(a.started_at||''));
  const recording=currentRecordings.find(item=>item.id===selectedRecordingId)||currentRecordings[0];
  const activeCapture=activeCaptureId?recordings[activeCaptureId]:!activeCaptureKnown?Object.values(recordings).find(captureActive):undefined;
  const activeRecordingKey=Object.values(recordings).filter(item=>captureActive(item)||asrActive(item)||retryPending[item.id])
    .map(item=>`${item.case_id}:${item.interview_id}:${item.id}`).sort().join('|');
  const canPlay=!!recording?.audio_available&&!captureActive(recording);
  const stableSegments=recording?.segments.filter(segment=>segment.stable&&segment.text.trim())||[];
  // Repaired transcript reads as continuous prose: collapse all whitespace and
  // agent-side line breaks, then wrap a new line only at speaker turns
  // (调查员：/农户：, and 问：/答： if the agent uses that style).
  const repairedLines=useMemo(()=>{
    const stream=stableSegments.filter(segment=>repairedMap[segment.id])
      .map(segment=>String(repairedMap[segment.id]).replace(/\s+/g,'').replace(/〔[?？]〕/g,'')).join('');
    return stream?stream.split(/(?=调查员[:：]|农户[:：]|问[:：]|答[:：])/):[];
  },[stableSegments,repairedMap]);
  useEffect(()=>{
    if(!repairOn)return;
    const fresh=stableSegments.filter(segment=>!repairSeen.current.has(segment.id)&&segment.text.length<=2000);
    for(const segment of fresh)repairSeen.current.add(segment.id);
    if(fresh.length){
      repairQueue.current=[...repairQueue.current,...fresh.map(segment=>({id:segment.id,text:segment.text}))];
      setRepairPending(repairQueue.current.length);
    }
    if(repairQueue.current.length)scheduleRepair(); // resume after re-toggle even without new segments
    return ()=>{if(repairTimer.current)window.clearTimeout(repairTimer.current);};
  },[stableSegments,repairOn,scheduleRepair]);
  const pendingTranscriptCount=recording?.segments.filter(segment=>!segment.stable&&segment.text.trim()).length||0;
  const currentTranscribedSeconds=recording?.asr.current_run_transcribed_seconds??
    (recording?.asr.current_run_transcribed_frames==null?recording?.asr.transcribed_seconds:recording.asr.current_run_transcribed_frames/recording.sample_rate);
  const s3StableSegments=Object.values(recordings).filter(item=>item.case_id===currentCase?.id).flatMap(item=>item.segments
    .filter(segment=>segment.stable&&segment.text.trim()).map(segment=>({recording_id:item.id,interview_id:item.interview_id,
      segment_id:segment.id,segment_revision:segment.revision??1,text:segment.text,start_seconds:segment.start_seconds,end_seconds:segment.end_seconds})));
  const canStart=allowed&&!!interview&&recordingsReady&&!recordingsLoading&&activeCaptureKnown&&!activeCapture&&!startPending&&deviceId!==null&&!devicesLoading&&lease!==null&&!lease.occupied;
  const mergeRecordings=useCallback((incoming:Recording[],requestSequence:number)=>{
    if(!mounted.current)return;
    const accepted=incoming.filter(item=>!deletedCaseIds.current.has(item.case_id)&&requestSequence>=(recordingAccepted.current[item.id]||0));
    accepted.forEach(item=>{recordingAccepted.current[item.id]=requestSequence;});
    if(accepted.length)setRecordings(previous=>({...previous,...Object.fromEntries(accepted.map(item=>[item.id,item]))}));
  },[]);
  const acceptActive=useCallback((value:Recording|null,requestSequence:number)=>{
    if(!mounted.current||requestSequence<activeAcceptedSequence.current)return;
    activeAcceptedSequence.current=requestSequence;setActiveCaptureKnown(true);setActiveCaptureId(value?.id||null);setActiveStatusError('');
    // The global endpoint discovers ownership and unknown recordings. Known recordings
    // are updated by their detail endpoint, so a parallel ownership read cannot regress ASR progress.
    if(value&&!knownRecordings.current[value.id])mergeRecordings([value],requestSequence);
  },[mergeRecordings]);
  const refreshActive=useCallback(async()=>{
    const requestSequence=++recordingRequestSeq.current;
    try{const value=await api<{recording:Recording|null}>('/capture/active');acceptActive(value.recording,requestSequence);}
    catch{if(mounted.current&&requestSequence>=activeAcceptedSequence.current){setActiveCaptureKnown(false);setActiveStatusError('活动录音状态暂未确认，不能据此认定录音已停止。请刷新或对已知录音请求停止。');}}
  },[acceptActive]);
  const refreshLease=useCallback(async()=>{try{const value=await api<Lease>('/capture/lease');if(mounted.current)setLease(value);}catch{if(mounted.current)setLease(null);}},[]);
  const refreshDevices=useCallback(async()=>{
    const ticket=++deviceRequestSeq.current;setDevicesLoading(true);setDevicesError('');
    try{const value=await api<{devices:Device[];default_device_id:number|null}>('/capture/devices');
      if(!mounted.current||ticket!==deviceRequestSeq.current)return;setDevices(value.devices);
      setDeviceId(previous=>value.devices.some(item=>item.id===previous)?previous:value.devices.some(item=>item.id===value.default_device_id)?value.default_device_id:value.devices[0]?.id??null);
    }catch(error){if(mounted.current&&ticket===deviceRequestSeq.current){setDevicesError(errorMessage(error));setDevices([]);setDeviceId(null);}}
    finally{if(mounted.current&&ticket===deviceRequestSeq.current)setDevicesLoading(false);}
  },[]);
  const closeNew=useCallback(()=>{setShowNew(false);setCreateError('');},[]);
  const updateList=(value:Case)=>setCases(previous=>[value,...previous.filter(item=>item.id!==value.id)].sort((a,b)=>b.updated_at.localeCompare(a.updated_at)));
  const loadWorkspace=useCallback(async(signal?:AbortSignal)=>{
    setLoading(true);setLoadErrors([]);const activeSequence=++recordingRequestSeq.current;
    const results=await Promise.allSettled([api<Health>('/health',{signal}),api<{capabilities:Capability[]}>('/capabilities',{signal}),
      api<Policy>('/runtime-policy',{signal}),api<{cases:Case[]}>('/cases',{signal}),api<Lease>('/capture/lease',{signal}),api<{recording:Recording|null}>('/capture/active',{signal})]);
    if(!mounted.current||signal?.aborted)return;
    const errors:string[]=[];const [h,c,p,list,lock,globalCapture]=results;
    if(h.status==='fulfilled')setHealth(h.value);else{setHealth(null);errors.push('未能连接本地尽调服务。');}
    if(c.status==='fulfilled')setCapabilities(c.value.capabilities);else errors.push('功能状态尚未读取，请刷新。');
    if(p.status==='fulfilled')setPolicy(p.value);else{setPolicy(null);errors.push('未能确认当前数据使用模式。');}
    if(list.status==='fulfilled'){
      const visible=list.value.cases.filter(item=>!deletedCaseIds.current.has(item.id));
      setCases(visible);setCasesLoaded(true);
      setSelectedId(previous=>visible.some(item=>item.id===previous)?previous:visible[0]?.id||'');
    }else errors.push('案件列表暂未读取，请重试。');
    if(lock.status==='fulfilled')setLease(lock.value);else{setLease(null);errors.push('录音占用状态尚未确认。');}
    if(globalCapture.status==='fulfilled')acceptActive(globalCapture.value.recording,activeSequence);
    else if(activeSequence>=activeAcceptedSequence.current){setActiveCaptureKnown(false);setActiveStatusError('尚未确认全局录音状态，请刷新后再开始录音。');}
    setLoadErrors(errors);setLoading(false);
  },[acceptActive]);
  useEffect(()=>{mounted.current=true;const controller=new AbortController();void loadWorkspace(controller.signal);return()=>{mounted.current=false;controller.abort();caseRequest.current++;};},[loadWorkspace]);
  useEffect(()=>{
    selection.current=selectedId;const ticket=++caseRequest.current,controller=new AbortController();
    setCurrentCase(null);setCaseError('');setSelectedInterview('');setActionError('');setSuccess('');
    if(!selectedId){setCaseLoading(false);return()=>controller.abort();}
    try{localStorage.setItem('due-diligence:selected-case:v1',selectedId);}catch{/* Selection is optional; case persistence belongs to the service. */}
    setCaseLoading(true);
    void api<Case>('/cases/'+encodeURIComponent(selectedId),{signal:controller.signal}).then(value=>{
      if(!mounted.current||controller.signal.aborted||ticket!==caseRequest.current)return;
      if(value.id!==selectedId||value.data_class!=='synthetic')throw new Error('案件身份或数据模式不符，本次未打开案件。');
      setCurrentCase(value);setSelectedInterview(value.interviews?.find(item=>item.active_recording_id)?.id||value.interviews?.[0]?.id||'');
    }).catch(error=>{if(mounted.current&&!controller.signal.aborted&&ticket===caseRequest.current)setCaseError(errorMessage(error));})
      .finally(()=>{if(mounted.current&&!controller.signal.aborted&&ticket===caseRequest.current)setCaseLoading(false);});
    return()=>controller.abort();
  },[selectedId,caseReload]);
  useEffect(()=>{if(tab==='interview'&&currentCase)void refreshDevices();},[tab,currentCase?.id,refreshDevices]);
  useEffect(()=>{
    const controller=new AbortController(),ticket=++listRequestSeq.current,requestSequence=++recordingRequestSeq.current;
    setRecordingsReady(false);setRecordingsError('');setCaptureError('');setPlaybackError('');
    if(!currentCase||!interview){setRecordingsLoading(false);setSelectedRecordingId('');return()=>controller.abort();}
    const caseId=currentCase.id,interviewId=interview.id;setRecordingsLoading(true);
    void api<{recordings:Recording[];active_recording_id:string|null}>(`/cases/${encodeURIComponent(caseId)}/interviews/${encodeURIComponent(interviewId)}/recordings`,{signal:controller.signal}).then(value=>{
      if(!mounted.current||controller.signal.aborted||ticket!==listRequestSeq.current)return;
      if(value.recordings.some(item=>item.case_id!==caseId||item.interview_id!==interviewId))throw new Error('录音归属不符，本次未加载录音列表。');
      mergeRecordings(value.recordings,requestSequence);setRecordingsReady(true);
      const activeItem=value.recordings.find(item=>item.id===value.active_recording_id);
      if(activeItem)acceptActive(activeItem,requestSequence);
      const ordered=[...value.recordings].sort((a,b)=>(b.started_at||'').localeCompare(a.started_at||''));
      setSelectedRecordingId(previous=>value.recordings.some(item=>item.id===previous)?previous:value.active_recording_id||ordered[0]?.id||'');
    }).catch(error=>{if(mounted.current&&!controller.signal.aborted&&ticket===listRequestSeq.current)setRecordingsError(errorMessage(error));})
      .finally(()=>{if(mounted.current&&!controller.signal.aborted&&ticket===listRequestSeq.current)setRecordingsLoading(false);});
    return()=>controller.abort();
  },[currentCase?.id,interview?.id,recordingReload,mergeRecordings,acceptActive]);
  useEffect(()=>{
    if(!activeRecordingKey)return;
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      const scopes=activeRecordingKey.split('|').map(value=>{const [case_id,interview_id,id]=value.split(':');return {case_id,interview_id,id};});
      const detailRequests=scopes.map(async scope=>{
        const requestSequence=++recordingRequestSeq.current;
        const value=await api<Recording>(recordingPath(scope),{signal:controller.signal});
        if(value.id!==scope.id||value.case_id!==scope.case_id||value.interview_id!==scope.interview_id)throw new Error('录音归属不符，已忽略本次状态。');
        return {value,requestSequence};
      });
      const activeSequence=++recordingRequestSeq.current;
      const [responses,globalCapture]=await Promise.all([Promise.allSettled(detailRequests),
        api<{recording:Recording|null}>('/capture/active',{signal:controller.signal}).then(value=>({ok:true as const,value})).catch(()=>({ok:false as const}))]);
      if(controller.signal.aborted||!mounted.current)return;
      setPollErrors(previous=>{const next={...previous};responses.forEach((response,index)=>{
        if(response.status==='fulfilled'){delete next[scopes[index].id];}else next[scopes[index].id]='状态更新暂时中断，录音可能仍在继续。保留上次确认的状态，请刷新或请求停止。';
      });return next;});
      responses.forEach(response=>{if(response.status==='fulfilled')mergeRecordings([response.value.value],response.value.requestSequence);});
      if(globalCapture.ok)acceptActive(globalCapture.value.recording,activeSequence);
      else if(activeSequence>=activeAcceptedSequence.current){setActiveCaptureKnown(false);setActiveStatusError('活动录音状态更新中断；请保留上次状态，必要时请求停止并刷新。');}
      timer=setTimeout(poll,1000);
    };
    timer=setTimeout(poll,1000);return()=>{controller.abort();clearTimeout(timer);};
  },[activeRecordingKey,mergeRecordings,acceptActive]);
  useEffect(()=>{setPlaybackError('');},[recording?.id]);
  useEffect(()=>{void refreshLease();},[activeCapture?.id,activeCapture?.capture_state,refreshLease]);
  useEffect(()=>{if(activeCaptureKnown&&!activeCaptureId)setStopError('');},[activeCaptureKnown,activeCaptureId]);
  useEffect(()=>{if(activeCapture&&showNew)setShowNew(false);},[activeCapture?.id,showNew]);
  async function createCase(title:string){
    if(mutation.current||!allowed)return;mutation.current=true;setBusy('create');setCreateError('');
    try{const value=await api<Case>('/cases',post({title,subject_type:'farmer',product_id:'farmer_demo'}));
      if(!mounted.current)return;updateList(value);setCasesLoaded(true);setSelectedId(value.id);setTab('prepare');setShowNew(false);setSuccess('尽调案件已建立。');
    }catch(error){if(mounted.current)setCreateError(errorMessage(error));}
    finally{mutation.current=false;if(mounted.current)setBusy('');}
  }
  async function createInterview(){
    if(!currentCase||mutation.current||!allowed)return;
    const caseId=currentCase.id;mutation.current=true;setBusy('interview');setActionError('');setSuccess('');
    try{const interview=await api<Interview>('/cases/'+encodeURIComponent(caseId)+'/interviews',post({title:`访谈记录 ${(currentCase.interviews?.length||0)+1}`}));
      if(!mounted.current||selection.current!==caseId)return;
      setCurrentCase(previous=>previous?.id===caseId?{...previous,interviews:[interview,...(previous.interviews||[]).filter(item=>item.id!==interview.id)]}:previous);
      setSelectedInterview(interview.id);setTab('interview');setSuccess('访谈记录已建立；开始录音后，音频与转写会分别记录状态。');
    }catch(error){if(mounted.current&&selection.current===caseId)setActionError(errorMessage(error));}
    finally{mutation.current=false;if(mounted.current)setBusy('');}
  }
// Any audio file (mp3/m4a/wav/…) → 16 kHz mono PCM16 WAV, fully in-browser via
// WebAudio decode + OfflineAudioContext resample; the import API stays WAV-only.
async function toWav16kMono(file:File):Promise<Blob>{
  const arrayBuffer=await file.arrayBuffer();
  const AudioCtx=window.AudioContext||(window as unknown as {webkitAudioContext:typeof AudioContext}).webkitAudioContext;
  const decodeCtx=new AudioCtx();
  let decoded:AudioBuffer;
  try{decoded=await decodeCtx.decodeAudioData(arrayBuffer);}
  finally{void decodeCtx.close();}
  const frames=Math.max(1,Math.ceil(decoded.duration*16000));
  const offline=new OfflineAudioContext(1,frames,16000);
  const source=offline.createBufferSource();source.buffer=decoded;source.connect(offline.destination);source.start();
  const rendered=await offline.startRendering();
  const samples=rendered.getChannelData(0);
  const pcm=new Int16Array(samples.length);
  for(let index=0;index<samples.length;index++){
    const clamped=Math.max(-1,Math.min(1,samples[index]));
    pcm[index]=clamped<0?clamped*0x8000:clamped*0x7FFF;
  }
  const header=new ArrayBuffer(44),view=new DataView(header);
  const writeText=(offset:number,text:string)=>{for(let i=0;i<text.length;i++)view.setUint8(offset+i,text.charCodeAt(i));};
  writeText(0,'RIFF');view.setUint32(4,36+pcm.length*2,true);writeText(8,'WAVE');
  writeText(12,'fmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);
  view.setUint32(24,16000,true);view.setUint32(28,32000,true);view.setUint16(32,2,true);view.setUint16(34,16,true);
  writeText(36,'data');view.setUint32(40,pcm.length*2,true);
  return new Blob([header,pcm.buffer],{type:'audio/wav'});
}

  async function importRecording(file:File){
    if(!allowed||!!busy)return;
    if(!interview||!currentCase){setCaptureError('录音必须挂在访谈下：请先点现场页左上「+ 建立访谈记录」，再导入录音。');return;}
    setBusy('import');setCaptureError('');setSuccess('');
    try{
      const wav=await toWav16kMono(file); // mp3/m4a/wav all normalize to 16k mono WAV
      const response=await fetch(API+'/cases/'+encodeURIComponent(currentCase.id)+'/interviews/'+encodeURIComponent(interview.id)+'/recordings/import',{
        method:'POST',headers:{'Content-Type':'audio/wav','X-Import-Filename':encodeURIComponent(file.name.replace(/\.[^.]+$/,'')+'.wav')},body:wav});
      const body=await response.json().catch(()=>null);
      if(!response.ok)throw new Error((body&&body.detail&&(body.detail.message||String(body.detail)))||'导入失败。');
      setSuccess('已导入录音（'+audioTime(body.saved_seconds)+'）；已排队转写。');
      setSelectedInterview(interview.id);setSelectedRecordingId(body.id);
      await refreshRecordingState();
      await fetch(API+'/cases/'+encodeURIComponent(currentCase.id)+'/interviews/'+encodeURIComponent(interview.id)+'/recordings/'+body.id+'/transcription/retry',
        {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
      setSuccess('已导入录音（'+audioTime(body.saved_seconds)+'）；已排队转写，稍后刷新查看文字。');
    }catch(error){setCaptureError('导入失败：'+(error instanceof Error?error.message:'无法解码该音频文件。'));}
    finally{setBusy('');}
  }
  async function startRecording(){
    if(!currentCase||!interview||deviceId===null||!canStart||captureStartRequest.current)return;
    const caseId=currentCase.id,interviewId=interview.id;captureStartRequest.current=true;setStartPending(true);setCaptureError('');setSuccess('');
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000),requestSequence=++recordingRequestSeq.current;
    try{
      const value=await api<Recording>(`/cases/${encodeURIComponent(caseId)}/interviews/${encodeURIComponent(interviewId)}/recordings/start`,{...post({device_id:deviceId,variant:asrVariant}),signal:controller.signal});
      if(!mounted.current)return;
      if(value.case_id!==caseId||value.interview_id!==interviewId)throw new Error('录音归属不符，请刷新当前案件确认录音状态。');
      mergeRecordings([value],requestSequence);acceptActive(captureActive(value)?value:null,requestSequence);setSelectedRecordingId(value.id);setPauseRecordingId('');
      setSuccess('已收到录音任务状态，请以收音与实际落盘进度为准。');
    }catch(error){if(mounted.current){setCaptureError(error instanceof Error&&error.name==='AbortError'?'开始请求尚未确认，录音可能已启动。请刷新录音状态，避免重复开录。':errorMessage(error));setRecordingReload(previous=>previous+1);}}
    finally{clearTimeout(timeout);captureStartRequest.current=false;if(mounted.current){setStartPending(false);void refreshLease();void refreshActive();}}
  }
  async function stopRecording(value:Recording,intent:'pause'|'stop'){
    if(captureStopRequests.current.has(value.id))return;
    captureStopRequests.current.add(value.id);setStopPendingId(value.id);setStopError('');
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000),requestSequence=++recordingRequestSeq.current;
    try{
      const snapshot=await api<Recording>(recordingPath(value)+'/stop',{method:'POST',signal:controller.signal});
      if(!mounted.current)return;
      if(snapshot.id!==value.id||snapshot.case_id!==value.case_id||snapshot.interview_id!==value.interview_id)throw new Error('停止响应的录音归属不符，请刷新并核对当前录音。');
      mergeRecordings([snapshot],requestSequence);
      acceptActive(captureActive(snapshot)?snapshot:null,requestSequence);
      if(intent==='pause')setPauseRecordingId(value.id);else setPauseRecordingId(previous=>previous===value.id?'':previous);
    }catch(error){if(mounted.current)setStopError(error instanceof Error&&error.name==='AbortError'?'停止请求尚未确认，录音可能仍在继续。请再次请求停止或刷新状态。':errorMessage(error));}
    finally{clearTimeout(timeout);captureStopRequests.current.delete(value.id);if(mounted.current){setStopPendingId(previous=>previous===value.id?'':previous);void refreshLease();void refreshActive();}}
  }
  async function retryTranscription(value:Recording){
    if(!allowed||!canRetryTranscription(value)||transcriptionRetryRequests.current.has(value.id))return;
    transcriptionRetryRequests.current.add(value.id);
    setRetryPending(previous=>({...previous,[value.id]:true}));setRetryErrors(previous=>({...previous,[value.id]:''}));
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000),requestSequence=++recordingRequestSeq.current;
    try{
      const snapshot=await api<Recording>(recordingPath(value)+'/transcription/retry',{...post({}),signal:controller.signal});
      if(!mounted.current)return;
      if(snapshot.id!==value.id||snapshot.case_id!==value.case_id||snapshot.interview_id!==value.interview_id)throw new Error('转写响应的录音归属不符，请刷新当前录音状态。');
      mergeRecordings([snapshot],requestSequence);
    }catch(error){if(mounted.current)setRetryErrors(previous=>({...previous,[value.id]:error instanceof Error&&error.name==='AbortError'?'重新转写请求尚未确认，任务可能已进入队列。请刷新转写状态后再决定是否重试。':errorMessage(error)}));}
    finally{
      clearTimeout(timeout);transcriptionRetryRequests.current.delete(value.id);
      if(mounted.current){setRetryPending(previous=>({...previous,[value.id]:false}));setRecordingReload(previous=>previous+1);}
    }
  }
  function showRecording(value:Recording){
    if(selection.current!==value.case_id)setSelectedId(value.case_id);
    else setSelectedInterview(value.interview_id);
    setSelectedRecordingId(value.id);setTab('interview');
  }
  function refreshRecordingState(){setRecordingReload(previous=>previous+1);setPlaybackError('');audioPlayer.current?.load();void refreshLease();void refreshActive();}
  function playFrom(seconds:number){
    if(!canPlay||!audioPlayer.current)return;
    try{audioPlayer.current.currentTime=Math.max(0,seconds);void audioPlayer.current.play().catch(()=>setPlaybackError('未能开始回听，请先在播放器中播放本段录音。'));}
    catch{setPlaybackError('音频尚未准备好，请先加载本段录音后再定位回听。');}
  }
  function selectCase(id:string){if(busy)return;if(id===selectedId)setCaseReload(previous=>previous+1);else setSelectedId(id);setTab('prepare');}
  function onCaseDeleted(id:string){
    deletedCaseIds.current.add(id);
    setCases(previous=>previous.filter(item=>item.id!==id));
    setRecordings(previous=>Object.fromEntries(Object.entries(previous).filter(([,item])=>item.case_id!==id)));
    if(selection.current===id){
      caseRequest.current++;selection.current='';
      audioPlayer.current?.pause();
      setCurrentCase(null);setSelectedId('');setSelectedInterview('');setSelectedRecordingId('');setTab('prepare');
      try{localStorage.removeItem('due-diligence:selected-case:v1');}catch{/* Server deletion does not depend on browser storage. */}
    }
    window.dispatchEvent(new Event('due-diligence:retention-changed'));
    void loadWorkspace();
  }
  const occupiedBy=lease?.owner?.app_id==='due-diligence'?'其他尽调录音任务':['meeting','meeting-demo'].includes(lease?.owner?.app_id||'')?'会议应用':'其他本地录音任务';

  return <div className="due-diligence-shell"><div className="dd-app" inert={showNew}>
    <header className="dd-header"><div className="dd-brand"><span className="dd-brand-mark"><FileCheck2 size={23}/></span><strong>尽调工作台</strong><span>让每项信息都有依据</span></div><div className="dd-header-status"><span className="dd-environment"><ShieldCheck size={14}/> 农户贷款尽调</span><span className={'dd-service '+(allowed?'connected':'')}><i/>{allowed?'本地工作空间':'服务待确认'}</span></div></header>
    <aside className="dd-sidebar"><button className="dd-new-case" disabled={!allowed||!!busy||loading||!!activeCapture||startPending} onClick={()=>{setCreateError('');setShowNew(true);}} title={activeCapture?'请先停止当前录音，再新建案件':undefined}><Plus size={17}/> 新建尽调案件</button><div className="dd-sidebar-label"><span>案件记录</span><span>{casesLoaded?cases.length.toString().padStart(2,'0'):'—'}</span></div>
      <nav className="dd-case-list" aria-label="案件列表">{cases.map(item=><button key={item.id} className={'dd-case-link '+(item.id===selectedId?'selected':'')} disabled={!!busy} onClick={()=>selectCase(item.id)}><FolderOpen size={17}/><span><strong>{item.title}</strong><small>{dateLabel(item.updated_at)} · 准备中</small></span><ChevronRight size={13}/></button>)}
        {!cases.length&&<p className="dd-sidebar-empty">{loading?'正在读取案件……':casesLoaded?'新建尽调案件后，可在这里重新打开。':'案件列表暂未读取。'}</p>}</nav>
      <div className="dd-sidebar-note"><LockKeyhole size={16}/><div><strong>独立的数据空间</strong><p>资料仅存本机，不自动带入其他业务系统。</p></div></div><footer><span><i/> 外部资料传输默认关闭</span><button onClick={()=>void loadWorkspace()} disabled={loading||!!busy}><RefreshCw size={14} className={loading?'dd-spin':''}/> 刷新工作空间</button></footer>
    </aside>
    <main className="dd-workspace"><div className="dd-breadcrumb"><span><FolderOpen size={14}/> 尽调案件 <ChevronRight size={13}/> {currentCase?.title||'工作空间'}</span><span>农户经营贷款</span></div>
      {activeCapture&&<div className="dd-capture-dock" role="region" aria-label="当前录音控制"><div><Radio size={17}/><span><strong>{captureLabels[activeCapture.capture_state]}</strong><small>实际已落盘 {audioTime(activeCapture.saved_seconds)} · {frameCount(activeCapture.saved_frames)} 帧</small></span></div><div className="dd-capture-dock-actions"><button onClick={()=>showRecording(activeCapture)}>查看本段</button><button className="dd-stop-button" disabled={stopPendingId===activeCapture.id} onClick={()=>void stopRecording(activeCapture,'stop')}><Square size={14}/>{stopPendingId===activeCapture.id?'正在请求停止':'停止并保存本段'}</button></div></div>}
      {activeCapture&&pollErrors[activeCapture.id]&&<div className="dd-workspace-notice"><Notice>{pollErrors[activeCapture.id]}</Notice></div>}
      {activeStatusError&&<div className="dd-workspace-notice"><Notice>{activeStatusError}</Notice></div>}
      {stopError&&<div className="dd-workspace-notice"><Notice>{stopError}</Notice></div>}
      {loadErrors.length>0&&<div className="dd-workspace-notice"><Notice>{loadErrors.join(' ')}{!allowed&&' 当前不能新建案件。'}</Notice></div>}
      {policy&&!allowed&&!loading&&<div className="dd-workspace-notice"><Notice>当前服务未就绪，请核对本地服务后刷新。</Notice></div>}
      {caseLoading?<div className="dd-loading" role="status"><LoaderCircle size={23} className="dd-spin"/><p>正在打开案件……</p></div>:caseError?<div className="dd-content-pad"><Notice>{caseError}</Notice><button onClick={()=>setCaseReload(previous=>previous+1)} disabled={loading}><RefreshCw size={14}/> 重新打开案件</button></div>:!currentCase?
        <div className="dd-welcome"><span className="dd-eyebrow">准备有上下文 · 交流有记录 · 结论有来源</span><h1>从一份案件，<br/>开始尽调准备。</h1><p>核对事前资料与问卷，<br className="dd-desktop-break"/>现场访谈留痕、辅助试算，并生成带来源的报告初稿。</p><button className="dd-primary" disabled={!allowed||!!busy||loading||!!activeCapture||startPending} onClick={()=>{setCreateError('');setShowNew(true);}}><Plus size={17}/> 新建第一份尽调案件</button>
          <div className="dd-welcome-flow"><div><FolderOpen size={23}/><strong>准备资料</strong><small>合成快照与问卷</small></div><div><Mic size={23}/><strong>现场访谈</strong><small>记录与补问建议</small></div><div><Calculator size={23}/><strong>测算与复核</strong><small>参考计算与规则审阅</small></div><div><FileText size={23}/><strong>报告初稿</strong><small>A4封面与DOCX导出</small></div></div></div>:
        <><header className="dd-case-header"><div className="dd-case-head-left"><div className="dd-case-meta"><span>准备中</span><span>农户贷款尽调</span><small>建立于 {dateLabel(currentCase.created_at)}</small></div><h1>{currentCase.title}</h1><p>农户场景；事前记载、现场陈述、记录准确确认与事实核验分别保留。</p></div><CaseHeaderBadges key={currentCase.id+':head'} caseId={currentCase.id} interviews={interviews.length} recordings={Object.values(recordings).filter(item=>item.case_id===currentCase.id).length}/></header>
          <div className="dd-toolbar"><nav className="dd-tabs" role="tablist" aria-label="尽调阶段">{tabs.map((item,index)=><button key={item.id} id={'dd-tab-'+item.id} role="tab" aria-selected={tab===item.id} aria-controls="dd-case-panel" tabIndex={tab===item.id?0:-1} className={tab===item.id?'active':''} onClick={()=>setTab(item.id)} onKeyDown={event=>{let next=index;if(event.key==='ArrowRight')next=(index+1)%tabs.length;else if(event.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;else return;event.preventDefault();setTab(tabs[next].id);document.getElementById('dd-tab-'+tabs[next].id)?.focus();}}><span>{item.number}</span>{item.label}</button>)}</nav><span className="dd-toolbar-note">资料、陈述与核验状态分别保留</span></div>
          <div className="dd-case-panel" ref={casePanel} id="dd-case-panel" role="tabpanel" aria-labelledby={'dd-tab-'+tab} tabIndex={0}>
            {actionError&&<Notice>{actionError}</Notice>}{success&&<Notice kind="success">{success}</Notice>}
            {tab==='prepare'&&<><div className="dd-section-heading"><div><span className="dd-eyebrow">尽调前准备</span><h2>先明确已知与待补。</h2></div><button className="dd-primary" disabled={!allowed||!!busy} onClick={()=>void createInterview()}>{busy==='interview'?<LoaderCircle size={15} className="dd-spin"/>:<SquarePen size={15}/>} {busy==='interview'?'正在建立':'建立访谈记录'}</button></div>
              <CustomerInfoCard key={currentCase.id+':info'} caseId={currentCase.id} allowed={allowed}/>
              <S3Workspace key={currentCase.id+':prepare'} caseId={currentCase.id} mode="prepare" allowed={allowed}/>
              <section className="dd-card dd-interview-summary"><div><BookOpen size={22}/><div><h3>本案访谈记录</h3><p>{interviews.length?`已有 ${interviews.length} 次访谈记录，可在“现场”查看。`:'尚未建立访谈记录。建立后可在此案件中重新打开。'}</p></div></div><button onClick={()=>setTab('interview')}>查看现场页 <ArrowRight size={14}/></button></section>
            </>}
            {tab==="interview"&&<><div className="dd-onsite-actions"><button disabled={!allowed||!!busy} onClick={()=>void createInterview()}>{busy==="interview"?<LoaderCircle size={15} className="dd-spin"/>:<Plus size={15}/>} 建立访谈记录</button></div>
              <div className="dd-onsite-3col">
                <div className="dd-onsite-left">
                <div className="dd-interview-grid"><section className="dd-card dd-interview-list"><div className="dd-card-heading"><BookOpen size={17}/><h3>本案访谈</h3><span>{interviews.length} 次</span></div>{interviews.map(item=>{
                  const known=Object.values(recordings).filter(value=>value.case_id===currentCase.id&&value.interview_id===item.id);
                  const count=Math.max(known.length,item.recordings?.length||0),live=known.some(captureActive);
                  return <button className={'dd-interview-link '+(item.id===interview?.id?'selected':'')} key={item.id} onClick={()=>setSelectedInterview(item.id)}><span><strong>{item.title}</strong><small>{dateLabel(item.created_at)} · {live?'正在录音':count?`${count} 段录音记录`:'尚无录音段'}</small></span><ChevronRight size={13}/></button>;
                })}{!interviews.length&&<p className="dd-help">先建立访谈记录，再手动开始本机测试录音。</p>}</section>
                  <section className="dd-card dd-transcript-card"><div className="dd-card-heading"><FileText size={17}/><h3>文字记录</h3><span className="dd-tag">近似时间范围</span></div>
                    {recording&&asrActive(recording)&&<div className="dd-ai-live" role="status">
                      <span className="dd-ai-orb"><i>AI</i></span>
                      <span className="dd-ai-copy"><strong>AI 正在转录</strong><small>{recording.variant==='sichuan'?'四川话分批识别中，约每12秒出一批文字；':'普通话实时识别中；'}{recording.asr.retry_queued?'等待转写资源，音频保存不受识别影响':
                        (currentTranscribedSeconds!=null&&recording.saved_seconds
                          ? `已转写 ${audioTime(currentTranscribedSeconds)} / ${audioTime(recording.saved_seconds)}（${Math.min(100,Math.round(currentTranscribedSeconds/recording.saved_seconds*100))}%）`
                          : '实时识别中，随说话逐步生成')}</small></span>
                      <span className="dd-ai-progress" aria-hidden="true"><i style={{width:`${currentTranscribedSeconds!=null&&recording.saved_seconds?Math.min(100,currentTranscribedSeconds/recording.saved_seconds*100):0}%`}}/></span>
                      <span className="dd-ai-wave" aria-hidden="true"><i/><i/><i/><i/><i/><i/><i/></span>
                    </div>}
                    <p className="dd-help">片段时间用于粗略回听，尚未逐字对齐；文字不是已核验事实。停止收音后，模型可能继续补齐本段转写。</p>
                    {pendingTranscriptCount>0&&<Notice kind="info">{recording?.transcript_is_from_previous_run?'上次识别留下的文字尚未全部经过本次识别核对。':'部分文字尚未达到当前稳定转写范围。'}{` 其中 ${pendingTranscriptCount} 段暂不列为稳定文字。`} 录音保存进度单独显示。</Notice>}
                    <div className="dd-repair-bar">
                      <label className="dd-repair-toggle"><input type="checkbox" checked={repairOn}
                        onChange={event=>{setRepairOn(event.target.checked);setRepairNote(event.target.checked?'智能修复已开启：新文字将分批整理。':'智能修复已关闭；已整理内容保留。');}}/>
                        <span>AI 整理文字</span></label>
                      <small>{repairOn?(repairBusy?`正在整理…（剩 ${repairPending} 段）`:(repairPending?`待整理 ${repairPending} 段`:(Object.keys(repairedMap).length?`已整理 ${Object.keys(repairedMap).length} 段`:'等待新文字'))):'关闭'}</small>
                      {repairNote&&<small className="dd-repair-note">{repairNote}</small>}
                    </div>
                    {repairOn&&Object.keys(repairedMap).length>0&&<div className="dd-transcript-repaired" aria-label="AI整理稿">
                      <strong>AI 整理稿（对照参考）</strong>
                      {repairedLines.map((line,index)=><p key={index}>{line}</p>)}
                      <small>由本地智能体根据原文整理；数字与事实以原文为准。</small>
                    </div>}
                    {stableSegments.length?<><div className="dd-transcript-readable" aria-label="连续文字记录">{(recording?.punctuated_text||stableSegments.map(segment=>segment.text).join('')).split(/(?<=[。！？!?])/u).filter(Boolean).map((sentence,index)=><p key={index}>{sentence}</p>)}</div><details className="dd-transcript-details"><summary>按时间定位回听</summary><div className="dd-transcript-list">{stableSegments.map(segment=><div className="dd-transcript-segment" key={segment.id}><button type="button" disabled={!canPlay} title={canPlay?'从近似时间位置回听':'停止并保存本段后可定位回听'} onClick={()=>playFrom(segment.start_seconds)}><Play size={11}/>{audioTime(segment.start_seconds)}—{audioTime(segment.end_seconds)}</button><p>{segment.text}</p></div>)}</div></details></>:<div className="dd-transcript-empty">{pendingTranscriptCount>0?'已有文字仍待本次识别核对，当前尚无可显示的稳定文字。':recording?.asr.state==='failed'?'本段尚无可显示的稳定转写。':recording&&asrActive(recording)?'等待稳定文字；音频保存进度在上方单独显示。':'尚无稳定文字记录。'}</div>}
                  </section>
                  <div className="dd-live-workbench"><section className="dd-card dd-recorder-card"><div className="dd-card-heading"><Mic size={18}/><h3>{interview?.title||'本机录音'}</h3><button className="dd-compact-button" disabled={recordingsLoading||!interview} onClick={refreshRecordingState}><RefreshCw size={13} className={recordingsLoading?'dd-spin':''}/> 刷新录音状态</button></div>
                    <div className="dd-device-picker"><label>本机麦克风<select aria-label="选择本机麦克风" value={deviceId??''} disabled={!!activeCapture||startPending||devicesLoading} onChange={event=>setDeviceId(event.target.value===''?null:Number(event.target.value))}>{!devices.length&&<option value="">{devicesLoading?'正在读取设备……':'暂无可用输入设备'}</option>}{devices.map(device=><option key={device.id} value={device.id}>{device.name}{device.is_default?' · 默认':''}</option>)}</select></label><label>识别语言<select aria-label="选择识别语言" value={asrVariant} disabled={!!activeCapture||startPending} onChange={event=>setAsrVariant(event.target.value as 'mandarin'|'sichuan')}><option value="mandarin">普通话 · 实时</option><option value="sichuan">四川话 · 分批</option></select></label><button disabled={devicesLoading} onClick={()=>{void refreshDevices();void refreshLease();void refreshActive();}}><RefreshCw size={14} className={devicesLoading?'dd-spin':''}/> 刷新设备</button><button disabled={!allowed||!!busy||!interview||!!activeCapture} onClick={()=>{const input=document.getElementById('dd-import-file');if(input)input.click();}} title={!interview?'请先点上方「+ 建立访谈记录」再导入录音':!!activeCapture?'录音进行中，请先停止':'导入外部录音（手机/录音笔，支持 mp3/m4a/wav 等），自动转成文字'}><Upload size={14}/> 导入录音</button><input id="dd-import-file" type="file" accept="audio/*,.wav,.mp3,.m4a,.amr,.aac" style={{display:'none'}} onChange={event=>{const file=event.target.files&&event.target.files[0];event.target.value='';if(file)void importRecording(file);}}/></div>
                    {devicesError&&<Notice>{devicesError}</Notice>}{recordingsError&&<Notice>{recordingsError} 当前尚未确认是否已有录音，请刷新状态后再开始。</Notice>}{captureError&&<Notice>{captureError}</Notice>}
                    <div className="dd-recorder-controls"><button className="dd-primary" disabled={!canStart} onClick={()=>void startRecording()}>{startPending?<LoaderCircle size={15} className="dd-spin"/>:<Mic size={15}/>} {startPending?'正在请求开始':pauseRecordingId&&recording?.id===pauseRecordingId?'新段恢复录音':currentRecordings.length?'开始新录音段':'开始测试录音'}</button>
                      {activeCapture&&<><button disabled={stopPendingId===activeCapture.id} onClick={()=>void stopRecording(activeCapture,'pause')}><Pause size={14}/> 暂停并保存本段</button><button className="dd-stop-button" disabled={stopPendingId===activeCapture.id} onClick={()=>void stopRecording(activeCapture,'stop')}><Square size={14}/>{stopPendingId===activeCapture.id?'正在请求停止':'停止并保存'}</button></>}
                    </div><p className="dd-help">暂停会停止并保存当前段；恢复会新建录音段，段间时间不会录制，不是无缝续录。模型加载或转写失败不锁住停止按钮。</p>
                    {pauseRecordingId&&recording?.id===pauseRecordingId&&!captureActive(recording)&&<Notice kind="info">本段已停止。继续录音将建立新段，暂停期间的声音不会补录。</Notice>}
                    {recordingsLoading&&<p className="dd-inline-loading" role="status"><LoaderCircle size={14} className="dd-spin"/> 正在读取已保存录音与当前状态……</p>}
                    {recording?<><div className="dd-recording-selector"><label>查看录音段<select aria-label="选择录音段" value={recording.id} onChange={event=>setSelectedRecordingId(event.target.value)}>{currentRecordings.map((item,index)=><option key={item.id} value={item.id}>第 {currentRecordings.length-index} 段 · {item.started_at?dateLabel(item.started_at):'启动时点待确认'} · {captureLabels[item.capture_state]}</option>)}</select></label></div>
                      {pollErrors[recording.id]&&recording.id!==activeCapture?.id&&<Notice>{pollErrors[recording.id]}</Notice>}
                      <div className="dd-recording-states"><div><span>收音与保存</span><strong className={captureActive(recording)?'active':''}>{captureActive(recording)&&<Radio size={14}/>} {captureLabels[recording.capture_state]}</strong></div><div><span>语音转文字{recording.variant==='sichuan'?'（四川话）':''}</span><strong>{asrActive(recording)&&<LoaderCircle size={14} className="dd-spin"/>} {recording.asr.retry_queued?'等待转写资源':asrLabels[recording.asr.state]}</strong></div></div>
                      <div className="dd-recording-metrics"><div><span>实际已落盘音频</span><strong>{audioTime(recording.saved_seconds)}</strong></div><div><span>已落盘帧</span><strong>{frameCount(recording.saved_frames)}</strong></div><div><span>本次已转写音频范围</span><strong>{currentTranscribedSeconds==null?'尚未取得':audioTime(currentTranscribedSeconds)}</strong></div></div>
                      <p className="dd-save-confirmation">{recording.last_saved_at?`最近落盘确认：${dateLabel(recording.last_saved_at)}`:'尚未取得落盘确认时点。'}{recording.capture_state==='saved'?' 本段已落盘不代表录音质量或 S2 验收已通过。':''}</p>
                      <CaptureRecoveryNotice recording={recording}/>
                      {recording.error&&<Notice>{recording.error} 请以已落盘范围为准。</Notice>}
                      {recording.data_error&&<Notice><strong>业务状态或文本修订尚未保存。</strong> {recording.data_error} 请保留录音目录并检查存储；音频保存情况以上方实际落盘范围和回听状态为准。</Notice>}
                      {(recording.asr.message||recording.asr.error)&&<div className={'dd-asr-note '+(recording.asr.state==='failed'?'error':'')}><span>转写状态</span><p>{recording.asr.error?asrErrorLabel(recording.asr.error):recording.asr.message}</p>{recording.asr.state==='failed'&&<small>请单独核对录音保存状态；失败的转写不会伪装为已完成。</small>}</div>}
                      {(['failed','disabled'].includes(recording.asr.state)||recording.asr.retry_queued||retryPending[recording.id])&&<div className="dd-asr-actions"><button disabled={!allowed||!canRetryTranscription(recording)||!!retryPending[recording.id]} onClick={()=>void retryTranscription(recording)}>{retryPending[recording.id]?<LoaderCircle size={14} className="dd-spin"/>:<RefreshCw size={14}/>} {retryPending[recording.id]?'正在请求重新转写':recording.asr.retry_queued?'重新转写已排队':'重新转写本段'}</button><p className="dd-help">{recording.asr.retry_queued?'正在等待前段任务释放资源，请勿重复提交。':recording.saved_frames>0?'处理已保存音频；录音保存状态和停止按钮单独控制。':'有已保存音频后可重新转写。'}</p></div>}
                      {retryErrors[recording.id]&&<Notice>重新转写：{retryErrors[recording.id]}</Notice>}
                      <div className="dd-audio-playback"><div><Headphones size={16}/><strong>本段回听</strong></div>{canPlay?<><audio key={recording.id} ref={audioPlayer} controls preload="metadata" src={API+recordingPath(recording)+'/audio'} onError={()=>setPlaybackError('未能读取本段音频，请刷新状态后重试；不要据此认定录音已丢失。')}/><p>回听实际已落盘的音频；点击下方文字的时间范围，可跳到近似位置。</p></>:<p>{captureActive(recording)?'停止并保存本段后，可在此回听实际落盘的音频。':'当前没有服务确认可回听的音频。'}</p>}{playbackError&&<Notice>{playbackError}</Notice>}</div>
                    </>:!recordingsLoading&&<div className="dd-no-recording"><Mic size={24}/><p>{interview?'本次访谈尚无录音段。选择麦克风后，可手动开始测试。':'尚未建立访谈记录。'}</p></div>}
                  </section>
                  </div></div>
                </div>
                <div className="dd-onsite-mid"><AnswerBoard key={currentCase.id+":answers"} caseId={currentCase.id} allowed={allowed}/></div>
              </div>
              <details className="dd-verify-rail"><summary>核查与证据（原话、事实确认、录音转写）</summary>
                  <S3Workspace key={currentCase.id+':interview'} caseId={currentCase.id} mode="interview" interviewId={interview?.id} allowed={allowed} stableSegments={s3StableSegments}/>
                  </details>
              {lease?.occupied&&!activeCapture&&<Notice kind="info">录音设备正被{occupiedBy}占用。请等待该任务结束后刷新状态。</Notice>}
            </>}
            {tab==='calculate'&&<div className="dd-calc-page">
              <CreditSummary key={currentCase.id+':credit'} caseId={currentCase.id} generating={limitGenerating} step={limitStep} error={limitError} onRetry={()=>void runLimitGeneration()} onOpenWorkbench={()=>{const d=document.getElementById('dd-calc-advanced');if(d){(d as HTMLDetailsElement).open=true;d.scrollIntoView({behavior:'smooth',block:'start'});}}}/>
              <details className="dd-calc-advanced" id="dd-calc-advanced"><summary>完整测算工作台（输入、台账、评分明细与历史）</summary>
                <CalculationPanel key={currentCase.id} caseId={currentCase.id}/>
              </details>
              <ReportsWorkspace key={currentCase.id+':report'} caseId={currentCase.id} caseTitle={currentCase.title}/>
            </div>}
            <details style={{marginTop:20}}><summary style={{cursor:'pointer',fontSize:12,color:'var(--dd-muted)'}}>本地备份与恢复</summary><MaintenancePanel/></details>
            <details style={{marginTop:20}}><summary style={{cursor:'pointer',fontSize:12,color:'var(--dd-muted)'}}>问卷题目管理（必问/选问、启用/停用）</summary><div style={{marginTop:10}}><QuestionnaireAdmin/></div></details>
            <RetentionPanel caseId={currentCase.id} caseTitle={currentCase.title} onDeleted={onCaseDeleted}/>
            <details className="dd-capabilities"><summary><Layers3 size={14}/> 查看当前功能状态</summary><div>{capabilities.length?capabilities.map(item=><div className="dd-capability" key={item.id}><strong>{item.label}</strong><span>{item.status==='under_verification'?'测试中':['available','implemented','ready','enabled'].includes(item.status)?'已启用':'尚未启用'}</span><p>{item.reason}</p></div>):<p className="dd-help">功能状态尚未读取，请刷新工作空间。</p>}</div></details>
          </div></>}
      {!currentCase&&!loading&&<div style={{padding:'0 28px 20px',overflowY:'auto',flexShrink:0,maxHeight:'40vh'}}><RetentionPanel onDeleted={onCaseDeleted}/></div>}
    </main>
  </div>{showNew&&<NewCaseDialog busy={busy==='create'} onClose={closeNew} onSubmit={title=>void createCase(title)} error={createError}/>}</div>;
}
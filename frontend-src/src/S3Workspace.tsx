import {useCallback, useEffect, useRef, useState} from 'react';
import type {FormEvent, ReactNode} from 'react';
import {AlertCircle, Check, ClipboardList, FileText, History, LoaderCircle, RefreshCw, Save, ShieldCheck} from 'lucide-react';
import './S3Workspace.css';

type Presence='known'|'explicit_zero'|'missing'|'unknown'|'not_applicable'|'withheld';
type ValueType='integer'|'decimal'|'text'|'enum';
type DimensionStatus='unknown'|'explicit'|'not_applicable';
type Period={kind:string;label:string;start:string|null;end:string|null};
type Subject={id:string;label:string;kind:string};
type Statement={id:string;case_id:string;revision:number;text:string;kind:'manual'|'asr_segment';has_audio:boolean;
  speaker_label?:string;speaker_role?:string;subject_id?:string|null;subject_basis?:string|null;
  record_confirmation:string;source_kind?:string;interview_id?:string|null;needs_review?:boolean;
  source_ref?:{recording_id:string;interview_id:string;segment_id:string;segment_revision:number;source_run_id:string|null;text_sha256:string;audio_start_frame:number;audio_end_frame:number}|null;
  created_at?:string;updated_at?:string};
type Fact={id:string;case_id:string;field_id:string;revision:number;origin:'onsite'|'previsit';
  value_type:string;value:string|null;presence:Presence;unit?:string|null;unit_status?:string;
  period?:Period|null;period_status?:string;subject_id?:string|null;subject_status?:string;subject_basis?:string|null;
  statement_id?:string|null;statement_revision?:number|null;record_confirmation:string|Record<string,unknown>;verification:string|Record<string,unknown>;
  record_confirmation_meta?:Record<string,unknown>;verification_meta?:Record<string,unknown>;subject_scope?:string;
  needs_review:boolean;conflict:boolean;calculation_eligibility:unknown;source_ref?:Record<string,unknown>|null;source_refs?:Record<string,unknown>[]};
type Field={field_id:string;label:string;question_text:string;handling:string;value_type:string;required?:boolean;
  section?:string;question_keys?:string[];source_question_ids?:string[];source_locations?:{location?:string;kind?:string}[];
  notes?:string;default_unit?:string;value_options?:{value:string;label:string}[];conditions?:unknown[];applicability:string;applicability_reason?:string;completion:string;fact_ids:string[]};
type Previsit={state:string;fixture_id?:string|null;context_version?:number|null;snapshot_id?:string|null;cached?:boolean;
  data_as_of?:string|null;read_at?:string|null;loaded_at?:string|null;parties:{party_id:string;role:string;synthetic_label?:string}[];
  households:{household_id:string;synthetic_label?:string}[];business_units:{business_unit_id:string;synthetic_label?:string;kind?:string}[];
  facts:Fact[];materials:Record<string,unknown>[];history:Record<string,unknown>[]};
type Workspace={case_id:string;mode:'synthetic_development';previsit:Previsit;
  questionnaire:{version:string;fields:Field[];progress:Record<string,number>};statements:Statement[];facts:Fact[];
  conflicts:{field_id?:string;fact_ids?:string[];reason?:string}[];credit:{state:string;reason:string;effective_limit:null}};
type Example={fixture_id:string;scenario:string;label:string;description:string};
type SnapshotView={snapshot_id:string;context_version:number;loaded_at:string;cached:boolean;
  context:Pick<Previsit,'data_as_of'|'facts'|'materials'|'parties'|'households'|'business_units'>};
export type S3StableSegment={recording_id:string;interview_id:string;segment_id:string;segment_revision:number;text:string;start_seconds:number;end_seconds:number};
type Props={caseId:string;mode:'prepare'|'interview';interviewId?:string;allowed:boolean;stableSegments?:S3StableSegment[]};
type FactDraft={field_id:string;value_type:ValueType;value:string;presence:Presence;unit:string;unit_status:DimensionStatus;
  period_status:DimensionStatus;period_kind:string;period_label:string;period_start:string;period_end:string;subject_id:string;subject_status:'unknown'|'explicit';subject_basis:string};
const API='/api/due-diligence';
const labels:Record<string,string>={known:'已说明',explicit_zero:'明确为零',missing:'未提供',unknown:'未知',not_applicable:'明确不适用',withheld:'明确不愿提供',
  pending:'待确认记录',record_accurate:'已确认记录准确',revoked:'准确确认已撤销',not_checked:'尚未核验',
  interview:'本版可访谈',extension_required:'需扩展后接入',upstream_only:'事前系统只读',not_supported:'首版不支持',
  applicable:'适用',needs_review:'需要重新核对',candidate:'有事实候选',unloaded:'尚未加载',available:'可用合成快照',unavailable:'来源暂不可达',denied:'无读取权限',
  applicant:'申请人',household_member:'家属',investigator:'调查员',third_party:'第三方',unknown_speaker:'未知说话人',
  explicit:'已明确',blocked:'不满足测算条件',onsite:'现场陈述',previsit:'事前记载',year:'年度',season:'季节',as_of:'截至时点',duration:'时长',purchase_date:'购入或借款时点',
  integer:'整数',decimal:'数值',text:'文字',enum:'选项',true:'是',false:'否'};
Object.assign(labels,{checked_consistent:'合成示例核验一致',structured_available:'已有结构化记载',uploaded_not_parsed:'已上传，尚未解析',
  access_denied:'无读取权限',not_provided:'未提供',not_queried:'尚未查询',no_rule_provided:'尚无时效规则',pending_rule_calibration:'需在“测算”页补齐独立输入并复核资格',base:'基础版本',updated:'更新版本',
  context_match:'与当前字段主体范围相符',out_of_scope:'主体超出当前字段范围',pending_business_definition:'归属规则待确认',
  blocked_source_changed:'来源已变更，需要复核',pending_confirmation:'待确认记录准确',blocked_subject:'主体条件不足',blocked_conflict:'尚有未解决冲突',blocked_missing:'事实缺失或不适用',blocked_unit:'单位条件不足',blocked_period:'期间尚未明确'});
const wording=(value:unknown,fallback='待说明'):string=>{
  if(value===null||value===undefined||value==='')return fallback;
  if(typeof value==='boolean')return value?'是':'否';
  if(typeof value==='object'){const record=value as Record<string,unknown>;return wording(record.state??record.label??record.location,fallback);}
  return labels[String(value)]||String(value);
};
const stringValue=(value:unknown)=>value===null||value===undefined?'':typeof value==='object'?JSON.stringify(value):String(value);
const dateText=(value:unknown)=>{if(typeof value!=='string'||!value)return '未提供';const date=new Date(value);return Number.isNaN(date.getTime())?value:date.toLocaleString('zh-CN');};
const periodText=(period:Period|null|undefined)=>period?`${period.label}${period.start||period.end?`（${period.start||'起点未知'}—${period.end||'终点未知'}）`:''}`:'期间未知';
const scaleCategoryLabels:Record<string,string>={fruit:'水果',grain:'主粮',herb:'中草药',other:'其他（已明确归属）'};
const factValue=(fact:Fact)=>fact.presence==='explicit_zero'?'0':fact.presence==='known'?(fact.field_id==='crop.main.scale_category'?scaleCategoryLabels[fact.value||'']||'品类未明确':stringValue(fact.value)||'值未提供'):wording(fact.presence);
const factSource=(fact:Fact)=>{
  const nested=fact.source_ref?.source_refs;
  return Array.isArray(nested)&&nested[0]&&typeof nested[0]==='object'?nested[0] as Record<string,unknown>:fact.source_ref||fact.source_refs?.[0];
};
const factMetadata=(fact:Fact,kind:'record_confirmation'|'verification')=>fact[kind==='record_confirmation'?'record_confirmation_meta':'verification_meta']||
  (typeof fact[kind]==='object'?fact[kind] as Record<string,unknown>:null);
const factUnitText=(fact:Fact)=>fact.unit_status==='explicit'?fact.unit||'单位未填':wording(fact.unit_status,'单位未知');
const factPeriodText=(fact:Fact)=>fact.period_status==='explicit'?periodText(fact.period):wording(fact.period_status,'期间未知');
const newDraft=(field?:Field):FactDraft=>({field_id:field?.field_id||'',value_type:['integer','decimal','enum'].includes(field?.value_type||'')?field!.value_type as ValueType:'text',
  value:'',presence:'unknown',unit:'',unit_status:'unknown',period_status:'unknown',period_kind:'',period_label:'',period_start:'',period_end:'',subject_id:'',subject_status:'unknown',subject_basis:''});
async function request<T>(path:string,options:RequestInit={}):Promise<T>{
  const response=await fetch(API+path,options);
  if(!response.ok){const body=await response.json().catch(()=>null);const detail=body?.detail;
    throw new Error(typeof detail==='string'?detail:typeof detail?.message==='string'?detail.message:`本次操作未完成（${response.status}），请刷新后核对。`);}
  return response.json() as Promise<T>;
}
const jsonRequest=(value:unknown,method='POST'):RequestInit=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
function Note({children,kind='info'}:{children:ReactNode;kind?:'info'|'warn'|'error'|'success'}){
  return <div className={'s3-note '+kind} role={kind==='error'?'alert':'status'}>{kind==='success'?<Check size={13}/>:<AlertCircle size={13}/>} <span>{children}</span></div>;
}
function Meta({items}:{items:[string,ReactNode][]}){return <dl className="s3-meta">{items.map(([name,value])=><div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>;}
function StatusTag({value,warning=false}:{value:unknown;warning?:boolean}){return <span className={'s3-tag '+(warning?'warn':value==='record_accurate'?'good':'')}>{wording(value)}</span>;}

export default function S3Workspace({caseId,mode,interviewId,allowed,stableSegments=[]}:Props){
  const [workspace,setWorkspace]=useState<Workspace|null>(null),[examples,setExamples]=useState<Example[]>([]),[fixtureId,setFixtureId]=useState('');
  const [variant,setVariant]=useState('base'),[loading,setLoading]=useState(true),[error,setError]=useState(''),[success,setSuccess]=useState(''),[busy,setBusy]=useState('');
  const [fieldId,setFieldId]=useState(''),[statementId,setStatementId]=useState(''),[statementRevision,setStatementRevision]=useState<number|null>(null);
  const [sourceText,setSourceText]=useState(''),[speakerLabel,setSpeakerLabel]=useState(''),[speakerRole,setSpeakerRole]=useState('unknown'),[sourceSubject,setSourceSubject]=useState(''),[sourceBasis,setSourceBasis]=useState('');
  const [editingSource,setEditingSource]=useState<{id:string;revision:number}|null>(null),[draft,setDraft]=useState<FactDraft>(newDraft()),[editingFact,setEditingFact]=useState<{id:string;revision:number}|null>(null);
  const [factHistory,setFactHistory]=useState<Record<string,Fact[]>>({}),[historyOpen,setHistoryOpen]=useState<string|null>(null),[basis,setBasis]=useState<Record<string,string>>({});
  const [sourceMode,setSourceMode]=useState<'manual'|'asr'>('manual'),[segmentKey,setSegmentKey]=useState(''),[sourceHistory,setSourceHistory]=useState<Record<string,Statement[]>>({}),[sourceHistoryOpen,setSourceHistoryOpen]=useState<string|null>(null);
  const [historySnapshot,setHistorySnapshot]=useState<SnapshotView|null>(null);
  const mounted=useRef(false),readSequence=useRef(0),writing=useRef(false),pendingSourceRefresh=useRef(false);
  const base='/cases/'+encodeURIComponent(caseId);
  const clearDeniedContent=useCallback(()=>{
    setHistorySnapshot(null);setSourceText('');setSpeakerLabel('');setSpeakerRole('unknown');setSourceSubject('');setSourceBasis('');setEditingSource(null);
    setStatementId('');setStatementRevision(null);setFieldId('');setDraft(newDraft());setEditingFact(null);setSourceHistory({});setFactHistory({});setSourceHistoryOpen(null);setHistoryOpen(null);setBasis({});setSegmentKey('');
  },[]);
  const refresh=useCallback(async(signal?:AbortSignal)=>{
    const sequence=++readSequence.current;
    const value=await request<Workspace>(base+'/diligence',{signal});
    if(value.case_id!==caseId||value.mode!=='synthetic_development')throw new Error('返回的案件或数据模式不符，本次未打开资料。');
    if(mounted.current&&!signal?.aborted&&sequence===readSequence.current){if(value.previsit.state==='denied')clearDeniedContent();setWorkspace(value);}
    return value;
  },[base,caseId,clearDeniedContent]);
  useEffect(()=>{
    mounted.current=true;const controller=new AbortController();setLoading(true);setError('');
    void Promise.allSettled([refresh(controller.signal),request<{examples:Example[]}>('/previsit/examples',{signal:controller.signal})]).then(results=>{
      if(!mounted.current||controller.signal.aborted)return;
      if(results[0].status==='rejected')setError(results[0].reason instanceof Error?results[0].reason.message:'资料状态尚未读取。');
      if(results[1].status==='fulfilled'){const incoming=results[1].value.examples;setExamples(incoming);setFixtureId(previous=>previous||incoming[0]?.fixture_id||'');}
      else setError(previous=>previous||'合成快照列表尚未读取，请刷新。');
      setLoading(false);
    });
    return()=>{mounted.current=false;controller.abort();readSequence.current++;};
  },[refresh]);
  useEffect(()=>{
    const onSourcesUpdated=(event:Event)=>{
      if((event as CustomEvent<{caseId:string}>).detail?.caseId!==caseId)return;
      if(writing.current){pendingSourceRefresh.current=true;return;}
      // Only replace the persisted workspace. Keep unsaved drafts, selection and
      // chosen source revision; a denied response still clears restricted data.
      void refresh().catch(reason=>{if(mounted.current)setError(reason instanceof Error?reason.message:'新转写来源尚未读取，请稍后刷新。');});
    };
    window.addEventListener('due-diligence:asr-sources-updated',onSourcesUpdated);
    return()=>window.removeEventListener('due-diligence:asr-sources-updated',onSourcesUpdated);
  },[caseId,refresh]);
  const fields=workspace?.questionnaire.fields||[];
  const questions=fields.filter(field=>field.handling==='interview'&&field.applicability!=='not_applicable');
  // the verify rail lists only fields that actually carry statements or facts;
  // unanswered questionnaire items have nothing to verify here and would just
  // duplicate the onsite question board.
  const recordedFieldIds=new Set((workspace?.facts||[]).map(f=>f.field_id));
  const verifiable=questions.filter(field=>recordedFieldIds.has(field.field_id));
  const selectedField=fields.find(field=>field.field_id===fieldId)||questions[0];
  const statements=workspace?.statements||[];
  const selectedStatement=statements.find(statement=>statement.id===statementId);
  const subjects:Subject[]=[...(workspace?.previsit.parties||[]).map(p=>({id:p.party_id,label:p.synthetic_label||p.party_id,kind:wording(p.role)})),
    ...(workspace?.previsit.households||[]).map(p=>({id:p.household_id,label:p.synthetic_label||p.household_id,kind:'家庭'})),
    ...(workspace?.previsit.business_units||[]).map(p=>({id:p.business_unit_id,label:p.synthetic_label||p.business_unit_id,kind:'经营单元'}))];
  const subjectName=(id:unknown)=>typeof id==='string'&&id?subjects.find(subject=>subject.id===id)?.label||id:'主体归属未知';
  const previsit=workspace?.previsit;
  const snapshotLoadedAt=previsit?.history.find(entry=>entry.snapshot_id===previsit.snapshot_id)?.loaded_at;
  const currentFacts=workspace?.facts||[];
  const fieldFacts=currentFacts.filter(fact=>fact.field_id===(selectedField?.field_id||fieldId));
  const conflictFields=[...new Set(currentFacts.filter(fact=>fact.conflict).map(fact=>fact.field_id))];
  const sourceCurrent=!!selectedStatement&&statementRevision===selectedStatement.revision;
  const sourceConfirmed=sourceCurrent&&selectedStatement?.record_confirmation==='record_accurate';
  const factDraftChangedSource=!!selectedStatement&&!sourceCurrent;
  const segmentOptionKey=(segment:S3StableSegment)=>`${segment.recording_id}:${segment.segment_id}:${segment.segment_revision}`;
  const selectedSegment=stableSegments.find(segment=>segmentOptionKey(segment)===segmentKey);
  useEffect(()=>{if(!fieldId&&questions[0]){setFieldId(questions[0].field_id);setDraft(newDraft(questions[0]));}},[fieldId,questions[0]?.field_id]);
  useEffect(()=>{if(previsit?.fixture_id)setFixtureId(previsit.fixture_id);},[previsit?.fixture_id]);
  useEffect(()=>{if(previsit?.state==='denied')clearDeniedContent();},[previsit?.state,clearDeniedContent]);
  const run=async(key:string,operation:()=>Promise<void>)=>{
    if(writing.current||!allowed)return;writing.current=true;setBusy(key);setError('');setSuccess('');
    try{await operation();}
    catch(reason){if(mounted.current){setError(reason instanceof Error?reason.message:'本次操作未完成，请核对后重试。');try{await refresh();}catch{/* Keep the existing snapshot visible on read failure. */}}}
    finally{
      writing.current=false;
      if(mounted.current){
        setBusy('');
        if(pendingSourceRefresh.current){
          pendingSourceRefresh.current=false;
          void refresh().catch(reason=>{if(mounted.current)setError(reason instanceof Error?reason.message:'新转写来源尚未读取，请稍后刷新。');});
        }
      }
    }
  };
  const loadPrevisit=()=>run('previsit',async()=>{const value=await request<Workspace>(base+'/previsit/load',jsonRequest({fixture_id:fixtureId,variant}));
    if(value.case_id!==caseId)throw new Error('快照案件不符，未采用本次结果。');
    readSequence.current++;if(value.previsit.state==='denied')clearDeniedContent();setWorkspace(value);setSuccess('已读取合成资料状态；真实上游仍未接通。');});
  const readSnapshot=(snapshotId:string)=>run('snapshot:'+snapshotId,async()=>{
    if(previsit?.state==='denied')throw new Error('当前无读取权限，不能从历史查看正文。');
    const value=await request<SnapshotView>(base+'/previsit/snapshots/'+encodeURIComponent(snapshotId));
    if(value.snapshot_id!==snapshotId||!value.context||!Array.isArray(value.context.facts))throw new Error('历史快照身份或结构不符，本次未展示正文。');
    setHistorySnapshot(value);setSuccess('正在只读查看历史快照；未改变现场使用的当前快照。');
  });
  const selectField=(field:Field)=>{setFieldId(field.field_id);setDraft(newDraft(field));setEditingFact(null);setSuccess('');};
  const useStatement=(statement:Statement)=>{setStatementId(statement.id);setStatementRevision(statement.revision);setSuccess('已选择这版原话，事实归属仍需单独填写。');};
  const saveSource=(event:FormEvent)=>{event.preventDefault();void run('source',async()=>{
    const payload={text:sourceText,speaker_label:speakerLabel.trim()||'未知说话人',speaker_role:speakerRole,subject_id:sourceSubject||null,subject_basis:sourceSubject?sourceBasis.trim()||null:null};
    const statement=await request<Statement>(editingSource?base+'/statements/'+encodeURIComponent(editingSource.id):base+'/statements',jsonRequest(editingSource?{...payload,expected_revision:editingSource.revision}:{...payload,interview_id:interviewId||null},editingSource?'PATCH':'POST'));
    await refresh();setStatementId(statement.id);setStatementRevision(statement.revision);setSourceText('');setSpeakerLabel('');setSpeakerRole('unknown');setSourceSubject('');setSourceBasis('');setEditingSource(null);
    setSuccess('原话已保存为待确认记录；尚未确认记录准确，也未核验事实。');
  });};
  const editSource=(statement:Statement)=>{if(statement.kind!=='manual')return;setEditingSource({id:statement.id,revision:statement.revision});setSourceText(statement.text);setSpeakerLabel(statement.speaker_label||'');setSpeakerRole(statement.speaker_role||'unknown');setSourceSubject(statement.subject_id||'');setSourceBasis(statement.subject_basis||'');};
  const confirmSource=(statement:Statement)=>run('source-confirm:'+statement.id,async()=>{await request(base+'/statements/'+encodeURIComponent(statement.id)+'/confirm',jsonRequest({expected_revision:statement.revision}));await refresh();setSuccess('已确认这版原话记录准确。事实候选仍需单独确认，核验状态不变。');});
  const importSegment=()=>run('source-segment',async()=>{
    if(!selectedSegment)throw new Error('请先选择本案已有的非空稳定转写段。');
    const statement=await request<Statement>(base+'/statements/from-segment',jsonRequest({interview_id:selectedSegment.interview_id,
      recording_id:selectedSegment.recording_id,segment_id:selectedSegment.segment_id,segment_revision:selectedSegment.segment_revision,
      speaker_label:speakerLabel.trim()||'未知说话人',speaker_role:speakerRole,subject_id:sourceSubject||null,subject_basis:sourceSubject?sourceBasis.trim()||null:null}));
    await refresh();setStatementId(statement.id);setStatementRevision(statement.revision);setSuccess('已引用服务端核对的本案转写版本；说话人与事实归属仍需人员确认。');
  });
  const refreshSegment=(statement:Statement,segment:S3StableSegment)=>run('source-refresh:'+statement.id,async()=>{
    await request(base+'/statements/'+encodeURIComponent(statement.id)+'/refresh-segment',jsonRequest({expected_revision:statement.revision,segment_revision:segment.segment_revision}));
    await refresh();setSuccess('已明确采用更新的转写版本。相关原话与事实需要重新核对，旧版保留。');
  });
  const readSourceHistory=(statement:Statement)=>run('source-history:'+statement.id,async()=>{
    const result=await request<{statement_id:string;versions:Statement[]}>(base+'/statements/'+encodeURIComponent(statement.id)+'/history');
    setSourceHistory(previous=>({...previous,[statement.id]:result.versions}));setSourceHistoryOpen(previous=>previous===statement.id?null:statement.id);
  });
  const saveFact=(event:FormEvent)=>{event.preventDefault();void run('fact',async()=>{
    if(!selectedStatement||statementRevision===null)throw new Error('请先选择一版现场原话，再保存事实候选。');
    if(!sourceCurrent)throw new Error('原话已修订，请先重新选择来源版本并核对候选。');
    const payload={statement_id:selectedStatement.id,statement_revision:statementRevision,value_type:draft.value_type,
      value:draft.presence==='explicit_zero'?'0':draft.presence==='known'?draft.value:null,presence:draft.presence,
      unit:draft.unit_status==='explicit'?draft.unit:null,unit_status:draft.unit_status,
      period:draft.period_status==='explicit'?{kind:draft.period_kind,label:draft.period_label,start:draft.period_start||null,end:draft.period_end||null}:null,period_status:draft.period_status,
      subject_id:draft.subject_status==='explicit'?draft.subject_id||null:null,subject_status:draft.subject_status,subject_basis:draft.subject_status==='explicit'?draft.subject_basis.trim()||null:null};
    await request(editingFact?base+'/facts/'+encodeURIComponent(editingFact.id)+'/revise':base+'/facts',jsonRequest(editingFact?{...payload,expected_revision:editingFact.revision}:{...payload,field_id:draft.field_id}));
    await refresh();setEditingFact(null);setDraft(newDraft(selectedField));setSuccess('事实候选已保存，人员确认与事实核验状态分别保留。');
  });};
  const editFact=(fact:Fact)=>{
    const field=fields.find(item=>item.field_id===fact.field_id);setFieldId(fact.field_id);setEditingFact({id:fact.id,revision:fact.revision});
    setStatementId(fact.statement_id||'');setStatementRevision(fact.statement_revision??null);
    setDraft({field_id:fact.field_id,value_type:['integer','decimal','enum'].includes(fact.value_type)?fact.value_type as ValueType:'text',value:fact.value??'',presence:fact.presence,
      unit:fact.unit||'',unit_status:(fact.unit_status||'unknown') as DimensionStatus,period_status:(fact.period_status||'unknown') as DimensionStatus,
      period_kind:fact.period?.kind||'',period_label:fact.period?.label||'',period_start:fact.period?.start||'',period_end:fact.period?.end||'',subject_id:fact.subject_id||'',subject_status:fact.subject_status==='explicit'?'explicit':'unknown',subject_basis:fact.subject_basis||''});
    if(!field)setError('该字段目前不在活动问卷中，请先核对来源与版本。');
  };
  const actOnFact=(fact:Fact,action:'confirm'|'revoke')=>run(action+':'+fact.id,async()=>{
    await request(base+'/facts/'+encodeURIComponent(fact.id)+'/'+action,jsonRequest({expected_revision:fact.revision,...(basis[fact.id]?.trim()?{basis:basis[fact.id].trim()}:{})}));
    await refresh();setSuccess(action==='confirm'?'已确认这条记录准确；不代表已经核验事实或具备测算资格。':'已撤销准确确认，历史版本保留。');
  });
  const readHistory=(fact:Fact)=>run('history:'+fact.id,async()=>{const result=await request<{fact_id:string;versions:Fact[]}>(base+'/facts/'+encodeURIComponent(fact.id)+'/history');setFactHistory(previous=>({...previous,[fact.id]:result.versions}));setHistoryOpen(previous=>previous===fact.id?null:fact.id);});
  const patchDraft=<K extends keyof FactDraft>(key:K,value:FactDraft[K])=>setDraft(previous=>({...previous,[key]:value}));
  const renderFact=(fact:Fact,compact=false)=>{
    const source=statements.find(statement=>statement.id===fact.statement_id);
    const boundSource=source?.revision===fact.statement_revision?source:sourceHistory[fact.statement_id||'']?.find(version=>version.revision===fact.statement_revision);
    const confirmReady=fact.origin==='onsite'&&source?.record_confirmation==='record_accurate'&&source.revision===fact.statement_revision&&!fact.needs_review;
    return <article className="s3-fact" key={fact.id}><header><strong>{fields.find(field=>field.field_id===fact.field_id)?.label||fact.field_id} · {factValue(fact)}</strong><StatusTag value={fact.origin}/></header>
      <div className="s3-tags"><StatusTag value={fact.presence}/><StatusTag value={fact.record_confirmation}/><StatusTag value={fact.verification}/>{fact.needs_review&&<StatusTag value="needs_review" warning/>}{fact.conflict&&<span className="s3-tag warn">存在冲突</span>}</div>
      <Meta items={[["单位",factUnitText(fact)],["期间",factPeriodText(fact)],["主体归属",subjectName(fact.subject_id)],["主体范围",wording(fact.subject_scope,'来源未提供范围判断')],["版本",`事实第 ${fact.revision} 版${fact.statement_revision!=null?` / 原话第 ${fact.statement_revision} 版`:''}`],["计算资格",wording(fact.calculation_eligibility,'该事实尚未评估测算资格')]]}/>
      {boundSource?<blockquote>{boundSource.text}</blockquote>:source?<div className="s3-history"><Note kind="warn">本事实引用原话第 {fact.statement_revision??'未知'} 版，当前原话已到第 {source.revision} 版。旧引用仍保留，请查看原话历史，当前新版文字不作为旧事实的原引用。</Note><div className="s3-actions"><button disabled={!!busy} onClick={()=>void readSourceHistory(source)}>读取引用原话历史</button></div></div>:<p>事前来源：{stringValue(factSource(fact)?.source_id||factSource(fact)?.location||factSource(fact)?.locator||factSource(fact)?.snapshot_id)||'来源定位尚未提供'}。没有原件访问入口。</p>}
      {!compact&&<><p className="s3-rule-caption">准确确认仅确认记录内容，独立核验另行保留。请到“测算”页补齐本次模型所需的独立输入和核验，再复核计算资格；本条事实不自动计入评分或额度。</p>
        {fact.origin==='onsite'&&<label>本次核对说明（可选）<input value={basis[fact.id]||''} onChange={event=>setBasis(previous=>({...previous,[fact.id]:event.target.value}))} placeholder="如：已与当事人逐项核对记录"/></label>}
        <div className="s3-actions">{fact.origin==='onsite'&&<><button disabled={!!busy||!allowed} onClick={()=>editFact(fact)}>修改候选</button><button disabled={!!busy||!allowed||!confirmReady||fact.record_confirmation==='record_accurate'} title={!confirmReady?'先确认当前原话版本，并处理需重新核对状态':undefined} onClick={()=>void actOnFact(fact,'confirm')}><ShieldCheck size={13}/>确认记录准确</button><button disabled={!!busy||!allowed||fact.record_confirmation!=='record_accurate'} onClick={()=>void actOnFact(fact,'revoke')}>撤销确认</button></>}<button disabled={!!busy} onClick={()=>void readHistory(fact)}><History size={13}/>版本历史</button></div>
        {historyOpen===fact.id&&<div className="s3-history-list">{(factHistory[fact.id]||[]).map(version=><div className="s3-history-item" key={version.revision}><strong>第 {version.revision} 版 · {factValue(version)}</strong><p>{wording(version.record_confirmation)} / {wording(version.verification)} / {factUnitText(version)} / {factPeriodText(version)} / {subjectName(version.subject_id)}</p></div>)}</div>}
      </>}
    </article>;
  };
  return <section className="s3-workspace" aria-label={mode==='prepare'?'事前资料':'问卷与事实确认'}>
    <div className="s3-heading"><div><span className="dd-eyebrow">事前资料 · 只读</span><h2>{mode==='prepare'?'先看事前记载，再确定要问什么。':'按稳定问卷记录，逐项确认原话与事实。'}</h2><p>售前仅获得基础申请资料；经营细节以现场访谈为准。</p></div><button disabled={loading||!!busy} onClick={()=>void run('refresh',async()=>{await refresh();setSuccess('已重新读取本案保存状态。');})}><RefreshCw size={13} className={busy==='refresh'?'dd-spin':''}/>刷新资料与事实</button></div>
    {error&&<Note kind="error">{error}</Note>}{success&&<Note kind="success">{success}</Note>}
    {loading&&!workspace?<div className="s3-empty"><LoaderCircle size={19} className="dd-spin"/> 正在读取本案合成资料与问卷……</div>:!workspace?<div className="s3-empty">当前未取得资料状态，请刷新重试。录音控制不受此处影响。</div>:<>
      {mode==='prepare'&&<>
        <section className="s3-card"><div className="s3-heading"><div><h3>事前记载</h3><p>只读来源；有资料不等于已核验。</p></div><span className="s3-tag">{previsit?.facts.length||0} 项</span></div>
          {previsit?.facts.length?<div className="s3-facts-grid">{previsit.facts.map((fact,index)=>{
            const label=fields.find(field=>field.field_id===fact.field_id)?.label||fact.field_id;
            const unit=factUnitText(fact);
            return <div className="s3-fact-item" key={fact.id||index}><span>{label}</span>
              <strong>{factValue(fact)}{unit?` ${unit}`:''}</strong></div>;})}
          </div>:<div className="s3-empty">尚无获准可读的事前字段。可以继续合成访谈，未知保持未知。</div>}
        </section>
        <details className="s3-card s3-dev-details"><summary>核查视图（处理状态、来源定位与字段接入处置）</summary>
          <p className="s3-rule-caption" style={{margin:"6px 0"}}>演示装载入口：为当前案件绑定本地合成快照（真实上游未接通）。</p>
          <div className="s3-grid"><section className="s3-card"><div className="s3-heading"><div><h3>加载已准备的合成快照</h3><p>这是本地只读样例接入，不上传附件，也不连接真实业务系统。</p></div><FileText size={17}/></div>
          <label>合成案例<select value={fixtureId} onChange={event=>setFixtureId(event.target.value)} disabled={!!busy||!!previsit?.fixture_id}>{examples.map(example=><option key={example.fixture_id} value={example.fixture_id}>{example.label||example.fixture_id}</option>)}</select></label>
          <p className="s3-rule-caption">{examples.find(example=>example.fixture_id===fixtureId)?.description||'合成样例列表尚未读取。'}</p>
          {previsit?.fixture_id&&<p className="s3-rule-caption">本案已绑定此合成案例。演示另一案例时请新建案件，不能按相同姓名改绑。</p>}
          <label>读取情形<select value={variant} onChange={event=>setVariant(event.target.value)} disabled={!!busy}><option value="base">读取基础版本</option><option value="updated">读取更新版本</option><option value="unavailable">模拟来源暂不可达</option><option value="denied">模拟无读取权限</option></select></label>
          <div className="s3-actions"><button className="dd-primary" disabled={!allowed||!!busy||!fixtureId} onClick={()=>void loadPrevisit()}>{busy==='previsit'?<LoaderCircle size={13} className="dd-spin"/>:<RefreshCw size={13}/>}加载合成资料状态</button></div>
          </section><section className="s3-card"><div className="s3-heading"><h3>本次资料状态</h3><StatusTag value={previsit?.state} warning={previsit?.state!=='available'}/></div>
          <Meta items={[["数据来源","本地合成快照；真实上游未接通"],["快照版本",previsit?.context_version??'尚未加载'],["快照编号",previsit?.snapshot_id||'尚未加载'],["数据截至",previsit?.data_as_of||'上游未提供'],["快照首次读取",dateText(previsit?.read_at||previsit?.loaded_at||snapshotLoadedAt)],["使用状态",previsit?.state==='unloaded'?'尚未读取':previsit?.cached?'缓存快照，不能视为实时同步':previsit?.state==='available'?'本次读取的合成快照':previsit?.state==='denied'?'权限拒绝，未展示事前正文':'来源不可达，当前无可用快照']]}/>
          {(previsit?.cached||['unavailable','denied'].includes(previsit?.state||''))&&<div className="s3-history"><Note kind="warn">{previsit?.cached?'当前保留既有快照及其截至日；来源变化后的相关事实需要重新核对。':'没有获准可用的当前资料，不据此补全字段。'} 缺附件请回到上游资料准备流程处理。</Note></div>}
          <details className="s3-history"><summary>快照读取历史（{previsit?.history.length||0}）</summary><div className="s3-history-list">{previsit?.history.map((entry,index)=><div className="s3-history-item" key={stringValue(entry.id||entry.snapshot_id)||index}><strong>{stringValue(entry.snapshot_id||entry.fixture_id)||`读取记录 ${index+1}`}</strong><p>版本 {stringValue(entry.context_version)||'未知'} · {wording(entry.state||entry.variant)} · {dateText(entry.read_at||entry.loaded_at||entry.created_at)}</p><p>{entry.cached?'缓存记录':'保留历史，未覆盖为当前事实'}</p><div className="s3-actions"><button disabled={!!busy||previsit.state==='denied'||!entry.snapshot_id} onClick={()=>void readSnapshot(String(entry.snapshot_id))}>只读查看此快照</button></div></div>)}{!previsit?.history.length&&<p className="s3-rule-caption">尚无读取历史。</p>}</div></details>
          </section></div>
          {historySnapshot&&previsit?.state!=='denied'&&<section className="s3-card"><div className="s3-heading"><div><h3>历史快照 · 第 {historySnapshot.context_version} 版</h3><p>仅供回看旧版本，不会替换当前快照，也不是原件访问入口。</p></div><button onClick={()=>setHistorySnapshot(null)}>关闭历史正文</button></div><Meta items={[["快照编号",historySnapshot.snapshot_id],["数据截至",historySnapshot.context.data_as_of||'未提供'],["读取时间",dateText(historySnapshot.loaded_at)],["状态",historySnapshot.cached||previsit?.state==='unavailable'?'缓存历史，不能视为实时同步':'保留的历史快照，只读']]}/>
          <div className="s3-history"><div className="s3-table-wrap"><table className="s3-table"><thead><tr><th>历史字段与值</th><th>单位 / 期间</th><th>主体</th><th>准确确认 / 核验</th></tr></thead><tbody>{historySnapshot.context.facts.map((fact,index)=>{
          const party=historySnapshot.context.parties.find(item=>item.party_id===fact.subject_id),household=historySnapshot.context.households.find(item=>item.household_id===fact.subject_id),unit=historySnapshot.context.business_units.find(item=>item.business_unit_id===fact.subject_id);
          return <tr key={fact.id||index}><td>{fields.find(field=>field.field_id===fact.field_id)?.label||fact.field_id}：{factValue(fact)}<small>{wording(fact.presence)}</small></td><td>{factUnitText(fact)}<small>{factPeriodText(fact)}</small></td><td>{party?.synthetic_label||household?.synthetic_label||unit?.synthetic_label||fact.subject_id||'主体归属未知'}</td><td>{wording(fact.record_confirmation)}<small>{wording(fact.verification)}</small></td></tr>;
          })}</tbody></table></div></div>
          {!!historySnapshot.context.materials.length&&<details className="s3-history"><summary>历史材料状态（{historySnapshot.context.materials.length}）</summary><div className="s3-history-list">{historySnapshot.context.materials.map((material,index)=><div className="s3-history-item" key={stringValue(material.material_id)||index}>{stringValue(material.label||material.material_id)||`材料 ${index+1}`} · {wording(material.processing_state)} · {wording(material.access_status)} · {wording(material.verification)}</div>)}</div></details>}
          </section>}
          {!!previsit?.facts.length&&<div className="s3-table-wrap"><table className="s3-table"><thead><tr><th>字段与值</th><th>主体</th><th>准确确认 / 核验</th><th>来源定位</th></tr></thead><tbody>{previsit.facts.map((fact,index)=><tr key={fact.id||index}><td>{fields.find(field=>field.field_id===fact.field_id)?.label||fact.field_id}<strong>：{factValue(fact)}</strong><small>{wording(fact.presence)}</small></td><td>{subjectName(fact.subject_id)}<small>{wording(fact.subject_scope,'来源未提供范围判断')}</small></td><td>{wording(fact.record_confirmation)}<small>{wording(fact.verification)}</small></td><td>{stringValue(factSource(fact)?.source_id||factSource(fact)?.location||factSource(fact)?.locator)||'来源位置未提供'}<small>{stringValue(factSource(fact)?.locator)||'未提供原件访问入口'}</small></td></tr>)}</tbody></table></div>}
          {!!previsit?.materials.length&&<div className="s3-history-list">{previsit.materials.map((material,index)=><div className="s3-history-item" key={stringValue(material.material_id)||index}><strong>{stringValue(material.label||material.material_id)||`材料 ${index+1}`}</strong><p>处理状态：{wording(material.processing_state)}；访问：{wording(material.access_status)}；核验：{wording(material.verification)}</p></div>)}</div>}
          <div className="s3-groups">{['interview','upstream_only','extension_required','not_supported'].map(handling=>{
          const group=fields.filter(field=>field.handling===handling);return <details className="s3-group" key={handling}><summary>{wording(handling)}<span>{group.length} 项</span></summary><div className="s3-group-list">{group.map(field=><div className="s3-field" key={field.field_id}><strong>{field.label}</strong><p>{field.notes||field.question_text}</p><small>字段：{field.field_id}；原 key：{field.question_keys?.join('、')||'未提供'}</small><small>原问题：{field.source_question_ids?.join('、')||'未提供'}</small><small>来源位置：{field.source_locations?.filter(source=>source.location).map(source=>source.location).join('；')||'未提供'}（索引说明，不是原件链接）</small></div>)}</div></details>;
        })}</div>
        </details>
      </>}
      {mode==='interview'&&previsit?.state==='denied'&&<Note kind="warn">当前没有读取本案来源与事实内容的权限，已关闭旧正文和编辑内容。请先回到“准备”核对资料权限，不能从历史旁路查看。</Note>}
      {mode==='interview'&&previsit?.state!=='denied'&&<>
        {previsit?.state==='unloaded'&&<Note kind="warn">本案尚未加载事前合成快照。可在“准备”选择样例；未提供的主体、单位和期间保持未知，不自动归属申请人。</Note>}
        {!!conflictFields.length&&<div className="s3-conflict"><h4>有 {conflictFields.length} 个字段存在未解决冲突</h4><p className="dd-help">事前记载与现场陈述同时保留，不能直接作为已满足条件的测算输入。</p><div className="s3-actions">{conflictFields.map(id=><button key={id} onClick={()=>{const field=fields.find(item=>item.field_id===id);if(field)selectField(field);}}>{fields.find(field=>field.field_id===id)?.label||id}</button>)}</div></div>}
        <div className="s3-interview-grid"><aside className="s3-card"><div className="s3-heading"><div><h3><ClipboardList size={15}/> 证据核查字段</h3><p>仅列出已有原话或事实记录、需要核对证据的字段；问卷待问项在现场问卷作答，不在此重复。</p></div></div><div className="s3-tags"><span className="s3-tag">{verifiable.length} 项有记录待核查</span><span className="s3-tag">{workspace.questionnaire.progress.record_accurate??0} 项记录准确</span></div>
          <div className="s3-question-list">{verifiable.map((field,index)=><button key={field.field_id} className={selectedField?.field_id===field.field_id?'selected':''} onClick={()=>selectField(field)}><strong>{String(index+1).padStart(2,'0')} · {field.label}</strong><small>{field.question_text}</small><StatusTag value={field.completion} warning={field.completion==='needs_review'}/>{field.applicability==='unknown'&&<small>适用条件待核对：{field.applicability_reason||'已有信息不足以判定。'}</small>}</button>)}{!verifiable.length&&<div className="s3-empty">尚无需要核查的原话或事实记录；先在现场问卷作答或引用录音原话。</div>}</div>
          <details className="s3-history"><summary>明确不适用的访谈条目</summary><div className="s3-history-list">{fields.filter(field=>field.handling==='interview'&&field.applicability==='not_applicable').map(field=><div className="s3-history-item" key={field.field_id}>{field.label}：{field.applicability_reason||'按当前已知条件不适用'}</div>)}</div></details>
        </aside><div className="s3-stack"><section className="s3-card"><div className="s3-heading"><div><h3>{editingSource?'修改现场原话，保留历史版本':'记录合成口述或引用已有转写'}</h3><p>手工记录明确标为未录音；说话人角色与事实主体分开填写。</p></div><span className="s3-tag warn">仅合成开发</span></div>
          {!editingSource&&<label>原话来源方式<select value={sourceMode} disabled={!!busy} onChange={event=>setSourceMode(event.target.value as 'manual'|'asr')}><option value="manual">手工合成口述（未录音）</option><option value="asr">引用本案已有稳定转写</option></select></label>}
          <form onSubmit={sourceMode==='asr'&&!editingSource?event=>{event.preventDefault();void importSegment();}:saveSource}><div className="s3-form-grid">
            {sourceMode==='asr'&&!editingSource?<label className="wide">选择本案稳定转写段<select value={segmentKey} disabled={!!busy} onChange={event=>setSegmentKey(event.target.value)}><option value="">请选择，服务端将再次核对来源</option>{stableSegments.map(segment=><option key={segmentOptionKey(segment)} value={segmentOptionKey(segment)}>第 {segment.segment_revision} 版 · {segment.start_seconds.toFixed(1)}—{segment.end_seconds.toFixed(1)}秒 · {segment.text.slice(0,55)}</option>)}</select>{selectedSegment?<blockquote className="s3-segment-preview">{selectedSegment.text}</blockquote>:<p className="s3-rule-caption">当前可引用 {stableSegments.length} 个非空稳定段；没有稳定段时可先使用手工合成口述，不需要开录。</p>}</label>:<label className="wide">现场原话<textarea aria-label="合成口述原话" required value={sourceText} disabled={!!busy} onChange={event=>setSourceText(event.target.value)} placeholder="例如：我家今年种了十一点五亩柑橘。请使用虚构内容。"/></label>}
            <label>说话人称呼（可未知）<input value={speakerLabel} disabled={!!busy} onChange={event=>setSpeakerLabel(event.target.value)} placeholder="未知说话人"/></label><label>说话人角色<select value={speakerRole} disabled={!!busy} onChange={event=>setSpeakerRole(event.target.value)}><option value="unknown">未知，尚未确认身份</option><option value="applicant">申请人</option><option value="household_member">家属</option><option value="investigator">调查员</option><option value="third_party">第三方</option></select></label>
            <label>原话涉及的主体<select value={sourceSubject} disabled={!!busy} onChange={event=>setSourceSubject(event.target.value)}><option value="">主体归属未知</option>{subjects.map(subject=><option key={subject.id} value={subject.id}>{subject.label} · {subject.kind}</option>)}</select></label><label>主体判断依据<input value={sourceBasis} required={!!sourceSubject} disabled={!!busy||!sourceSubject} onChange={event=>setSourceBasis(event.target.value)} placeholder={sourceSubject?'请说明选择该主体的依据':'如未明确说明，保持空白'}/></label></div>
            <div className="s3-actions"><button className="dd-primary" disabled={!allowed||!!busy||(sourceMode==='asr'&&!editingSource?!selectedSegment:!sourceText.trim())} type="submit"><Save size={13}/>{busy==='source'||busy==='source-segment'?'正在保存':sourceMode==='asr'&&!editingSource?'引用此转写段，待确认':'保存原话，待确认'}</button>{editingSource&&<button type="button" disabled={!!busy} onClick={()=>{setEditingSource(null);setSourceText('');setSpeakerLabel('');setSpeakerRole('unknown');setSourceSubject('');setSourceBasis('');}}>取消修改</button>}</div>
          </form>
          <div className="s3-source-list">{statements.map(statement=>{
            const latestSegment=statement.source_ref&&stableSegments.find(segment=>segment.recording_id===statement.source_ref!.recording_id&&segment.segment_id===statement.source_ref!.segment_id);
            const newerSegment=latestSegment&&latestSegment.segment_revision!==statement.source_ref?.segment_revision;
            return <article className={'s3-source '+(statement.id===statementId?'selected':'')} key={statement.id}><header><strong>{statement.speaker_label||'未知说话人'} · 第 {statement.revision} 版</strong><StatusTag value={statement.record_confirmation}/></header><p>{wording(statement.speaker_role,'身份未知')}；主体：{subjectName(statement.subject_id)}；{statement.has_audio?'引用本案录音转写':'合成口述，未录音'}</p><blockquote>{statement.text}</blockquote>
              {statement.source_ref&&<p>录音来源：{statement.source_ref.recording_id} / 片段 {statement.source_ref.segment_id} 第 {statement.source_ref.segment_revision} 版；近似音频范围 {(statement.source_ref.audio_start_frame/16000).toFixed(1)}—{(statement.source_ref.audio_end_frame/16000).toFixed(1)} 秒。回听入口保留在下方录音区。</p>}
              {(statement.needs_review||newerSegment)&&<Note kind="warn">来源转写或引用状态需要重新核对，旧原话与旧事实保留。{newerSegment?'可明确采用新的稳定转写版本。':''}</Note>}
              <div className="s3-actions"><button disabled={!!busy} onClick={()=>useStatement(statement)}>{statement.id===statementId&&statementRevision===statement.revision?'已选当前版本':'使用此版本原话'}</button>{statement.kind==='manual'&&<button disabled={!allowed||!!busy} onClick={()=>editSource(statement)}>修改原话</button>}<button disabled={!allowed||!!busy||statement.record_confirmation==='record_accurate'||statement.needs_review} onClick={()=>void confirmSource(statement)}>确认原话记录准确</button>{newerSegment&&latestSegment&&<button disabled={!allowed||!!busy} onClick={()=>void refreshSegment(statement,latestSegment)}>采用最新转写版本</button>}<button disabled={!!busy} onClick={()=>void readSourceHistory(statement)}><History size={13}/>原话历史</button></div>
              {sourceHistoryOpen===statement.id&&<div className="s3-history-list">{(sourceHistory[statement.id]||[]).map(version=><div className="s3-history-item" key={version.revision}><strong>原话第 {version.revision} 版 · {wording(version.record_confirmation)}</strong><blockquote>{version.text}</blockquote><p>{version.speaker_label||'未知说话人'} / {wording(version.speaker_role,'角色未知')} / {subjectName(version.subject_id)} / {version.has_audio?'有录音来源':'未录音'}</p></div>)}</div>}
            </article>;
          })}{!statements.length&&<p className="s3-rule-caption">尚无现场原话。先保存合成口述，再创建事实候选。</p>}</div>
        </section>
        <section className="s3-card"><div className="s3-heading"><div><h3>{editingFact?'修订事实候选':'整理事实候选'} · {selectedField?.label||'先选择问题'}</h3><p>{selectedField?.question_text||'字段来源与主体归属需要明确。'}</p></div><span className="s3-tag">尚未事实核验</span></div>
          {selectedStatement?<Note kind={sourceCurrent?'info':'warn'}>当前来源：{selectedStatement.speaker_label||'未知说话人'}，原话第 {statementRevision??'未知'} 版。{factDraftChangedSource?'来源已修订，请重新选择当前原话版本再保存。':sourceConfirmed?'原话记录准确已确认；事实候选仍需单独确认。':'原话尚未确认记录准确。'}</Note>:<Note kind="warn">尚未选择现场原话。请在上方点击“使用此版本原话”。</Note>}
          <form onSubmit={saveFact}><div className="s3-form-grid"><label>值的状态<select value={draft.presence} disabled={!!busy} onChange={event=>patchDraft('presence',event.target.value as Presence)}>{(['known','explicit_zero','unknown','missing','not_applicable','withheld'] as Presence[]).filter(state=>!(selectedField?.value_options?.length&&state==='explicit_zero')).map(state=><option key={state} value={state}>{wording(state)}</option>)}</select></label><label>值的类型（题库固定）<select value={draft.value_type} disabled title="类型由当前字段的题库版本确定"><option value={draft.value_type}>{wording(draft.value_type)}</option></select></label>
            <label className="wide">实际记录的值{selectedField?.value_options?.length?<select aria-label="事实候选值" value={draft.presence==='known'?draft.value:''} disabled={!!busy||draft.presence!=='known'} required={draft.presence==='known'} onChange={event=>patchDraft('value',event.target.value)}><option value="">请选择已明确的品类，不猜选</option>{selectedField.value_options.map(option=><option key={option.value} value={option.value}>{option.label}</option>)}</select>:<input aria-label="事实候选值" value={draft.presence==='explicit_zero'?'0':draft.presence==='known'?draft.value:''} disabled={!!busy||draft.presence!=='known'} required={draft.presence==='known'} onChange={event=>patchDraft('value',event.target.value)} placeholder={draft.presence==='known'?'按原话填写，不自动换算':'未知、未提供或不适用不会补成0'}/>}</label>{selectedField?.field_id==='crop.main.scale_category'&&<p className="s3-rule-caption wide">这是独立的规模评分品类，原大／中／小分类仍待接入。选择有原话依据的主营经营单元；品类单位选择“不适用”，期间与主营面积逐项核对。“其他”不能代替未知。</p>}
            <label>单位状态<select value={draft.unit_status} disabled={!!busy} onChange={event=>patchDraft('unit_status',event.target.value as DimensionStatus)}><option value="unknown">单位未知</option><option value="explicit">原话已明确单位</option><option value="not_applicable">单位明确不适用</option></select></label><label>单位<input value={draft.unit} required={draft.unit_status==='explicit'} disabled={!!busy||draft.unit_status!=='explicit'} onChange={event=>patchDraft('unit',event.target.value)} placeholder="如：元、万元、亩；不自动转换"/></label>
            <label>期间状态<select value={draft.period_status} disabled={!!busy} onChange={event=>patchDraft('period_status',event.target.value as DimensionStatus)}><option value="unknown">期间未知</option><option value="explicit">原话已明确期间</option><option value="not_applicable">期间明确不适用</option></select></label><label>期间类型<select value={draft.period_kind} required={draft.period_status==='explicit'} disabled={!!busy||draft.period_status!=='explicit'} onChange={event=>patchDraft('period_kind',event.target.value)}><option value="">请选择期间类型</option>{['year','season','as_of','duration','purchase_date'].map(kind=><option key={kind} value={kind}>{wording(kind)}</option>)}</select></label><label className="wide">期间说明<input value={draft.period_label} required={draft.period_status==='explicit'} disabled={!!busy||draft.period_status!=='explicit'} onChange={event=>patchDraft('period_label',event.target.value)} placeholder="如：2026年、过去12个月、截至某日；不自动年化"/></label>
            <label>期间开始日期<input aria-label="期间开始日期" type="date" value={draft.period_start} disabled={!!busy||draft.period_status!=='explicit'} onChange={event=>patchDraft('period_start',event.target.value)}/></label><label>期间结束日期<input aria-label="期间结束日期" type="date" value={draft.period_end} disabled={!!busy||draft.period_status!=='explicit'} onChange={event=>patchDraft('period_end',event.target.value)}/></label>
            <label>主体归属状态<select value={draft.subject_status} disabled={!!busy} onChange={event=>patchDraft('subject_status',event.target.value as 'unknown'|'explicit')}><option value="unknown">主体归属未知</option><option value="explicit">人员明确主体归属</option></select></label><label>事实属于谁<select value={draft.subject_id} required={draft.subject_status==='explicit'} disabled={!!busy||draft.subject_status!=='explicit'} onChange={event=>patchDraft('subject_id',event.target.value)}><option value="">请选择主体，不默认申请人</option>{subjects.map(subject=><option key={subject.id} value={subject.id}>{subject.label} · {subject.kind}</option>)}</select></label><label className="wide">事实主体判断依据<input value={draft.subject_basis} required={draft.subject_status==='explicit'} disabled={!!busy||draft.subject_status!=='explicit'} onChange={event=>patchDraft('subject_basis',event.target.value)} placeholder="如：原话明确说明是申请人家庭本年度费用"/></label></div>
            <div className="s3-actions"><button className="dd-primary" disabled={!allowed||!!busy||!draft.field_id||!selectedStatement||!sourceCurrent} type="submit"><Save size={13}/>{busy==='fact'?'正在保存':editingFact?'保存修订，重新待确认':'保存事实候选'}</button>{editingFact&&<button disabled={!!busy} type="button" onClick={()=>{setEditingFact(null);setDraft(newDraft(selectedField));}}>取消修订</button>}</div>
          </form>
          {fieldFacts.some(fact=>fact.conflict)&&<div className="s3-history"><Note kind="warn">此字段存在冲突。以下事前记载与现场陈述并列保留，人员确认记录准确不会自动解决事实冲突。</Note></div>}
          <div className="s3-fact-list">{fieldFacts.map(fact=>renderFact(fact))}{!fieldFacts.length&&<div className="s3-empty">该字段尚无事实记录。没有说明不等于0或不适用。</div>}</div>
        </section></div></div>
      </>}
    </>}
  </section>;
}
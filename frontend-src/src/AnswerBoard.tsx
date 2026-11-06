import {useCallback, useEffect, useMemo, useState} from 'react';
import {AlertCircle, Check, CheckCheck, LoaderCircle, Plus, RefreshCw, Sparkles, X} from 'lucide-react';
import './AnswerBoard.css';

const API='/api/due-diligence';

type StructureMember={key:string;label:string;value_type:string;unit?:string|null};
type ListStructure={id:string;label:string;cardinality?:string;members:StructureMember[]};
type Question={field_id:string;label:string;question_text?:string|null;section?:string|null;
  value_type:string;unit?:string|null;required_class:string;list_structure?:ListStructure|null;
  required_source_markers?:string;conditions?:{state?:string}|null;source?:string|null};
type AnswerValue={text?:string;number?:number;boolean?:boolean;date?:string;choice?:string;unit?:string};
type AnswerItem={item_id:string;status:'active'|'revoked';note?:string|null;members:Record<string,unknown>};
type Answer={answer_id:string;field_id:string;revision:number;status:'draft'|'confirmed'|'not_applicable';
  value:AnswerValue|null;items:AnswerItem[]|null;basis:{type:string;id:string;note?:string|null}[]|null;
  not_applicable_reason?:string|null;updated_at:string;actor:string;
  period?:{kind?:string;start?:string;end?:string;label?:string}|null};
type Candidate={candidate_id:string;field_id:string;value_kind:'text'|'number'|'boolean'|'date';value:unknown;
  unit?:string|null;period?:string|null;subject?:string|null;quote:string;recording_id:string;segment_id:string;
  segment_revision:number;reason:string;catalog_version:string;created_at:string};
type Suggestion={suggestion_id:string;field_id:string;question:string;reason:string;
  recording_id:string;segment_id:string;segment_revision:number;quote:string;created_at:string};
type RiskSignal={suggestion_id:string;topic:string;question:string;reason:string;
  recording_id:string;segment_id:string;segment_revision:number;quote:string;created_at:string};
type CandidateView={case_id:string;candidates:Candidate[];suggestions?:Suggestion[];risk_signals?:RiskSignal[];
  latest_run:{run_id:string;created_at:string;
  segments_sent?:number;rejected_count?:number;rejected?:{reason:string;field_id?:string}[]}|null};
type PrevisitReference={fact_id:string;fact_revision:number;previsit_value:unknown;presence?:string;
  onsite_value?:unknown;conflict:boolean};
type Progress={required:{done:number;applicable:number};optional:{done:number;total:number};
  pending_judgement:number;not_applicable:number;condition_inapplicable:number;
  pending_fields:string[];not_applicable_fields:string[];catalog_version:string};
type Board={case_id:string;catalog_version:string;questions:Question[];answers:Answer[];progress:Progress;
  previsit_reference?:Record<string,PrevisitReference>};
type BridgeGap={model_field:string;answer_field:string;reason:string;detail:string};
type Bridge={mapped:Record<string,unknown>;gaps:BridgeGap[]};
type Props={caseId:string;allowed:boolean};

async function request<T>(url:string,init?:RequestInit):Promise<T>{
  const response=await fetch(url,init);
  const body=await response.json().catch(()=>null);
  if(!response.ok){const detail=body?.detail;const message=typeof detail==='object'?detail?.message||JSON.stringify(detail):detail||response.statusText;
    throw Object.assign(new Error(String(message)),{status:response.status});}
  return body as T;
}

export default function AnswerBoard({caseId,allowed}:Props){
  const [board,setBoard]=useState<Board|null>(null);
  const [candidateView,setCandidateView]=useState<CandidateView|null>(null);
  const [bridge,setBridge]=useState<Bridge|null>(null);
  const [extracting,setExtracting]=useState(false),[adopting,setAdopting]=useState(''),[adoptingSug,setAdoptingSug]=useState('');
  const [bulkAdopting,setBulkAdopting]=useState(false);
  const [loading,setLoading]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const [filter,setFilter]=useState<'all'|'unanswered'|'answered'>('all');

  const refresh=useCallback(async(showLoading=true)=>{
    if(showLoading)setLoading(true);
    try{
      const [nextBoard,nextCandidates,nextBridge]=await Promise.all([
        request<Board>(`${API}/cases/${caseId}/answer-board`),
        request<CandidateView>(`${API}/cases/${caseId}/extraction/candidates`),
        request<Bridge>(`${API}/cases/${caseId}/calculation/v2/answers-bridge`).catch(()=>null)]);
      setBoard(nextBoard);setCandidateView(nextCandidates);setBridge(nextBridge);setError('');
    }catch(exc){setError((exc as Error).message||'读取问卷失败。');}
    finally{if(showLoading)setLoading(false);}
  },[caseId]);
  useEffect(()=>{setBoard(null);void refresh();},[refresh]);
  useEffect(()=>{
    const onFollowup=()=>void refresh(false);
    window.addEventListener('due-diligence:followup-changed',onFollowup);
    return ()=>window.removeEventListener('due-diligence:followup-changed',onFollowup);
  },[refresh]);

  const byAnswer=useMemo(()=>{
    const map:Record<string,Answer>={};
    for(const answer of board?.answers||[])if(!(answer.field_id in map))map[answer.field_id]=answer;
    return map;
  },[board]);
  const candidatesByField=useMemo(()=>{
    const map:Record<string,Candidate[]>={};
    for(const candidate of candidateView?.candidates||[])(map[candidate.field_id]??=[]).push(candidate);
    return map;
  },[candidateView]);

  async function runExtraction(){
    if(!allowed||extracting)return;
    setExtracting(true);setError('');setNotice('');
    try{
      const result=await request<{deduplicated:boolean;candidates_accepted:number;candidates_rejected:number;
        segments_sent:number;suggestions?:Suggestion[];risk_signals?:RiskSignal[]}>(`${API}/cases/${caseId}/extraction/run`,{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
      const sugCount=(result.suggestions||[]).length+(result.risk_signals||[]).length;
      setNotice(result.deduplicated?'原话无变化：沿用上次候选，不重复调用模型。'
        :`提取完成：${result.candidates_accepted} 条候选通过证据校验，${result.candidates_rejected} 条被拒绝${sugCount?`，补问建议 ${sugCount} 条`:''}。`);
      await refresh(false);
    }catch(exc){setError((exc as Error).message||'提取失败。');await refresh(false);}
    finally{setExtracting(false);}
  }

  async function adoptSuggestion(suggestion:Suggestion|RiskSignal){
    if(!allowed||adoptingSug)return;
    setAdoptingSug(suggestion.suggestion_id);setError('');setNotice('');
    try{
      await request(`${API}/cases/${caseId}/extraction/suggestions/adopt`,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({suggestion_id:suggestion.suggestion_id})});
      setNotice('补问建议已采纳，已作为新问题加入问卷。');
      window.dispatchEvent(new Event('due-diligence:followup-changed'));
      await refresh(false);
    }catch(exc){
      setError((exc as Error).message||'采纳补问建议失败。');
      await refresh(false);
    }finally{setAdoptingSug('');}
  }

  async function adoptAll(){
    if(!allowed||bulkAdopting)return;
    setBulkAdopting(true);setError('');setNotice('');
    try{
      const result=await request<{adopted_count:number;skipped_answered:string[];conflict_fields:string[]}>(
        `${API}/cases/${caseId}/extraction/candidates/adopt-all`,{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
      const conflict=result.conflict_fields.length?`，${result.conflict_fields.length} 个字段有多个候选需人工选择`:'';
      const skipped=result.skipped_answered.length?`，${result.skipped_answered.length} 个字段已答跳过`:'';
      setNotice(`已确认采纳 ${result.adopted_count} 条候选${skipped}${conflict}。`);
      window.dispatchEvent(new Event('due-diligence:followup-changed'));
      await refresh(false);
    }catch(exc){
      setError((exc as Error).message||'批量采纳失败。');
      await refresh(false);
    }finally{setBulkAdopting(false);}
  }

  async function submit(fieldId:string,payload:Record<string,unknown>,existing?:Answer){
    if(!allowed)return;
    setNotice('');
    try{
      if(existing)await request(`${API}/cases/${caseId}/answers/${existing.answer_id}`,{
        method:'PUT',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...payload,expected_revision:existing.revision})});
      else await request(`${API}/cases/${caseId}/answers`,{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({field_id:fieldId,...payload})});
      setNotice('已保存。');await refresh(false);
    }catch(exc){
      const message=(exc as Error).message||'保存失败。';
      setError(/已被他人修改|revision/i.test(message)?'该答案已被其他修改更新，请刷新后重试。':message);
      await refresh(false);
    }
  }

  async function adopt(candidate:Candidate,status:'draft'|'confirmed'){
    if(!allowed||adopting)return;
    setAdopting(candidate.candidate_id);setError('');setNotice('');
    try{
      await request(`${API}/cases/${caseId}/answers/adopt-candidate`,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({candidate_id:candidate.candidate_id,status,actor:'workbench_user'})});
      setNotice(status==='confirmed'?'已采纳并确认该候选。':'已采纳为草稿，待人工核对后确认。');
      await refresh(false);
    }catch(exc){
      const message=(exc as Error).message||'采纳失败。';
      setError(/已被他人修改|已有答案/.test(message)?'该问题已有答案记录，请在原答案上修订。':message);
      await refresh(false);
    }finally{setAdopting('');}
  }

  const progress=board?.progress;
  function jumpToQuestion(fieldId:string){
    setFilter('all'); // the target may be hidden by the current filter
    window.setTimeout(()=>{
      const element=document.getElementById('q-'+fieldId);
      if(!element)return;
      element.scrollIntoView({behavior:'smooth',block:'center'});
      element.classList.remove('ab-flash');
      void element.offsetWidth; // restart the animation
      element.classList.add('ab-flash');
    },60);
  }
  const labelOf=(fieldId:string)=>{
    const question=(board?.questions||[]).find(item=>item.field_id===fieldId||item.field_id.startsWith('followup.'+fieldId+'.'));
    return question?question.label:fieldId;
  };
  const counts=useMemo(()=>{
    const questions=board?.questions||[];
    const answered=questions.filter(question=>byAnswer[question.field_id]).length;
    return {all:questions.length,answered,unanswered:questions.length-answered};
  },[board,byAnswer]);
  const grouped=useMemo(()=>{
    const out:Record<string,Question[]>={};
    for(const q of board?.questions||[]){
      if(filter==='unanswered'&&byAnswer[q.field_id])continue;
      if(filter==='answered'&&!byAnswer[q.field_id])continue;
      (out[q.section||'未分组']??=[]).push(q);
    }
    return out;
  },[board,filter,byAnswer]);

  return <section className="ab-board" aria-label="现场问卷与答案">
    <div className="ab-heading">
      <span className="ab-title">现场问卷</span>
      <div className="ab-heading-actions">
        <button disabled={!allowed||extracting||loading} onClick={()=>void runExtraction()}>
          {extracting?<LoaderCircle size={13} className="dd-spin"/>:<Sparkles size={13}/>}从录音提取候选</button>
        <button disabled={!allowed||!(candidateView?.candidates||[]).length||bulkAdopting||loading} onClick={()=>void adoptAll()}
          title="一键确认采纳所有单一候选；已答字段跳过，多候选字段留人工选择">
          {bulkAdopting?<LoaderCircle size={13} className="dd-spin"/>:<CheckCheck size={13}/>}全部采纳候选</button>
        <button disabled={loading} onClick={()=>void refresh()}><RefreshCw size={13} className={loading?'dd-spin':''}/>刷新</button>
      </div>
    </div>
    {board&&<div className="ab-ready" role="status" aria-label="额度就绪度">
      <div className="ab-ready-main">
        <strong>{counts.answered}</strong><span>已答 / {counts.all} 题</span>
        <em>必问 {progress?progress.required.done:'–'}/{progress?progress.required.applicable:'–'} · 选问 {progress?progress.optional.done:'–'}/{progress?progress.optional.total:'–'}</em>
      </div>
      {bridge&&bridge.gaps.length===0
        ?<div className="ab-ready-ok"><Check size={14}/> 答案已满足测算输入，可以生成授信额度
            <button disabled={!allowed} title="跳到报告页运行：带入确认答案 → 模型评分 → 生成额度"
              onClick={()=>window.dispatchEvent(new Event('due-diligence:go-generate'))}>一键生成额度</button></div>
        :<div className="ab-ready-miss">
            <span>生成额度还差 <strong>{bridge?bridge.gaps.length:'–'}</strong> 项：</span>
            {(bridge?.gaps||[]).slice(0,6).map(gap=>{
              const answered=!!byAnswer[gap.answer_field];
              return <button key={gap.answer_field+gap.reason}
                className={'ab-ready-gap'+(answered?' fix':'')}
                title={gap.detail||gap.reason} onClick={()=>jumpToQuestion(gap.answer_field)}>
                {labelOf(gap.answer_field)}·{answered?'已答需补':'未答'}·{gapReason(gap.reason)}</button>;})}
            {bridge&&bridge.gaps.length>6&&<span className="ab-ready-gap more">等 {bridge.gaps.length-6} 项…</span>}
          </div>}
    </div>}
    {extracting&&<div className="dd-ai-live" role="status">
      <span className="dd-ai-orb"><i>AI</i></span>
      <span className="dd-ai-copy"><strong>AI 正在分析录音</strong><small>正在把文字记录匹配到问卷题目并挖掘风险线索，约需 10~30 秒，请稍候</small></span>
      <span className="dd-ai-wave" aria-hidden="true"><i/><i/><i/><i/><i/><i/><i/></span>
    </div>}
    {board&&<div className="ab-filter" role="group" aria-label="筛选问题">
      {([['all','全部'],['unanswered','未答'],['answered','已答']] as const).map(([key,label])=>
        <button key={key} className={'ab-filter-chip'+(filter===key?' active':'')} aria-pressed={filter===key}
          onClick={()=>setFilter(key)}>{label} {counts[key]}</button>)}
    </div>}
    {progress&&<div className="ab-progress" role="status">
      <div className="ab-progress-item"><strong>{progress.required.done}/{progress.required.applicable}</strong><span>必问</span></div>
      <div className="ab-progress-item"><strong>{progress.optional.done}/{progress.optional.total}</strong><span>选问</span></div>
      <div className="ab-progress-item warn"><strong>{progress.pending_judgement}</strong><span>待判断</span></div>
      <div className="ab-progress-item"><strong>{progress.not_applicable}</strong><span>不适用</span></div>
    </div>}
    {error&&<div className="ab-error" role="alert"><AlertCircle size={14}/>{error}</div>}
    {notice&&<div className="ab-notice" role="status"><Check size={14}/>{notice}</div>}
    {(candidateView?.suggestions||[]).length>0&&<div className="ab-suggest" aria-label="补问建议">
      <div className="ab-suggest-head"><Sparkles size={12}/>来自录音的补问建议<span>{candidateView!.suggestions!.length}</span></div>
      {candidateView!.suggestions!.map(suggestion=>
        <div className="ab-suggest-item" key={suggestion.suggestion_id}>
          <div className="ab-suggest-body">
            <span className="ab-suggest-q">{suggestion.question}</span>
            <span className="ab-suggest-reason">{suggestion.reason}（依据「{suggestion.quote.slice(0,30)}」）</span>
          </div>
          <button disabled={!allowed||!!adoptingSug} onClick={()=>void adoptSuggestion(suggestion)}>
            {adoptingSug===suggestion.suggestion_id?<LoaderCircle size={12} className="dd-spin"/>:<Check size={12}/>}采纳入问卷</button>
        </div>)}
    </div>}
    {(candidateView?.risk_signals||[]).length>0&&<div className="ab-suggest risk" aria-label="风险线索追问">
      <div className="ab-suggest-head"><Sparkles size={12}/>问卷外风险线索追问<span>{candidateView!.risk_signals!.length}</span></div>
      {candidateView!.risk_signals!.map(signal=>
        <div className="ab-suggest-item" key={signal.suggestion_id}>
          <div className="ab-suggest-body">
            <span className="ab-suggest-q"><i className="ab-risk-topic">{signal.topic}</i>{signal.question}</span>
            <span className="ab-suggest-reason">{signal.reason}（原话「{signal.quote.slice(0,30)}」）</span>
          </div>
          <button disabled={!allowed||!!adoptingSug} onClick={()=>void adoptSuggestion(signal)}>
            {adoptingSug===signal.suggestion_id?<LoaderCircle size={12} className="dd-spin"/>:<Check size={12}/>}采纳入问卷</button>
        </div>)}
    </div>}
    {loading&&!board?<div className="ab-empty"><LoaderCircle size={18} className="dd-spin"/> 正在读取本案问卷……</div>:
      !board?<div className="ab-empty">尚未取得问卷状态，请刷新重试。</div>:
      <div className="ab-groups">
        {Object.entries(grouped).map(([section,questions])=>
          <div key={section} className="ab-group">
            <div className="ab-group-head"><strong>{section}</strong><span>{questions.length}</span></div>
            <div className="ab-question-list">
              {questions.map(question=>{
                const answer=byAnswer[question.field_id];
                const candidates=candidatesByField[question.field_id]||[];
                const collected=candidates.length>0;
                return <div key={question.field_id} id={'q-'+question.field_id} className="ab-question">
                  {(question.source==='ai'||question.source==='rule')&&
                    <span className={'ab-badge '+(question.source==='ai'?'ai':'rule')}>{question.source==='ai'?'AI':'补问'}</span>}
                  <div className="ab-q-line">
                    <span className="ab-q-text">{question.question_text||question.label}</span>
                    <span className={'ab-q-kind'+(question.required_class==='required_candidate'?' req':'')}>
                      {question.required_class==='required_candidate'?'必问':'选问'}</span>
                    <span className="ab-q-field">{question.label}{question.unit?`（${question.unit}）`:''}</span>
                    <span className={'ab-q-answer'+(answer?' '+answer.status:' none')}>
                      {answer?(answer.status==='not_applicable'?'不适用':answerSummary(answer)):collected?'有候选待采纳':'未答'}
                    </span>
                  </div>
                  {board.previsit_reference?.[question.field_id]?.conflict&&
                    <div className="ab-conflict" role="alert">事前 {String(board.previsit_reference[question.field_id].previsit_value)} ≠ 现场 {String(board.previsit_reference[question.field_id].onsite_value)}，两边保留请核对。</div>}
                  {!answer&&collected&&candidates.slice(0,2).map(candidate=>
                    <div className="ab-candidate" role="note" key={candidate.candidate_id}>
                      <span>「{candidate.quote.slice(0,30)}」→ {String(candidate.value)}{candidate.unit?` ${candidate.unit}`:''}</span>
                      <div className="ab-candidate-actions">
                        <button disabled={!allowed||!!adopting} onClick={()=>void adopt(candidate,'confirmed')}><Check size={12}/>采纳确认</button>
                      </div>
                    </div>)}
                  <CompactAnswer question={question} answer={answer} allowed={allowed}
                    onSubmit={payload=>void submit(question.field_id,payload,answer)}/>
                </div>;})}
            </div>
          </div>)}
        {!board.questions.length&&<div className="ab-empty">本案问卷没有可作答条目。</div>}
      </div>}
  </section>;
}

const GAP_REASONS:Record<string,string>={no_answer:'未答',answer_not_applicable:'标记不适用',
  answer_not_confirmed:'草稿未确认',unit_mismatch_or_missing:'单位不符',
  value_shape_unusable:'值不可用',option_mismatch:'选项口径不符',presale_conflict:'与事前记载冲突'};
function gapReason(reason:string){return GAP_REASONS[reason]||reason;}

function answerSummary(answer:Answer){
  const value=answer.value||{};
  if(value.number!==undefined)return `${value.number}${value.unit?` ${value.unit}`:''}`;
  if(value.boolean!==undefined)return value.boolean?'是':'否';
  const text=value.text||value.choice||value.date||'—';
  return String(text).slice(0,24);
}

type CompactProps={question:Question;answer?:Answer;allowed:boolean;
  onSubmit(payload:Record<string,unknown>):void};

function CompactAnswer({question,answer,allowed,onSubmit}:CompactProps){
  const [draft,setDraft]=useState('');
  const [naMode,setNaMode]=useState(false),[naReason,setNaReason]=useState('');
  const [detailOpen,setDetailOpen]=useState(false);
  const [items,setItemDrafts]=useState<AnswerItem[]>(answer?.items?.filter(item=>item.status==='active')??[]);
  const value=answer?.value||{};
  const initial=answer?(value.number!==undefined?String(value.number)
    :value.boolean!==undefined?(value.boolean?'是':'否')
    :String(value.text||value.choice||value.date||'')):'';
  const numeric=question.value_type==='integer'||question.value_type==='decimal';
  const isBool=question.value_type==='boolean';

  function parseValue(raw:string){
    if(numeric)return {number:Number(raw)}; // 单位由题目标注口径决定，答案不再填写
    if(isBool)return {boolean:raw==='是'};
    if(/^\d{4}-\d{2}-\d{2}$/.test(raw))return {date:raw};
    return {text:raw};
  }
  const hasRaw=draft.trim()!=='';
  const structure=question.list_structure;

  if(answer?.status==='not_applicable'){
    return <div className="ab-q-input"><span className="ab-na">不适用：{answer.not_applicable_reason}</span>
      <button disabled={!allowed} onClick={()=>onSubmit({status:'draft',value:parseValue(initial||''),items:null,
        basis:[{type:'manual',id:'reopen'}]})}>重新作答</button></div>;
  }
  if(structure){
    const active=items.filter(item=>item.status==='active').length;
    return <div className="ab-q-input">
      <button className="ab-detail-toggle" onClick={()=>setDetailOpen(previous=>!previous)}>
        {detailOpen?'收起明细':'多笔明细'}（{active} 笔）</button>
      {detailOpen&&<div className="ab-items">
        {items.map((item,index)=><div key={item.item_id} className="ab-item">
          <div className="ab-item-members">
            {structure.members.map(member=><label key={member.key}>{member.label}
              <input value={String(item.members[member.key]??'')}
                onChange={event=>setItemDrafts(previous=>previous.map((row,rowIndex)=>rowIndex===index
                  ?{...row,members:{...row.members,[member.key]:member.value_type==='integer'||member.value_type==='decimal'
                    ?(event.target.value===''?'':Number(event.target.value)):event.target.value}}:row))}/>
            </label>)}
          </div>
          <button className="ab-item-remove" onClick={()=>setItemDrafts(previous=>previous.filter((_,rowIndex)=>rowIndex!==index))}><X size={12}/>移除</button>
        </div>)}
        <button className="ab-item-add" onClick={()=>setItemDrafts(previous=>[...previous,
          {item_id:'item-'+(previous.length+1)+'-'+Date.now().toString(36).slice(-4),status:'active',members:{}}])}><Plus size={13}/>新增一笔</button>
        <div className="ab-q-actions">
          <button className="ab-confirm" disabled={!allowed||!items.length}
            onClick={()=>onSubmit({status:'confirmed',items,value:null,
              basis:[{type:'manual',id:'note',note:'明细人工录入'}]})}>确认</button>
        </div>
      </div>}
    </div>;
  }
  if(naMode){
    return <div className="ab-q-input">
      <input value={naReason} onChange={event=>setNaReason(event.target.value)} placeholder="不适用原因（必填）"/>
      <div className="ab-q-actions">
        <button className="ab-confirm" disabled={!allowed||!naReason.trim()}
          onClick={()=>onSubmit({status:'not_applicable',not_applicable_reason:naReason.trim(),value:null,items:null,
            basis:[{type:'manual',id:'na'}]})}>确认不适用</button>
        <button onClick={()=>setNaMode(false)}>取消</button>
      </div>
    </div>;
  }
  return <div className="ab-q-input">
    <input value={draft===''?initial:draft} onChange={event=>setDraft(event.target.value)}
      placeholder={numeric?'填写数值':isBool?'填 是 / 否':'填写答案'}/>
    <div className="ab-q-actions">
      <button className="ab-confirm" disabled={!allowed||!hasRaw}
        onClick={()=>onSubmit({status:'confirmed',value:parseValue(draft.trim()),items:null,
          basis:[{type:'manual',id:'note',note:'现场人工填写'}]})}>确认</button>
      <button className="ab-na-btn" disabled={!allowed} onClick={()=>setNaMode(true)}>不适用</button>
    </div>
  </div>;
}

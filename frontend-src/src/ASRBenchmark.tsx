import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import type {FormEvent} from 'react';
import {ArrowLeft, AudioLines, CheckCircle2, ChevronRight, Clock3, Download, FileAudio,
  FlaskConical, FolderOpen, History, LoaderCircle, RefreshCw, Save, ShieldCheck, Square,
  Upload, AlertCircle, Play, Plus, Trash2} from 'lucide-react';
import './ASRBenchmark.css';

type Keyword = {id:string; label:string; expected:string; aliases:string[]};
type Sample = {id:string; name:string; duration_seconds:number; reference:string; keywords:Keyword[];
  tag:string; audio_sha256:string; version:number};
type Model = {id:string; label:string; engine:string; available:boolean; reason?:string; mode:string};
type KeywordDetail = {id:string; label:string; expected:string; hit:boolean;
  matched_form:string|null; matched_via:'expected'|'alias'|null};
type Result = {sample_id:string; model_id:string; status:string; transcript?:string; error?:string|null; metrics_error?:string|null;
  metrics?:{cer:number|null; character_recall:number|null; keyword_recall:number|null;
    keyword_hits:number; keyword_total:number; keyword_details:KeywordDetail[]}|null;
  elapsed_seconds:number|null; inference_seconds?:number|null; rtf:number|null; peak_rss_mb:number|null};
type Run = {id:string; status:string; created_at:string; progress:{done:number; total:number};
  samples:Sample[]; models:Model[]; results:Result[]};
type Draft = {name:string; tag:string; reference:string; keywords:Keyword[]};
const API = '/api/benchmark';
const activeStatuses = new Set(['queued','pending','running','cancelling','cancel_requested']);
const active = (run:Run) => activeStatuses.has(run.status);
const labels:Record<string,string> = {queued:'排队中',pending:'待执行',running:'测试中',cancelling:'正在取消',
  cancel_requested:'正在取消',completed:'已完成',succeeded:'已完成',success:'已完成',failed:'失败',
  cancelled:'已取消',canceled:'已取消',interrupted:'已中断',partial:'部分完成',completed_with_errors:'完成，含失败项'};
const statusLabel = (value:string) => labels[value] || value || '状态未知';
const duration = (value:number) => Number.isFinite(value) ? `${Math.floor(value/60)}:${String(Math.floor(value%60)).padStart(2,'0')}` : '时长未知';
const percent = (value:number|null|undefined) => value == null || !Number.isFinite(value) ? '未评估' : `${(value*100).toFixed(1)}%`;
const numeric = (value:number|null|undefined, digits=2) => value == null || !Number.isFinite(value) ? '—' : value.toFixed(digits);
const dateLabel = (value:string) => {const d=new Date(value); return Number.isNaN(d.getTime()) ? value : d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});};
const draftFor = (sample:Sample):Draft => ({name:sample.name,tag:sample.tag||'',reference:sample.reference||'',
  keywords:(sample.keywords||[]).map(keyword=>({...keyword,aliases:[...(keyword.aliases||[])]}))});
const message = (error:unknown) => error instanceof Error ? error.message : '操作未完成，请重试。';

async function responseError(response:Response):Promise<Error> {
  const body=await response.json().catch(()=>null);
  if(typeof body?.detail==='string') return new Error(body.detail);
  if(Array.isArray(body?.detail)) return new Error(body.detail.map((item:{msg?:string})=>item.msg||'输入格式有误').join('；'));
  return new Error(`本地服务请求失败（${response.status}），请检查测试服务后重试。`);
}
async function request<T>(path:string, options:RequestInit={}):Promise<T> {
  const response=await fetch(API+path,options);
  if(!response.ok) throw await responseError(response);
  if(response.status===204) return undefined as T;
  return response.json() as Promise<T>;
}
const jsonBody = (method:string,body:unknown):RequestInit => ({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});

function validateKeywords(keywords:Keyword[]):void {
  keywords.forEach((keyword,index)=>{
    if(!keyword.label.trim()||!keyword.expected.trim()) throw new Error(`关键项 ${index+1} 请填写标签与期望原词；不需要的项可直接移除。`);
    if(keyword.aliases.some(alias=>!alias.trim())) throw new Error(`关键项 ${index+1} 的别名含空行，请删除空行；没有别名时可全部留空。`);
  });
}

async function validateWav(file:File):Promise<number> {
  if(file.size>20*1024*1024) throw new Error('文件超过 20 MiB，请截取不超过 10 分钟的测试音频。');
  const buffer=await file.arrayBuffer();
  if(buffer.byteLength<44) throw new Error('文件不是完整的 WAV 音频。');
  const view=new DataView(buffer);
  const text=(offset:number,length:number)=>String.fromCharCode(...new Uint8Array(buffer,offset,length));
  if(text(0,4)!=='RIFF'||text(8,4)!=='WAVE') throw new Error('请选择 WAV 文件；仅改文件扩展名无法转换音频格式。');
  let offset=12,format:number|undefined,channels=0,sampleRate=0,bits=0,byteRate=0,dataSize=0;
  while(offset+8<=buffer.byteLength) {
    const chunk=text(offset,4),size=view.getUint32(offset+4,true),start=offset+8;
    if(start+size>buffer.byteLength) throw new Error('WAV 文件内容不完整，请重新导出。');
    if(chunk==='fmt '&&size>=16) {
      format=view.getUint16(start,true);channels=view.getUint16(start+2,true);sampleRate=view.getUint32(start+4,true);
      byteRate=view.getUint32(start+8,true);bits=view.getUint16(start+14,true);
    }
    if(chunk==='data') dataSize+=size;
    offset=start+size+(size%2);
  }
  if(format!==1||channels!==1||sampleRate!==16000||bits!==16||byteRate!==32000) throw new Error('音频格式需为 16 kHz、单声道、PCM 16 位 WAV，请先在本机转换。');
  if(!dataSize||dataSize%2!==0) throw new Error('WAV 中没有有效的 PCM 音频数据。');
  const seconds=dataSize/byteRate;
  if(seconds>600) throw new Error('音频超过 10 分钟，请截取后再导入。');
  return seconds;
}

function Feedback({text,kind='error'}:{text:string;kind?:'error'|'success'}) {
  if(!text) return null;
  return <div className={`ab-feedback ${kind}`} role={kind==='error'?'alert':'status'}>
    {kind==='error'?<AlertCircle size={16}/>:<CheckCircle2 size={16}/>}<span>{text}</span></div>;
}

export default function ASRBenchmark() {
  const [models,setModels]=useState<Model[]>([]),[samples,setSamples]=useState<Sample[]>([]),[runs,setRuns]=useState<Run[]>([]);
  const [localPolicy,setLocalPolicy]=useState<boolean|null>(null),[loading,setLoading]=useState(true),[loadErrors,setLoadErrors]=useState<string[]>([]);
  const [selectedSamples,setSelectedSamples]=useState<string[]>([]),[selectedModels,setSelectedModels]=useState<string[]>([]);
  const [focusedId,setFocusedId]=useState(''),[draft,setDraft]=useState<Draft|null>(null),[saveError,setSaveError]=useState(''),[saveNote,setSaveNote]=useState('');
  const [file,setFile]=useState<File|null>(null),[importName,setImportName]=useState(''),[importTag,setImportTag]=useState(''),[importError,setImportError]=useState('');
  const [operation,setOperation]=useState(''),[runError,setRunError]=useState(''),[pollError,setPollError]=useState('');
  const [selectedRunId,setSelectedRunId]=useState(''),[comparisonSampleId,setComparisonSampleId]=useState(''),[detailLoading,setDetailLoading]=useState(false);
  const mounted=useRef(false),initializedSamples=useRef(false),initializedModels=useRef(false),busy=useRef(false),detailRequest=useRef(0);
  const uploadInput=useRef<HTMLInputElement>(null);
  const focused=samples.find(sample=>sample.id===focusedId);
  const run=runs.find(item=>item.id===selectedRunId);
  const activeIds=runs.filter(active).map(item=>item.id).sort().join('|');
  const running=runs.some(active);

  const mergeRuns=useCallback((incoming:Run[])=>setRuns(previous=>{
    const map=new Map(previous.map(item=>[item.id,item]));incoming.forEach(item=>map.set(item.id,item));
    return [...map.values()].sort((a,b)=>b.created_at.localeCompare(a.created_at));
  }),[]);
  const load=useCallback(async(signal?:AbortSignal)=>{
    setLoading(true);setLoadErrors([]);
    const results=await Promise.allSettled([
      request<{models:Model[];policy:{local_only:boolean}}>('/models',{signal}),
      request<{samples:Sample[]}>('/samples',{signal}),request<{runs:Run[]}>('/runs',{signal})
    ]);
    if(!mounted.current||signal?.aborted)return;
    const errors:string[]=[];
    const [modelResult,sampleResult,runResult]=results;
    if(modelResult.status==='fulfilled') {
      setModels(modelResult.value.models);setLocalPolicy(modelResult.value.policy?.local_only===true);
      if(!initializedModels.current){setSelectedModels(modelResult.value.models.filter(item=>item.available).map(item=>item.id));initializedModels.current=true;}
    }else {setLocalPolicy(null);errors.push('模型列表：'+message(modelResult.reason));}
    if(sampleResult.status==='fulfilled') {
      setSamples(sampleResult.value.samples);
      if(!initializedSamples.current){const first=sampleResult.value.samples[0];if(first){setFocusedId(first.id);setSelectedSamples([first.id]);}initializedSamples.current=true;}
    }else errors.push('样本列表：'+message(sampleResult.reason));
    if(runResult.status==='fulfilled'){mergeRuns(runResult.value.runs);setSelectedRunId(old=>old||runResult.value.runs[0]?.id||'');}
    else errors.push('测试历史：'+message(runResult.reason));
    setLoadErrors(errors);setLoading(false);
  },[mergeRuns]);

  useEffect(()=>{mounted.current=true;const controller=new AbortController();void load(controller.signal);
    return()=>{mounted.current=false;controller.abort();detailRequest.current++;};},[load]);
  useEffect(()=>{if(focused){setDraft(draftFor(focused));setSaveError('');setSaveNote('');}else setDraft(null);},[focused?.id,focused?.version]);
  useEffect(()=>{
    if(!activeIds)return;
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      const results=await Promise.allSettled(activeIds.split('|').map(id=>request<Run>('/runs/'+encodeURIComponent(id),{signal:controller.signal})));
      if(controller.signal.aborted||!mounted.current)return;
      const found=results.filter((item):item is PromiseFulfilledResult<Run>=>item.status==='fulfilled').map(item=>item.value);
      mergeRuns(found);setPollError(results.some(item=>item.status==='rejected')?'暂时无法更新进度，正在重试；这不代表后台测试已停止。':'');
      timer=setTimeout(poll,1500);
    };
    timer=setTimeout(poll,1500);
    return()=>{controller.abort();clearTimeout(timer);};
  },[activeIds,mergeRuns]);
  useEffect(()=>{if(run&&!run.samples.some(sample=>sample.id===comparisonSampleId))setComparisonSampleId(run.samples[0]?.id||'');},[run?.id,run?.samples,comparisonSampleId]);

  const dirty=!!focused&&!!draft&&JSON.stringify(draftFor(focused))!==JSON.stringify(draft);
  const chosenModelIds=selectedModels.filter(id=>models.some(model=>model.id===id&&model.available));
  const chosenSampleIds=selectedSamples.filter(id=>samples.some(sample=>sample.id===id));
  const comparison=run?.samples.find(sample=>sample.id===comparisonSampleId);
  const comparisonModels=run?.models||[];
  const shownResults=useMemo(()=>new Map((run?.results||[]).filter(result=>result.sample_id===comparisonSampleId).map(result=>[result.model_id,result])),[run?.results,comparisonSampleId]);
  const changeDraft=(patch:Partial<Draft>)=>{setDraft(old=>old?{...old,...patch}:old);setSaveError('');setSaveNote('');};
  const changeKeywords=(update:(keywords:Keyword[])=>Keyword[])=>{setDraft(old=>old?{...old,keywords:update(old.keywords)}:old);setSaveError('');setSaveNote('');};
  const changeKeyword=(id:string,patch:Partial<Pick<Keyword,'label'|'expected'|'aliases'>>)=>changeKeywords(items=>items.map(item=>item.id===id?{...item,...patch}:item));
  const addKeyword=()=>{const item:Keyword={id:`keyword_${crypto.randomUUID()}`,label:'',expected:'',aliases:[]};changeKeywords(items=>[...items,item]);};
  const toggle=(id:string,list:string[],set:(value:string[])=>void)=>set(list.includes(id)?list.filter(item=>item!==id):[...list,id]);
  async function perform(key:string,action:()=>Promise<void>,onError:(value:string)=>void) {
    if(busy.current)return;busy.current=true;setOperation(key);onError('');
    try{await action();}catch(error){if(mounted.current)onError(message(error));}
    finally{busy.current=false;if(mounted.current)setOperation('');}
  }
  function addSample(sample:Sample) {
    setSamples(old=>[sample,...old.filter(item=>item.id!==sample.id)]);setFocusedId(sample.id);
    setSelectedSamples(old=>[...new Set([...old,sample.id])]);
  }
  async function importSample(event:FormEvent) {
    event.preventDefault();if(!file){setImportError('请先选择本地 WAV 文件。');return;}
    if(dirty){setImportError('请先保存当前样本的修改，再导入新的样本。');return;}
    const chosenFile=file;
    await perform('import',async()=>{
      await validateWav(chosenFile);
      const body=new FormData();body.append('file',chosenFile);body.append('name',importName.trim()||chosenFile.name.replace(/\.wav$/i,''));body.append('tag',importTag.trim());
      const value=await request<Sample>('/samples',{method:'POST',body});
      if(!mounted.current)return;addSample(value);setFile(null);setImportName('');setImportTag('');if(uploadInput.current)uploadInput.current.value='';
    },setImportError);
  }
  async function saveSample(event:FormEvent) {
    event.preventDefault();if(!focused||!draft)return;
    await perform('save',async()=>{
      if(!draft.name.trim())throw new Error('请填写样本名称。');
      validateKeywords(draft.keywords);
      const value=await request<Sample>('/samples/'+encodeURIComponent(focused.id),jsonBody('PUT',{name:draft.name.trim(),tag:draft.tag.trim(),reference:draft.reference,keywords:draft.keywords}));
      if(!mounted.current)return;setSamples(old=>old.map(item=>item.id===value.id?value:item));setDraft(draftFor(value));setSaveNote('已保存。已有测试仍使用开始时的参考稿和关键项快照。');
    },setSaveError);
  }
  async function startRun() {
    if(!chosenSampleIds.length||!chosenModelIds.length)return;
    await perform('start',async()=>{
      if(localPolicy!==true)throw new Error('本地测试策略尚未确认，请重新加载模型列表。');
      if(dirty&&chosenSampleIds.includes(focusedId))throw new Error('当前样本有未保存修改，请保存后再开始测试。');
      const created=await request<Pick<Run,'id'|'status'|'results'>>('/runs',jsonBody('POST',{sample_ids:chosenSampleIds,model_ids:chosenModelIds}));
      if(!mounted.current)return;
      const snapshot:Run={...created,created_at:new Date().toISOString(),progress:{done:0,total:chosenSampleIds.length*chosenModelIds.length},
        samples:chosenSampleIds.map(id=>samples.find(sample=>sample.id===id)!),models:chosenModelIds.map(id=>models.find(model=>model.id===id)!)};
      mergeRuns([snapshot]);setSelectedRunId(created.id);setComparisonSampleId(chosenSampleIds[0]);setPollError('');
      try {const value=await request<Run>('/runs/'+encodeURIComponent(created.id));if(mounted.current)mergeRuns([value]);}
      catch {if(mounted.current)setPollError('测试已提交，暂时未能读取完整进度；正在自动重试。');}
    },setRunError);
  }
  async function chooseRun(id:string) {
    setSelectedRunId(id);setRunError('');setDetailLoading(true);const ticket=++detailRequest.current;
    try{const value=await request<Run>('/runs/'+encodeURIComponent(id));if(mounted.current&&ticket===detailRequest.current)mergeRuns([value]);}
    catch(error){if(mounted.current&&ticket===detailRequest.current)setRunError(message(error));}
    finally{if(mounted.current&&ticket===detailRequest.current)setDetailLoading(false);}
  }
  async function exportRun(format:'csv'|'json') {
    if(!run)return;
    await perform('export-'+format,async()=>{
      const response=await fetch(`${API}/runs/${encodeURIComponent(run.id)}/export?format=${format}`);
      if(!response.ok)throw await responseError(response);
      const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');
      link.href=url;link.download=`asr-benchmark-${run.id.replace(/[^a-zA-Z0-9_-]/g,'')}.${format}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    },setRunError);
  }

  return <div className="asr-benchmark">
    <header className="ab-header"><div className="ab-brand"><span className="ab-brand-mark"><AudioLines size={24}/></span><div><strong>语音模型测试台</strong><small>同一段录音，核对不同模型的真实表现</small></div></div>
      <div className="ab-header-actions"><span className={'ab-local '+(localPolicy===true?'confirmed':'')}><ShieldCheck size={14}/>{localPolicy===true?'本地测试模式':localPolicy===false?'本地策略不符':'正在确认本地服务'}</span><a href="http://127.0.0.1:8767" target="_blank" rel="noreferrer" className="ab-link"><ArrowLeft size={14}/> 会议应用（需先启动）</a></div></header>
    <main className="ab-page">
      <div className="ab-page-heading"><div><span className="ab-eyebrow">语音能力 · 可复现对比</span><h1>先听同一段，再看差异。</h1><p>保存人工参考稿与关键项，让每次比较都有相同依据。测试不会生成模型排名。</p></div><button disabled={loading||!!operation} onClick={()=>void load()}><RefreshCw size={15} className={loading?'ab-spin':''}/>刷新列表</button></div>
      {!!loadErrors.length&&<div className="ab-banner" role="alert"><AlertCircle size={17}/><div><strong>部分数据未加载</strong>{loadErrors.map(error=><p key={error}>{error}</p>)}<small>请确认独立测试服务已启动，再刷新列表。</small></div></div>}
      <div className="ab-layout">
        <aside className="ab-library" aria-label="测试样本库">
          <section className="ab-panel"><div className="ab-section-title"><h2><FolderOpen size={17}/> 样本库</h2><span>{samples.length} 份</span></div><p className="ab-help">勾选参与测试的样本，点击名称编辑依据。</p>
            <div className="ab-sample-list">{samples.map(sample=><div className={'ab-sample '+(focusedId===sample.id?'focused':'')} key={sample.id}>
              <input type="checkbox" aria-label={'选择样本：'+sample.name} checked={selectedSamples.includes(sample.id)} onChange={()=>toggle(sample.id,selectedSamples,setSelectedSamples)} disabled={!!operation}/>
              <button className="ab-sample-select" onClick={()=>{if(dirty&&focusedId!==sample.id){setSaveError('请先保存当前样本的修改，再切换样本。');return;}setFocusedId(sample.id);}}><strong>{sample.name}</strong><span>{duration(sample.duration_seconds)} · v{sample.version}{sample.tag&&<> · {sample.tag}</>}</span><small>{sample.reference?.trim()?'有人工参考稿':'无人工参考稿'} · {sample.keywords?.length||0} 个关键项</small></button><ChevronRight size={14}/></div>)}</div>
            {!samples.length&&<div className="ab-empty small"><FileAudio size={30}/><strong>{loading?'正在读取样本':'先准备一份测试录音'}</strong><p>导入本地 WAV，或加载公开样音。</p></div>}
          </section>
          <section className="ab-panel"><div className="ab-section-title"><h2><Upload size={17}/> 导入录音</h2></div><form onSubmit={importSample}>
            <label className="ab-file-picker"><FileAudio size={22}/><span>{file?file.name:'选择本地 WAV 文件'}<small>16 kHz · 单声道 · PCM 16 位<br/>最多 20 MiB、10 分钟</small></span><input ref={uploadInput} type="file" accept=".wav,audio/wav,audio/x-wav" aria-label="选择本地测试WAV" disabled={!!operation} onChange={event=>{const value=event.target.files?.[0]||null;setFile(value);setImportError('');if(value)setImportName(value.name.replace(/\.wav$/i,''));}}/></label>
            <label>样本名称<input maxLength={120} value={importName} disabled={!!operation} placeholder="例如：三人农户访谈·普通话" onChange={event=>setImportName(event.target.value)}/></label>
            <label>标签<input maxLength={100} value={importTag} disabled={!!operation} placeholder="例如：普通话 / 噪声 / 方言" onChange={event=>setImportTag(event.target.value)}/></label>
            <Feedback text={importError}/><button className="ab-wide" type="submit" disabled={!file||!!operation}>{operation==='import'?<LoaderCircle className="ab-spin" size={15}/>:<Upload size={15}/>} {operation==='import'?'校验并导入中':'导入到样本库'}</button>
          </form><div className="ab-divider"/><button className="ab-wide ab-secondary" disabled={!!operation} onClick={()=>void perform('demo',async()=>{if(dirty)throw new Error('请先保存当前样本的修改，再加载公开样音。');const sample=await request<Sample>('/demo',{method:'POST'});if(mounted.current)addSample(sample);},setImportError)}>{operation==='demo'?<LoaderCircle className="ab-spin" size={15}/>:<Play size={15}/>} {operation==='demo'?'正在加载':'加载公开样音'}</button><p className="ab-help">公开样音初始不含人工参考稿；未补充依据时只展示原始转写与性能，不显示准确率。</p></section>
        </aside>
        <div className="ab-content">
          <section className="ab-panel ab-editor" aria-label="参考稿与关键项">
            <div className="ab-section-title"><h2><span className="ab-step">01</span> 准备比较依据</h2>{focused&&<span>样本 v{focused.version}</span>}</div>
            {focused&&draft?<form onSubmit={saveSample}><div className="ab-editor-meta"><label>样本名称<input value={draft.name} maxLength={120} disabled={!!operation} onChange={event=>changeDraft({name:event.target.value})}/></label><label>标签<input value={draft.tag} maxLength={100} disabled={!!operation} onChange={event=>changeDraft({tag:event.target.value})}/></label></div>
              <div className="ab-audio"><FileAudio size={17}/><audio key={focused.id} controls preload="metadata" src={`${API}/samples/${encodeURIComponent(focused.id)}/audio`}/><span>{duration(focused.duration_seconds)}</span></div>
              <details className="ab-file-details"><summary>音频与版本信息</summary><p>SHA-256：<code>{focused.audio_sha256}</code></p><p>保存参考稿会创建新的样本版本；已运行结果保留其原始快照。</p></details>
              <div className="ab-editor-grid"><label>人工参考稿 <span className="ab-optional">可留空，最多 8000 字符</span><textarea rows={7} maxLength={8000} disabled={!!operation} value={draft.reference} placeholder="听原始录音后填写完整参考稿。留空时 CER 和字符召回显示“未评估”。" onChange={event=>changeDraft({reference:event.target.value})}/></label>
                <section className="ab-keyword-editor" aria-label="关键项编辑器"><div className="ab-keyword-editor-heading"><strong>关键项 <span className="ab-optional">可不设置</span></strong><button type="button" disabled={!!operation} onClick={addKeyword}><Plus size={14}/> 添加一项</button></div>
                  <p className="ab-help">标签、原词分别填写；别名每行一个，斜杠和竖线会作为词项原文保留。</p>
                  <div className="ab-keyword-edit-list">{draft.keywords.map((keyword,index)=><div className="ab-keyword-edit-item" key={keyword.id}>
                    <div className="ab-keyword-item-heading"><span>关键项 {index+1}</span><button type="button" aria-label={`移除关键项 ${index+1}`} title={`移除关键项 ${index+1}`} disabled={!!operation} onClick={()=>changeKeywords(items=>items.filter(item=>item.id!==keyword.id))}><Trash2 size={14}/> 移除</button></div>
                    <div className="ab-keyword-edit-fields"><label>标签<input aria-label={`关键项 ${index+1} 标签`} disabled={!!operation} value={keyword.label} placeholder="例如：亩产" onChange={event=>changeKeyword(keyword.id,{label:event.target.value})}/></label><label>期望原词<input aria-label={`关键项 ${index+1} 期望原词`} disabled={!!operation} value={keyword.expected} placeholder="例如：4000斤/亩" onChange={event=>changeKeyword(keyword.id,{expected:event.target.value})}/></label></div>
                    <label>别名 <span className="ab-optional">每行一个，可留空</span><textarea className="ab-keyword-aliases" rows={2} aria-label={`关键项 ${index+1} 别名，每行一个`} disabled={!!operation} value={keyword.aliases.join('\n')} spellCheck={false} placeholder={'四千斤/亩\n每亩四千斤'} onChange={event=>changeKeyword(keyword.id,{aliases:event.target.value===''?[]:event.target.value.split(/\r?\n/)})}/></label>
                  </div>)}</div>{!draft.keywords.length&&<p className="ab-keyword-editor-empty">尚未设置关键项。可先只比较人工参考稿，也可添加需要核对的完整词项。</p>}
                </section></div>
              <div className="ab-editor-footer"><p className={dirty?'ab-unsaved':'ab-help'}>{dirty?'有未保存的修改，测试前请保存。':'参考稿与关键项由人工维护；模型输出不自动改写参考稿。'}</p><button type="submit" disabled={!dirty||!!operation}>{operation==='save'?<LoaderCircle className="ab-spin" size={15}/>:<Save size={15}/>} {operation==='save'?'正在保存':'保存依据'}</button></div><Feedback text={saveError}/><Feedback text={saveNote} kind="success"/>
            </form>:<div className="ab-empty"><FileAudio size={32}/><strong>从左侧选择一份样本</strong><p>回听音频，再补充人工参考稿和需要重点核对的词项。</p></div>}
          </section>
          <section className="ab-panel" aria-label="模型选择与开始测试"><div className="ab-section-title"><h2><span className="ab-step">02</span> 选择本地模型</h2><span>{models.filter(model=>model.available).length} 个可用</span></div>
            <div className="ab-model-grid">{models.map(model=><label className={'ab-model '+(!model.available?'unavailable':'')+(selectedModels.includes(model.id)?' selected':'')} key={model.id}>
              <input type="checkbox" checked={selectedModels.includes(model.id)&&model.available} disabled={!model.available||!!operation} onChange={()=>toggle(model.id,selectedModels,setSelectedModels)}/><span><strong>{model.label}</strong><small>{model.mode==='streaming_replay'?'流式引擎 · 文件分块回放':model.mode==='offline'?'离线文件识别':model.mode} · {model.engine}</small><span className={'ab-availability '+(model.available?'ready':'')}>{model.available?'已就绪':model.reason||'当前不可用'}</span></span></label>)}</div>
            {!models.length&&<p className="ab-help">{loading?'正在读取模型状态……':'未取得可用模型列表，请刷新或检查本地服务。'}</p>}
            <div className="ab-run-toolbar"><div><strong>{chosenSampleIds.length} 份样本 × {chosenModelIds.length} 个模型</strong><p>每组使用同一份音频，顺序执行，减少同时抢占计算资源。</p></div><button className="ab-primary" disabled={!!operation||running||!chosenSampleIds.length||!chosenModelIds.length||localPolicy!==true||(dirty&&chosenSampleIds.includes(focusedId))} onClick={()=>void startRun()}>{operation==='start'?<LoaderCircle className="ab-spin" size={16}/>:<FlaskConical size={16}/>} {operation==='start'?'正在提交':running?'已有测试执行中':'开始对比测试'}</button></div>
          </section>
          <section className="ab-panel ab-results" aria-label="测试结果与历史"><div className="ab-section-title"><h2><span className="ab-step">03</span> 核对测试结果</h2><div className="ab-export"><button disabled={!run||!!operation} onClick={()=>void exportRun('csv')}><Download size={14}/> CSV</button><button disabled={!run||!!operation} onClick={()=>void exportRun('json')}><Download size={14}/> JSON</button></div></div>
            <div className="ab-history"><History size={16}/><label>测试历史<select aria-label="选择测试历史" value={selectedRunId} onChange={event=>void chooseRun(event.target.value)} disabled={!runs.length}>{!runs.length&&<option value="">尚无测试</option>}{runs.map(item=><option value={item.id} key={item.id}>{dateLabel(item.created_at)} · {statusLabel(item.status)} · {item.progress?.done||0}/{item.progress?.total||0}</option>)}</select></label>{detailLoading&&<LoaderCircle size={15} className="ab-spin"/>}</div>
            <Feedback text={runError}/><Feedback text={pollError}/>
            {run?<><div className="ab-progress-heading"><div><span className={'ab-status '+(active(run)?'running':run.status)}>{active(run)&&<LoaderCircle size={13} className="ab-spin"/>}{statusLabel(run.status)}</span><small>{run.progress?.done||0} / {run.progress?.total||0} 组已处理</small></div>{active(run)&&<button className="ab-cancel" disabled={!!operation||run.status==='cancelling'||run.status==='cancel_requested'} onClick={()=>void perform('cancel',async()=>{await request('/runs/'+encodeURIComponent(run.id)+'/cancel',{method:'POST'});const value=await request<Run>('/runs/'+encodeURIComponent(run.id));if(mounted.current)mergeRuns([value]);},setRunError)}><Square size={13}/>{operation==='cancel'?'正在请求取消':'取消本次测试'}</button>}</div>
              <progress className="ab-progress" aria-label="本次测试进度" value={run.progress?.done||0} max={Math.max(1,run.progress?.total||0)}/>
              <div className="ab-comparison-filter"><label>查看样本<select value={comparisonSampleId} onChange={event=>setComparisonSampleId(event.target.value)}>{run.samples.map(sample=><option key={sample.id} value={sample.id}>{sample.name} · v{sample.version}</option>)}</select></label><small>使用开始测试时的音频与标注快照，不随当前编辑变化。</small></div>
              <div className="ab-table-scroll"><table><caption>同音频模型对比，指标不生成综合排名</caption><thead><tr><th>模型 / 状态</th><th>CER ↓</th><th>字符召回</th><th>关键项覆盖</th><th>总耗时（含冷启动）</th><th>识别推理耗时</th><th>RTF</th><th>峰值 RSS</th></tr></thead><tbody>{comparisonModels.map(model=>{const result=shownResults.get(model.id);return <tr key={model.id}><th scope="row"><strong>{model.label}</strong><span>{statusLabel(result?.status||'pending')}</span></th><td>{percent(result?.metrics?.cer)}</td><td>{percent(result?.metrics?.character_recall)}</td><td>{percent(result?.metrics?.keyword_recall)}{(result?.metrics?.keyword_total||0)>0&&<small>{result?.metrics?.keyword_hits} / {result?.metrics?.keyword_total} 项</small>}</td><td>{numeric(result?.elapsed_seconds)}{result?.elapsed_seconds!=null?' 秒':''}</td><td>{numeric(result?.inference_seconds)}{result?.inference_seconds!=null?' 秒':''}</td><td>{numeric(result?.rtf,3)}</td><td>{numeric(result?.peak_rss_mb,1)}{result?.peak_rss_mb!=null?' MiB':''}</td></tr>;})}</tbody></table></div>
              {comparison&&<><div className="ab-reference-snapshot"><div><FileAudio size={16}/><strong>{comparison.name}</strong><span>本次参考稿 · v{comparison.version}</span></div><audio controls preload="none" src={`${API}/samples/${encodeURIComponent(comparison.id)}/audio`}/><p>{comparison.reference?.trim()||'没有人工参考稿，本次 CER 和字符召回未评估。可在样本中补充后重新测试，历史结果保持原样。'}</p></div>
                <div className="ab-transcript-grid">{comparisonModels.map(model=>{const result=shownResults.get(model.id),details=result?.metrics?.keyword_details||[],misses=details.filter(item=>!item.hit);return <article className="ab-transcript-card" key={model.id}><header><h3>{model.label}</h3><span>{statusLabel(result?.status||'pending')}</span></header>{result?.error&&<Feedback text={result.error}/>}<h4>模型转写</h4><p className="ab-transcript-text">{result?.transcript||(['succeeded','completed','success'].includes(result?.status||'')?'识别完成，输出为空。':result?.status==='failed'?'本项识别失败，未生成可比较转写。':'尚无转写结果。')}</p><>{result?.metrics_error&&<div className="ab-metrics-note" role="status"><AlertCircle size={15}/><span>部分评分未完成：{result.metrics_error}。识别结果仍保留，可继续核对转写和关键项。</span></div>}</><h4>关键项核对 {details.length>0&&<small>{misses.length} 项未命中</small>}</h4>{details.length?<ul className="ab-keyword-list">{details.map(item=><li className={item.hit?'hit':'miss'} key={item.id}><span>{item.hit?<CheckCircle2 size={14}/>:<AlertCircle size={14}/>}<strong>{item.label}</strong><em>{item.hit?'已命中':'未命中'}</em></span><small>期望：{item.expected}{item.hit&&item.matched_form&&<> · 命中{item.matched_via==='alias'?'别名':'原词'}：{item.matched_form}</>}</small></li>)}</ul>:<p className="ab-help">{comparison.keywords?.length?'尚无关键项评估结果。':'未配置关键项，本次词项覆盖未评估。'}</p>}</article>;})}</div></>}
            </>:<div className="ab-empty"><FlaskConical size={34}/><strong>还没有对比结果</strong><p>选择样本和模型后开始。完成结果、失败信息和使用的依据都会保留。</p></div>}
          </section>
          <footer className="ab-methods"><h3><Clock3 size={16}/> 这些数字说明什么</h3><p><strong>CER</strong> 是字符错误率，越低越好，可能超过 100%；没有人工参考稿时不评估。对齐评分超出预算时会单独提示，识别结果和关键项评测仍保留。</p><p><strong>关键项覆盖</strong> 是确定性词项匹配，含手工设置的别名；不能证明金额、单位、期间或说话人归属正确，也不能替代事实核验。</p><p><strong>总耗时</strong> 和 <strong>RTF</strong> 包含冷启动；RTF 为总耗时 ÷ 音频时长，首次加载模型与本机权重指纹计算会影响结果。识别推理耗时另列。这里测的是文件批处理或分块回放，不是现场转写延迟。峰值 RSS 为工作器自身进程由操作系统记录的峰值内存，不代表全系统占用；未取得时显示“—”。</p></footer>
        </div>
      </div>
    </main>
  </div>;
}

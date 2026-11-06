import {useCallback,useEffect,useRef,useState} from 'react';
import {AlertCircle,Check,Download,FileText,History,LoaderCircle,RefreshCw,Save,SquarePen} from 'lucide-react';
import './ReportsWorkspace.css';
import CreditHero from './CreditHero';
import ReportCoverView from './ReportCoverView';

type Cover={title:string;subtitle:string;organization:string;author:string;date:string;imageMode:'scene'|'none';focusX:number;focusY:number;fit:'cover'|'contain'};
type Summary={id:string;revision:number;kind:'generated'|'manual';parent_id:string|null;created_at:string;branch:string;stale:boolean;stale_dependencies:string[];status:string};
type Row={binding_id:string|null;field_id:string|null;label:string;value:string|null;unit:string|null;period:{label:string}|null;subject_id:string|null;status:string;origin:string;reference_ids:string[];gap:boolean};
type Section={id:string;title:string;text?:string;tables:{id:string;title:string;rows:Row[]}[];manual_notes:{text:string;basis:string|null;origin:string}[]};
type Reference={id:string;field_id:string;fact_id:string;fact_revision:number;statement_id:string|null;statement_revision:number|null;quote:string|null;quote_status:string;source_ref:unknown;record_confirmation:string;verification:string;needs_review:boolean;conflict:boolean};
type CalculationPeriod={kind?:string;label?:string;start?:string|null;end?:string|null};
type CalculationRecord={field_id?:string;label?:string;value:string|null;presence:string;unit:string|null;period:CalculationPeriod|null;subject_id:string|null;record_confirmation:string;verification:{state:string;reviewer?:string;basis?:string};source_ref:{kind?:string;text?:string;actor?:string;fact_id?:string;statement_id?:string;revision?:number}|null};
type CalculationAppendix={title?:string;notice?:string;scope:{evaluation_as_of:string;accounting_period:CalculationPeriod;household_id:string;scope_subject_ids:string[];scope_basis:string};fields:CalculationRecord[];assets:{record_id:string;type:string;share:string;amount:CalculationRecord}[];debts:{record_id:string;type:string;share:string;amount:CalculationRecord}[]};
type Report=Summary&{case_title:string;cover:Cover;template_version:string;sections:Section[];references:Reference[];credit:{state:string;effective_limit:string|null;reason?:string;score?:string|null;rating?:string|null};content_hash:string;image_note:string;calculation_appendix?:CalculationAppendix|null};
type Listing={case_id:string;reports:Summary[];current_fingerprint:string;template_version:string;binding_coverage:Record<string,number>;binding_count:number;table_count:number;formal_business_acceptance:string};
const API='/api/due-diligence/cases/';
const message=(value:unknown)=>value instanceof Error?value.message:'操作暂未完成，请重试。';
async function request<T>(path:string,options:RequestInit={}):Promise<T>{
  const response=await fetch(path,options);
  if(!response.ok){const value=await response.json().catch(()=>null);throw new Error(value?.detail?.message||`本地报告服务未完成请求（${response.status}）。`);}
  return response.json() as Promise<T>;
}
const json=(value:unknown):RequestInit=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
const dateLabel=(value:string)=>new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
const dependencyLabels:Record<string,string>={facts:'事实',statements:'原话',previsit:'事前快照',questionnaire:'问卷',template:'模板',model:'模型',calculation:'计算',case:'案件信息',cover_asset:'封面配图'};
const calculationPresence:Record<string,string>={known:'已说明',explicit_zero:'明确为零',unknown:'未知',missing:'未提供',not_applicable:'明确不适用',withheld:'明确不愿提供'};
const calculationCategories:Record<string,string>={fruit:'水果',grain:'主粮',herb:'中草药',other:'其他（已明确）',homestead:'宅基地及其房产',residence:'商品住宅',vehicle:'车辆',machinery:'农业机械设备',financial:'金融资产',other_fixed:'其他固定资产',other_current:'其他流动资产',bank:'银行借款',nonbank:'非银行负债'};
const decimalLabel=(value:string|null|undefined)=>{if(value===null||value===undefined||value==='')return '未明确';return /^-?\d+\.\d+$/.test(value)?value.replace(/(\.\d*?)0+$/,'$1').replace(/\.$/,''):value;};
function periodLabel(period:CalculationPeriod|null|undefined){if(!period)return '期间未明确';const label=period.label||({year:'完整会计年度',as_of:'截至时点',duration:'持续时长'} as Record<string,string>)[period.kind||'']||'期间';if(period.start&&period.end)return label+(period.start===period.end?`（${period.start}）`:`（${period.start}—${period.end}）`);return label;}
function calculationValue(record:CalculationRecord){if(!['known','explicit_zero'].includes(record.presence))return calculationPresence[record.presence]||'状态待明确';if(record.value===null||record.value==='')return '记载值未明确';return (calculationCategories[record.value]||decimalLabel(record.value))+(record.unit?' '+record.unit:'');}
function CalculationVerification({record}:{record:CalculationRecord}){return <><span>{record.record_confirmation==='record_accurate'?'记录准确已确认':'记录准确待确认'}</span><small>{record.verification?.state==='checked_consistent'?'本次测算输入已核验一致':'本次测算输入尚未核验一致'}</small>{record.verification?.reviewer&&<small>核验人：{record.verification.reviewer}</small>}{record.verification?.basis&&<p className="rw-calculation-basis">核验依据：{record.verification.basis}</p>}</>;}
function CalculationSource({record}:{record:CalculationRecord}){const source=record.source_ref;if(!source)return <span className="rw-gap">尚无可用依据</span>;if(source.kind==='manual')return <>{source.actor&&<small>记录人：{source.actor}</small>}<p className="rw-calculation-basis">{source.text||'人工依据原文未明确'}</p></>;if(source.kind==='fact')return <><span>本案事实第{source.revision??'—'}版</span><small>{source.fact_id||'来源编号未明确'}</small></>;if(source.kind==='statement')return <><span>本案原话第{source.revision??'—'}版</span><small>{source.statement_id||'来源编号未明确'}</small></>;return <span>来源定位保留于证据索引</span>;}
function CalculationSnapshot({appendix}:{appendix:CalculationAppendix}){
 const scope=appendix.scope;
 return <details className="rw-calculation-appendix"><summary><strong>本次测算输入快照</strong><span>{appendix.fields.length}项输入 · {appendix.assets.length}项资产 · {appendix.debts.length}项负债</span></summary><div className="rw-calculation-body"><p className="rw-calculation-notice">{appendix.notice?.trim()||'测算输入与事前资料记载分别保留，口径不同不能直接互换。'}</p>
  <dl className="rw-calculation-scope"><div><dt>本次资料基准日</dt><dd>{scope.evaluation_as_of||'未明确'}</dd></div><div><dt>本次完整会计期间</dt><dd>{periodLabel(scope.accounting_period)}</dd></div><div><dt>纳入家庭</dt><dd>{scope.household_id||'未明确'}</dd></div><div><dt>明确纳入主体</dt><dd>{scope.scope_subject_ids.length?scope.scope_subject_ids.join('、'):'未明确'}</dd></div><div className="rw-calculation-scope-basis"><dt>范围与归属依据</dt><dd>{scope.scope_basis||'尚未明确'}</dd></div></dl>
  <details className="rw-table-block rw-calculation-table"><summary><strong>本次采用的测算输入</strong><span>{appendix.fields.length}项原始记载</span></summary><div className="rw-table-scroll"><table><thead><tr><th>输入与记载</th><th>期间与主体</th><th>记录及核验依据</th><th>原始来源</th></tr></thead><tbody>{appendix.fields.map((record,index)=><tr key={(record.field_id||'input')+'-'+index}><td><strong>{record.label||'测算输入'}</strong><span className="rw-calculation-value">{calculationValue(record)}</span><small>{calculationPresence[record.presence]||'状态待明确'}</small></td><td>{periodLabel(record.period)}<small>主体：{record.subject_id||'尚未明确'}</small></td><td><CalculationVerification record={record}/></td><td><CalculationSource record={record}/></td></tr>)}</tbody></table></div></details>
  {(['assets','debts'] as const).map(kind=><details className="rw-table-block rw-calculation-table" key={kind}><summary><strong>{kind==='assets'?'本次资产现值清单':'本次负债当前余额清单'}</strong><span>{appendix[kind].length}项</span></summary>{appendix[kind].length?<div className="rw-table-scroll"><table><thead><tr><th>{kind==='assets'?'资产现值及份额':'负债余额及份额'}</th><th>期间与主体</th><th>记录及核验依据</th><th>原始来源</th></tr></thead><tbody>{appendix[kind].map((row,index)=><tr key={row.record_id+'-'+index}><td><strong>{calculationCategories[row.type]||(kind==='assets'?'资产':'负债')}</strong><span className="rw-calculation-value">{calculationValue(row.amount)}</span><small>明确归属份额：{decimalLabel(row.share)}</small><small>清单编号：{row.record_id}</small></td><td>{periodLabel(row.amount.period)}<small>主体：{row.amount.subject_id||'尚未明确'}</small></td><td><CalculationVerification record={row.amount}/></td><td><CalculationSource record={row.amount}/></td></tr>)}</tbody></table></div>:<p className="rw-calculation-help">本次快照清单无条目；无条目含义以本次范围、完整性和核验依据为准。</p>}</details>)}
 </div></details>;
}

export default function ReportsWorkspace({caseId,caseTitle}:{caseId:string;caseTitle:string}){
  const base=API+encodeURIComponent(caseId);
  const [listing,setListing]=useState<Listing|null>(null),[selected,setSelected]=useState(''),[report,setReport]=useState<Report|null>(null);
  const [busy,setBusy]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const [editSection,setEditSection]=useState(''),[editText,setEditText]=useState(''),[editBasis,setEditBasis]=useState('');
  const [source,setSource]=useState<Reference|null>(null),[coverOpen,setCoverOpen]=useState(true);
  const epoch=useRef(0),selectedRef=useRef(selected);selectedRef.current=selected;
  const refresh=useCallback(async()=>{
    const ticket=epoch.current;
    try{
      const value=await request<Listing>(base+'/reports');
      if(ticket!==epoch.current)return;
      setListing(value);
      const id=value.reports.some(r=>r.id===selectedRef.current)?selectedRef.current:value.reports[0]?.id||'';
      if(id){const next=await request<Report>(base+'/reports/'+encodeURIComponent(id));if(ticket===epoch.current){setSelected(id);setReport(next);}}
      else{setSelected('');setReport(null);}
    }catch(reason){if(ticket===epoch.current){setListing(null);setReport(null);setSource(null);setEditText('');setEditSection('');throw reason;}}
  },[base]);
  useEffect(()=>{epoch.current++;setListing(null);setReport(null);setSelected('');setSource(null);setEditText('');setEditSection('');setError('');setNotice('');setBusy('load');
    void refresh().catch(reason=>setError(message(reason))).finally(()=>setBusy(''));
    return()=>{epoch.current++;};
  },[refresh]);
  async function run(name:string,action:()=>Promise<void>){if(busy)return;const ticket=epoch.current;setBusy(name);setError('');setNotice('');try{await action();}catch(reason){if(ticket===epoch.current)setError(message(reason));}finally{if(ticket===epoch.current)setBusy('');}}
  function browserCover():Cover|undefined{
    try{
      const value=JSON.parse(localStorage.getItem('due-diligence:report-cover:v1:'+encodeURIComponent(caseId))||'null');
      if(value?.schema===1&&value.settings)return value.settings as Cover;
    }catch{throw new Error('本浏览器的封面设置无法读取，请在下方恢复并保存样式后再生成。');}
    return undefined;
  }
  async function generate(){
    const ticket=epoch.current;
    const current=await request<Listing>(base+'/reports');
    if(ticket!==epoch.current)return;
    const created=await request<Report>(base+'/reports',json({expected_fingerprint:current.current_fingerprint,cover:browserCover()}));
    if(ticket!==epoch.current)return;
    setSelected(created.id);selectedRef.current=created.id;setReport(created);setSource(null);setEditSection('');setEditText('');setCoverOpen(false);await refresh();
    setNotice(`第${created.revision}版初稿已保存。旧版及人工修改稿均保留。`);
  }
  async function open(id:string){const ticket=epoch.current;const next=await request<Report>(base+'/reports/'+encodeURIComponent(id));if(ticket!==epoch.current)return;setReport(next);setSelected(id);setSource(null);setEditSection('');setEditText('');}
  async function saveEdit(){if(!report||!editSection||!editText.trim())return;const ticket=epoch.current;const created=await request<Report>(base+'/reports/'+encodeURIComponent(report.id)+'/edits',json({expected_revision:report.revision,section_id:editSection,text:editText,basis:editBasis||null}));
    if(ticket!==epoch.current)return;
    setSelected(created.id);selectedRef.current=created.id;setReport(created);setEditText('');setEditBasis('');setEditSection('');await refresh();setNotice(`人工说明已另存为第${created.revision}版，原稿保持不变。`);
  }
  async function download(kind:'export.docx'|'evidence.json'){
    if(!report)return;
    const ticket=epoch.current;
    const response=await fetch(base+'/reports/'+encodeURIComponent(report.id)+'/'+kind);
    if(ticket!==epoch.current)return;
    if(!response.ok){const value=await response.json().catch(()=>null);throw new Error(value?.detail?.message||'导出未完成，请重试。');}
    const objectUrl=URL.createObjectURL(await response.blob());
    const link=document.createElement('a');link.href=objectUrl;link.download=kind==='export.docx'?`尽调初稿-v${report.revision}.docx`:`尽调证据索引-v${report.revision}.json`;link.click();
    setTimeout(()=>URL.revokeObjectURL(objectUrl),10000);setNotice('已请求下载当前报告版本；导出不会生成新的报告版本。');
  }
  return <section className="rw-workspace" aria-label="报告生成与复核">
    <div className="rw-toolbar"><div><span className="dd-eyebrow">报告初稿与复核</span><h2>按本案来源生成，逐项核对后交付。</h2><p>保留原模板9章40表；缺资料列出缺口，输入不合格时生成无额度初稿。</p></div><div className="rw-actions"><button onClick={()=>void run('refresh',refresh)} disabled={!!busy}><RefreshCw size={14}/>刷新版本</button><button className="dd-primary" onClick={()=>void run('generate',generate)} disabled={!!busy||!listing}>{busy==='generate'?<LoaderCircle size={14} className="dd-spin"/>:<FileText size={14}/>}生成新初稿</button></div></div>
    <ReportCoverView caseId={caseId} caseTitle={caseTitle}/>
    {error&&<div className="rw-message error" role="alert"><AlertCircle size={15}/>{error}</div>}
    {notice&&<div className="rw-message" role="status"><Check size={15}/>{notice}</div>}
    {listing&&<div className="rw-meta-line"><span>模板 {listing.template_version}</span><span>{listing.binding_count}项绑定已登记处置 · {listing.table_count}个原栏目</span><span>正式业务复核待完成</span></div>}
    {listing&&listing.reports.length>0&&<div className="rw-version-bar"><History size={16}/><label>报告版本<select aria-label="选择报告版本" value={selected} disabled={!!busy} onChange={e=>void run('open',()=>open(e.target.value))}>{listing.reports.map(item=><option key={item.id} value={item.id}>第{item.revision}版 · {item.kind==='manual'?'人工修改':'生成初稿'} · {item.stale?'需重新核对':'待复核'} · {dateLabel(item.created_at)}</option>)}</select></label><button onClick={()=>void run('docx',()=>download('export.docx'))} disabled={!!busy||!report}><Download size={14}/>导出DOCX</button><button onClick={()=>void run('evidence',()=>download('evidence.json'))} disabled={!!busy||!report}><Download size={14}/>证据索引</button></div>}
    {report?<>
      {report.stale&&<div className="rw-message warning" role="status"><AlertCircle size={15}/><span>本版来源已变化：{report.stale_dependencies.map(v=>dependencyLabels[v]||v).join('、')}。旧内容仍保留，可生成新版后对照。</span></div>}
      {report.credit.effective_limit!==null
        ?<CreditHero score={report.credit.score!=null?Number(report.credit.score):null}
            limit={report.credit.effective_limit} rating={report.credit.rating}
            note={report.stale?'历史试算额度 · 重新生成后更新':'辅助测算授信额度 · 本次报告快照'}/>
        :null}
      <div className="rw-report-heading"><div><h3>{report.cover.title}</h3><p>第{report.revision}版 · {report.kind==='manual'?'人工修改稿':'来源快照初稿'} · {report.stale?'历史快照，需重新核对':report.branch==='no_limit_draft'?'无有效额度':'含辅助试算快照，仍待复核'}</p></div><span>{report.credit.effective_limit===null?'未输出有效额度':`${report.stale?'历史试算额度':'辅助试算额度'} ${report.credit.effective_limit} 元`}</span></div>
      {report.calculation_appendix&&<CalculationSnapshot appendix={report.calculation_appendix}/>}
      <div className="rw-document">{report.sections.map(section=><section key={section.id} className="rw-chapter"><header><h3>{section.title}</h3><button disabled={!!busy} onClick={()=>{setEditSection(section.id);setEditText('');setEditBasis('');}}><SquarePen size={13}/>添加人工说明</button></header>
        {section.text&&<p className="rw-chapter-text">{section.text}</p>}
        {editSection===section.id&&<div className="rw-edit"><label>人工补充说明<textarea aria-label="人工补充说明" value={editText} maxLength={12000} onChange={e=>setEditText(e.target.value)}/></label><label>说明依据（可选）<input aria-label="人工说明依据" value={editBasis} maxLength={2000} onChange={e=>setEditBasis(e.target.value)}/></label><p>新增人工版本，保留现有来源表格。人工文字不会自动变为已核验事实。</p><div className="rw-actions"><button onClick={()=>setEditSection('')} disabled={!!busy}>取消</button><button className="dd-primary" disabled={!!busy||!editText.trim()} onClick={()=>void run('edit',saveEdit)}><Save size={14}/>另存人工版本</button></div></div>}
        {section.tables.map(table=><details key={table.id} className="rw-table-block" open={table.rows.some(row=>!row.gap)}><summary><strong>{table.id} {table.title}</strong><span>{table.rows.filter(row=>!row.gap).length}项已有来源 · {table.rows.filter(row=>row.gap).length}项待补</span></summary><div className="rw-table-scroll"><table><thead><tr><th>栏目</th><th>本次记载</th><th>记录与核验状态</th><th>来源</th></tr></thead><tbody>{table.rows.map((row,index)=><tr key={(row.binding_id||'gap')+'-'+index}><td>{row.label}</td><td className={row.gap?'rw-gap':''}>{row.value===null?'待补充':<>{row.value}{row.unit?' '+row.unit:''}</>}{row.period&&<small>{row.period.label}</small>}{row.subject_id&&<small>主体：{row.subject_id}</small>}</td><td><span>{row.origin}</span><small>{row.status}</small></td><td>{row.reference_ids.map(id=><button key={id} className="rw-reference" onClick={()=>setSource(report.references.find(v=>v.id===id)||null)}>查看依据</button>)}{!row.reference_ids.length&&<span className="rw-gap">尚无可用来源</span>}</td></tr>)}</tbody></table></div></details>)}
        {section.manual_notes.map((note,index)=><div className="rw-manual-note" key={index}><strong>人工补充 · 尚未核验</strong><p>{note.text}</p>{note.basis&&<small>依据：{note.basis}</small>}</div>)}
      </section>)}</div>
      {source&&<aside className="rw-source" aria-label="报告来源详情"><header><h3>对应来源</h3><button onClick={()=>setSource(null)}>关闭依据</button></header><p>{source.field_id} · 事实第{source.fact_revision}版</p><p>记录状态：{source.record_confirmation}；核验状态：{source.verification}</p>{source.needs_review&&<p>该来源需要重新核对。</p>}{source.quote?<blockquote>{source.quote}</blockquote>:<p>材料来源引用保留于证据索引；未提供原件访问入口。</p>}{source.statement_id&&<small>原话第{source.statement_revision}版 · {source.quote_status==='historical_referenced_revision'?'保留原引用版本':'本次引用版本'}</small>}<details><summary>来源标识与定位</summary><pre>{JSON.stringify(source.source_ref,null,2)}</pre></details></aside>}
    </>:<div className="rw-empty"><FileText size={28}/><h3>{busy==='load'?'正在读取报告版本':'尚未生成本案报告'}</h3><p>可以先调整A4封面，再从本案已保存的来源生成带缺口初稿。</p></div>}
  </section>;
}

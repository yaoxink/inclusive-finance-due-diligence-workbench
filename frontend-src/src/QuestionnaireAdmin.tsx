import {useCallback, useEffect, useMemo, useState} from 'react';
import {Check, ListChecks, LoaderCircle, RefreshCw} from 'lucide-react';
import './QuestionnaireAdmin.css';

type QuestionItem={field_id:string;label:string;question_text?:string|null;section?:string|null;
  handling:string;required:boolean;disabled:boolean;value_type:string;unit?:string|null};
type View={version:string;fields:QuestionItem[]};
type Patch={required?:boolean;disabled?:boolean};

async function request<T>(url:string,init?:RequestInit):Promise<T>{
  const response=await fetch(url,init);
  const body=await response.json().catch(()=>null);
  if(!response.ok){const detail=body?.detail;const message=typeof detail==='object'?detail?.message||JSON.stringify(detail):detail||response.statusText;
    throw new Error(String(message));}
  return body as T;
}

export default function QuestionnaireAdmin(){
  const [view,setView]=useState<View|null>(null);
  const [draft,setDraft]=useState<Record<string,Patch>>({});
  const [loading,setLoading]=useState(false),[saving,setSaving]=useState(false),[applying,setApplying]=useState(false);
  const [error,setError]=useState(''),[notice,setNotice]=useState('');

  const refresh=useCallback(async()=>{
    setLoading(true);
    try{const next=await request<View>('/api/due-diligence/questionnaire');
      setView(next);setDraft({});setError('');}
    catch(exc){setError((exc as Error).message||'读取题库失败。');}
    finally{setLoading(false);}
  },[]);
  useEffect(()=>{void refresh();},[refresh]);

  const dirty=useMemo(()=>{
    if(!view)return {};
    const out:Record<string,Patch>={};
    for(const item of view.fields){
      const patch=draft[item.field_id];
      if(!patch)continue;
      const merged:Patch={};
      if(patch.required!==undefined&&patch.required!==item.required)merged.required=patch.required;
      if(patch.disabled!==undefined&&patch.disabled!==item.disabled)merged.disabled=patch.disabled;
      if(Object.keys(merged).length)out[item.field_id]=merged;
    }
    return out;
  },[view,draft]);
  const dirtyCount=Object.keys(dirty).length;

  const grouped=useMemo(()=>{
    const out:Record<string,QuestionItem[]>={};
    for(const q of view?.fields||[])(out[q.section||'未分组']??=[]).push(q);
    return out;
  },[view]);

  async function save(){
    if(!dirtyCount||saving)return;
    setSaving(true);setError('');setNotice('');
    try{
      const result=await request<{version:string;updated_fields:string[]}>('/api/due-diligence/questionnaire',{
        method:'PUT',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({fields:dirty})});
      setNotice(`已保存 ${result.updated_fields.length} 处调整（题库版本 ${result.version}）。新案件自动生效；现有案件需点「应用到现有案件」。`);
      await refresh();
    }catch(exc){setError((exc as Error).message||'保存失败。');}
    finally{setSaving(false);}
  }

  async function applyToCases(){
    if(applying)return;
    setApplying(true);setError('');setNotice('');
    try{
      const result=await request<{version:string;cases_updated:number}>('/api/due-diligence/questionnaire/apply',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
      setNotice(`已把题库（${result.version}）应用到 ${result.cases_updated} 个现有案件，刷新页面即可看到。`);
    }catch(exc){setError((exc as Error).message||'应用失败。');}
    finally{setApplying(false);}
  }

  const effective=(item:QuestionItem,key:'required'|'disabled')=>
    draft[item.field_id]?.[key]!==undefined?draft[item.field_id]![key]!:item[key];

  return <div className="qa-panel" aria-label="问卷题目管理">
    <div className="qa-head">
      <span className="qa-title"><ListChecks size={14}/> 问卷题目管理 <small>题库版本 {view?.version||'…'}</small></span>
      <div className="qa-actions">
        <button disabled={loading} onClick={()=>void refresh()}><RefreshCw size={13} className={loading?'dd-spin':''}/>刷新</button>
        <button className="qa-save" disabled={!dirtyCount||saving} onClick={()=>void save()}>
          {saving?<LoaderCircle size={13} className="dd-spin"/>:<Check size={13}/>}保存调整{dirtyCount?`（${dirtyCount}）`:''}</button>
        <button disabled={applying||!view} onClick={()=>void applyToCases()}>
          {applying?<LoaderCircle size={13} className="dd-spin"/>:<ListChecks size={13}/>}应用到现有案件</button>
      </div>
    </div>
    {error&&<p className="qa-error" role="alert">{error}</p>}
    {notice&&<p className="qa-notice" role="status"><Check size={12}/> {notice}</p>}
    <p className="qa-help">开关只影响「现场问卷」：停用的题目不再显示、不再发给识别匹配、也不计入必问/选问分母；必问⇄选问即时调整分母。保存后新案件自动生效，现有案件需手动应用。</p>
    {!view&&loading?<p className="qa-help">正在读取题库……</p>:
      Object.entries(grouped).map(([section,items])=>
        <div key={section} className="qa-group">
          <div className="qa-group-head"><strong>{section}</strong><span>{items.filter(i=>!effective(i,'disabled')).length}/{items.length}</span></div>
          {items.map(item=>{
            const disabled=effective(item,'disabled');
            return <div key={item.field_id} className={'qa-row'+(disabled?' off':'')}>
              <div className="qa-text">
                <span className="qa-q">{item.question_text||item.label}</span>
                <span className="qa-field">{item.label}{item.unit?`（${item.unit}）`:''} · {item.field_id}</span>
              </div>
              <label className="qa-switch" title="必问计入必问分母">
                <input type="checkbox" checked={effective(item,'required')&&!disabled} disabled={disabled}
                  onChange={event=>setDraft(previous=>({...previous,[item.field_id]:{...previous[item.field_id],required:event.target.checked}}))}/>
                <span>{effective(item,'required')&&!disabled?'必问':'选问'}</span>
              </label>
              <label className="qa-switch" title="停用后不出现在现场问卷">
                <input type="checkbox" checked={disabled}
                  onChange={event=>setDraft(previous=>({...previous,[item.field_id]:{...previous[item.field_id],disabled:event.target.checked}}))}/>
                <span>{disabled?'已停用':'启用'}</span>
              </label>
            </div>;})}
        </div>)}
  </div>;
}

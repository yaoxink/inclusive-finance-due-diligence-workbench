import {useCallback, useEffect, useState} from 'react';
import {AlertCircle, Check, LoaderCircle, Pencil, RefreshCw, X} from 'lucide-react';
import './CustomerInfoCard.css';

const API='/api/due-diligence';
type Info={case_id:string;revision:number;fields:Record<string,string|null>;actor:string;updated_at:string;
  changed_keys?:string[];supersedes_revision?:number|null};
type View={case_id:string;revision:number;customer_info:Info|null;history:{revision:number;actor:string;updated_at:string}[]};
type Props={caseId:string;allowed:boolean};

const fieldMeta:{key:string;label:string;required?:boolean}[]=[
  {key:'customer_name',label:'客户姓名',required:true},
  {key:'id_type',label:'证件类型',required:true},
  {key:'id_number',label:'证件号码',required:true},
  {key:'phone',label:'联系电话'},
  {key:'home_address',label:'常住地址'},
  {key:'contact_address',label:'通讯地址'}];

async function request(url:string,init?:RequestInit){
  const response=await fetch(url,init);
  const body=await response.json().catch(()=>null);
  if(!response.ok){const detail=body?.detail;
    throw new Error(typeof detail==='object'?(detail?.message||JSON.stringify(detail)):detail||response.statusText);}
  return body;
}

export default function CustomerInfoCard({caseId,allowed}:Props){
  const [view,setView]=useState<View|null>(null);
  const [editing,setEditing]=useState(false),[busy,setBusy]=useState(false);
  const [error,setError]=useState(''),[notice,setNotice]=useState('');
  const [draft,setDraft]=useState<Record<string,string>>({});

  const refresh=useCallback(async()=>{
    try{const body=await request(`${API}/cases/${caseId}/customer-info`) as View;
      setView(body);setDraft(Object.fromEntries(fieldMeta.map(item=>[item.key,body.customer_info?.fields?.[item.key]||''])));}
    catch(exc){setError((exc as Error).message);}
  },[caseId]);
  useEffect(()=>{setEditing(false);void refresh();},[refresh]);

  async function save(){
    setBusy(true);setError('');setNotice('');
    try{
      await request(`${API}/cases/${caseId}/customer-info`,{method:'PUT',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({fields:draft,expected_revision:view?.revision??0})});
      setNotice('客户信息已更新。');setEditing(false);await refresh();
    }catch(exc){
      const message=(exc as Error).message;
      setError(/已被他人修改|revision/i.test(message)?'信息已被其他修改更新，已为你刷新，请核对后重新保存。':message);
      await refresh();
    }finally{setBusy(false);}
  }
  const info=view?.customer_info;
  return <section className="ci-card" aria-label="客户基本信息">
    <div className="ci-head">
      <div><h3>客户基本信息</h3>
        <p>{info?`v${info.revision} · ${info.actor} 更新于 ${new Date(info.updated_at).toLocaleString('zh-CN')}`:'尚未填写；供准备阶段维护，可随时修订。'}</p></div>
      {!editing&&<button disabled={!allowed} onClick={()=>setEditing(true)}><Pencil size={13}/>编辑</button>}
      {info&&editing&&<button onClick={()=>{setEditing(false);void refresh();}}><X size={13}/>取消</button>}
    </div>
    {error&&<p className="ci-error" role="alert"><AlertCircle size={13}/>{error}</p>}
    {notice&&<p className="ci-notice" role="status"><Check size={13}/>{notice}</p>}
    {!view?<div className="ci-empty"><LoaderCircle size={16} className="dd-spin"/> 读取客户信息……</div>:
      !editing?<dl className="ci-grid">
        {fieldMeta.map(item=><div key={item.key}><dt>{item.label}</dt><dd>{info?.fields?.[item.key]||'—'}</dd></div>)}
      </dl>:
      <div className="ci-form">
        {fieldMeta.map(item=><label key={item.key}>{item.label}{item.required&&<em>必填</em>}
          <input value={draft[item.key]||''} onChange={event=>setDraft(previous=>({...previous,[item.key]:event.target.value}))}/>
        </label>)}
        <div className="ci-actions">
          <button className="dd-primary" disabled={busy||!fieldMeta.filter(item=>item.required).every(item=>(draft[item.key]||'').trim())} onClick={()=>void save()}>
            {busy?<LoaderCircle size={14} className="dd-spin"/>:<Check size={14}/>}保存（新版本）</button>
          <small>保存产生新版本，保留全部历史；并发修改不会互相覆盖。</small>
        </div>
      </div>}
  </section>;
}

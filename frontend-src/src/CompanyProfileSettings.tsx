import {useEffect,useRef,useState} from 'react';
import type {FormEvent} from 'react';
import {Check,FileText,LoaderCircle,RefreshCw} from 'lucide-react';

export type CompanyProfileFields={name:string;aliases:string;overview:string;capabilities:string;boundaries:string;source_notes:string;confirmed:boolean};
export type CompanyProfile=CompanyProfileFields&{version:number;updated_at:string};
const empty:CompanyProfileFields={name:'',aliases:'',overview:'',capabilities:'',boundaries:'',source_notes:'',confirmed:false};
const fields=(profile:CompanyProfile):CompanyProfileFields=>({name:profile.name,aliases:profile.aliases,overview:profile.overview,
  capabilities:profile.capabilities,boundaries:profile.boundaries,source_notes:profile.source_notes,confirmed:profile.confirmed});

export default function CompanyProfileSettings({loadProfile,saveProfile,onSaved}:{loadProfile:()=>Promise<CompanyProfile>;
  saveProfile:(fields:CompanyProfileFields)=>Promise<CompanyProfile>;onSaved:(profile:CompanyProfile)=>void}){
  const [saved,setSaved]=useState<CompanyProfile|null>(null),[draft,setDraft]=useState<CompanyProfileFields>(empty);
  const [loading,setLoading]=useState(true),[saving,setSaving]=useState(false);
  const [feedback,setFeedback]=useState<{kind:'success'|'error';text:string}|null>(null);
  const active=useRef(true),request=useRef(0);
  async function load(){
    const seq=++request.current;setLoading(true);setFeedback(null);
    try{const profile=await loadProfile();if(!active.current||seq!==request.current)return;setSaved(profile);setDraft(fields(profile));}
    catch(e){if(active.current&&seq===request.current)setFeedback({kind:'error',text:(e as Error).message});}
    finally{if(active.current&&seq===request.current)setLoading(false);}
  }
  useEffect(()=>{active.current=true;void load();return()=>{active.current=false;request.current++;};},[]);
  const dirty=!!saved&&JSON.stringify(draft)!==JSON.stringify(fields(saved));
  const normalized={...draft,name:draft.name.trim(),aliases:draft.aliases.trim(),overview:draft.overview.trim(),
    capabilities:draft.capabilities.trim(),boundaries:draft.boundaries.trim(),source_notes:draft.source_notes.trim()};
  const canSave=!!saved&&!loading&&!saving&&dirty&&(!draft.confirmed||!!(normalized.name&&normalized.capabilities));
  function edit(key:Exclude<keyof CompanyProfileFields,'confirmed'>,value:string){
    setDraft(previous=>({...previous,[key]:value,confirmed:false}));setFeedback(null);
  }
  async function save(e:FormEvent){
    e.preventDefault();if(!canSave)return;setSaving(true);setFeedback(null);
    try{const profile=await saveProfile(normalized);if(!active.current)return;setSaved(profile);setDraft(fields(profile));onSaved(profile);
      setFeedback({kind:'success',text:profile.confirmed?`公司资料 v${profile.version} 已确认，后续准备可用于合作分析。`:`公司资料草稿 v${profile.version} 已保存，确认能力后可用于合作分析。`});}
    catch(e){if(active.current)setFeedback({kind:'error',text:(e as Error).message});}
    finally{if(active.current)setSaving(false);}
  }
  return <section className="settings-section company-settings" aria-label="我方公司资料">
    <h3><FileText size={17}/> 我方公司资料 <span className="badge">{loading?'读取中':saved?.version?`v${saved.version} · ${saved.confirmed?'已确认':'草稿'}`:'待填写'}</span></h3>
    <p className="muted">让合作建议结合我方实际能力。先保存草稿，核对后再确认；每次修改都会保留资料版本。</p>
    {loading&&<p className="settings-loading" role="status"><LoaderCircle size={15} className="spin"/> 正在读取公司资料……</p>}
    <form onSubmit={save}>
      <fieldset disabled={!saved||loading||saving}>
        <label>公司名称<input maxLength={200} autoComplete="organization" placeholder="填写当前公司全称" value={draft.name} onChange={e=>edit('name',e.target.value)}/></label>
        <label>历史名称／简称<input maxLength={1000} placeholder="多个名称可用逗号分隔，便于核对旧资料" value={draft.aliases} onChange={e=>edit('aliases',e.target.value)}/></label>
        <label>公司简介<textarea rows={3} maxLength={6000} placeholder="主营业务、服务对象与定位" value={draft.overview} onChange={e=>edit('overview',e.target.value)}/></label>
        <label>可提供的能力<textarea rows={5} maxLength={12000} placeholder="列出目前确实可提供的产品、服务、数据或交付能力" value={draft.capabilities} onChange={e=>edit('capabilities',e.target.value)}/></label>
        <label>合作边界<textarea rows={3} maxLength={6000} placeholder="地域、授权、资质、交付或资源方面的限制" value={draft.boundaries} onChange={e=>edit('boundaries',e.target.value)}/></label>
        <label>资料说明<textarea rows={3} maxLength={6000} placeholder="资料来源、核对日期、尚需确认的事项" value={draft.source_notes} onChange={e=>edit('source_notes',e.target.value)}/></label>
        <label className="profile-confirm"><input type="checkbox" checked={draft.confirmed} disabled={!draft.name.trim()||!draft.capabilities.trim()} onChange={e=>{setDraft(previous=>({...previous,confirmed:e.target.checked}));setFeedback(null);}}/><span>确认以上能力可作为合作分析依据</span></label>
        <p className="settings-help">修改内容后需重新确认。尚未确认的草稿不会作为我方已具备的能力。</p>
      </fieldset>
      {feedback&&<div className={'settings-feedback '+feedback.kind} role={feedback.kind==='error'?'alert':'status'}>{feedback.text}</div>}
      {!saved&&!loading?<button type="button" onClick={load}><RefreshCw size={14}/> 重新读取公司资料</button>:<div className="settings-actions"><button type="submit" className="primary" disabled={!canSave}>{saving?<LoaderCircle size={15} className="spin"/>:<Check size={15}/>} {saving?'正在保存':draft.confirmed?'保存并确认资料':'保存公司资料草稿'}</button><span>{dirty?'有未保存的修改':'保存后用于下一次生成，已有稿件保留原依据'}</span></div>}
      <p className="settings-help profile-disclosure">资料保存在本机；生成准备稿时，所需内容会发送给已配置的模型。公开搜索仅使用对方名称和公开关键词。</p>
    </form>
  </section>;
}

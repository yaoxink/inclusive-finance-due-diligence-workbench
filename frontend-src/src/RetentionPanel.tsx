import {useEffect,useRef,useState} from 'react';
import {AlertCircle,RefreshCw,Trash2} from 'lucide-react';
import './RetentionPanel.css';

type Preview={case_id:string;case_title:string;fingerprint:string;counts:Record<string,number>;
  recording_directories:string[];exclusive_report_assets:string[];shared_report_assets:string[];
  backups:{id:string;case_ids:string[]}[];restore_copies:{relative_path:string;case_ids:string[];will_remove_entire_copy:boolean}[];
  blockers:{code:string;path?:string}[];can_delete:boolean};
type Deletion={case_id:string;deleted_at:string;expires_at:string;state:string;last_error:string|null};
const base='/api/due-diligence';
async function api<T>(path:string,body?:unknown):Promise<T>{
  const r=await fetch(base+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const value=await r.json().catch(()=>null);
  if(!r.ok)throw new Error(value?.detail?.message||'资料保留操作未完成，请重新检查。');
  return value as T;
}
const date=(value:string)=>new Date(value).toLocaleString('zh-CN');
const blockerLabels:Record<string,string>={
  unknown_restore_directory:'恢复区有未登记的目录，需先核对',unknown_restore_content:'恢复副本包含额外内容，需先核对',
  unknown_restore_file:'恢复副本有未登记文件',unknown_case_tables:'存在尚未纳入删除覆盖的数据表',
  unknown_recording_directory:'录音目录归属尚未确认',unknown_export_directory:'导出区有未登记目录',
  unknown_export_file:'导出区有未登记文件',unknown_report_asset:'报告配图归属尚未确认',
  unknown_backup_file:'备份区有未登记文件',unknown_backup:'备份内容尚未确认',
  unknown_file_type:'资料包含不支持的文件类型',unknown_delete_job:'清理任务记录需先核对',
  unresolved_retention_files:'有上次尚未完成的清理任务',unsafe_backup_inventory:'备份清单未通过校验',
  unsafe_delete_path:'资料路径未通过安全检查',recording_busy:'录音或转写仍在运行',
  coverage_changed:'资料已变化，请重新检查删除范围',database_checkpoint_busy:'资料库仍在使用，请稍后重试'};

export default function RetentionPanel({caseId,caseTitle,onDeleted}:{caseId?:string;caseTitle?:string;onDeleted:(id:string)=>void}){
  const [preview,setPreview]=useState<Preview|null>(null),[title,setTitle]=useState(''),[ack,setAck]=useState(false);
  const [busy,setBusy]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const [deletions,setDeletions]=useState<Deletion[]>([]),[expanded,setExpanded]=useState(false);
  const current=useRef(caseId);current.current=caseId;
  const load=async()=>{const value=await api<{deletions:Deletion[]}>('/retention/status');setDeletions(value.deletions);};
  useEffect(()=>{setPreview(null);setTitle('');setAck(false);setError('');setNotice('');setExpanded(false);void load().catch(()=>{});},[caseId]);
  const inspect=async()=>{
    if(!caseId)return;const target=caseId;setBusy('preview');setError('');setNotice('');setPreview(null);
    try{const value=await api<Preview>(`/cases/${target}/deletion-preview`);if(current.current===target){setPreview(value);setTitle('');setAck(false);}}
    catch(e){if(current.current===target)setError(e instanceof Error?e.message:'覆盖检查未完成。');}finally{setBusy('');}
  };
  const remove=async()=>{
    if(!caseId||!preview)return;const target=caseId;setBusy('delete');setError('');setNotice('');
    try{
      const result=await api<{state:string;backup_expires_at:string}>(`/cases/${target}/delete`,{expected_fingerprint:preview.fingerprint,confirm_title:title});
      let cacheError=false;
      try{localStorage.removeItem(`due-diligence:report-cover:v1:${encodeURIComponent(target)}`);}catch{cacheError=true;}
      if(current.current===target){
        setPreview(null);setTitle('');setAck(false);
        setNotice((result.state==='deleted'?`本案活动数据已删除。相关本机备份受限保留不超过7天；含更早删除案件的整库备份按更早期限清理，具体见备份列表。`:'本案活动入口已关闭，文件清理尚未全部完成；请重试清理并查看处理状态。')+(cacheError?' 浏览器未允许清除本案封面缓存，请手动清除站点数据。':''));
      }
      onDeleted(target);
      void load().catch(()=>{});
    }catch(e){if(current.current===target){setError(e instanceof Error?e.message:'删除未完成。');setPreview(null);setTitle('');setAck(false);}}
    finally{setBusy('');}
  };
  const cleanup=async()=>{
    setBusy('cleanup');setError('');setNotice('');
    try{const value=await api<{jobs_completed:number;backups_purged:number;errors:unknown[]}>('/retention/cleanup',{});await load();window.dispatchEvent(new Event('due-diligence:retention-changed'));
      setNotice(`本次完成 ${value.jobs_completed} 个待清理任务、清理 ${value.backups_purged} 个到期备份。${value.errors.length?'仍有文件清理失败，可稍后重试。':''}`);
    }catch(e){setError(e instanceof Error?e.message:'清理未完成。');}finally{setBusy('');}
  };
  return <section className="dd-card retention-panel" aria-label="案件删除与七天保留">
    <div className="dd-section-heading"><div><h3><Trash2 size={16}/> 案件删除与保留</h3><p>手动确认后移除本案活动数据；相关本机备份受限保留不超过7天；共享整库备份按最早期限清理。</p></div>
      <button disabled={!!busy} onClick={()=>setExpanded(!expanded)}>{expanded?'收起删除检查':'查看删除范围'}</button></div>
    {error&&<p className="dd-notice" role="alert"><AlertCircle size={14}/>{error}</p>}
    {notice&&<p className="dd-notice" role="status">{notice}</p>}
    {expanded&&<div className="retention-body">
      <p>当前案件：<strong>{caseTitle||'尚未选择'}</strong>。先检查覆盖范围，再输入案件名称确认。不会删除其他案件的当前活动数据。</p>
      <button disabled={!!busy||!caseId} onClick={()=>void inspect()}>{busy==='preview'?<RefreshCw size={14} className="dd-spin"/>:null}检查本案删除范围</button>
      {preview&&<div className="retention-preview"><h4>{preview.case_title} · 删除覆盖预览</h4>
        <dl><div><dt>活动数据</dt><dd>{Object.values(preview.counts).reduce((a,b)=>a+b,0)} 条域记录与案件本身</dd></div>
          <div><dt>录音目录</dt><dd>{preview.recording_directories.length} 个</dd></div><div><dt>独占配图资产</dt><dd>{preview.exclusive_report_assets.length} 个；共享资产 {preview.shared_report_assets.length} 个继续保留</dd></div>
          <div><dt>整库备份</dt><dd>{preview.backups.length} 个含本案；立即禁止下载/恢复，最迟7天到期；含更早删除案时按更早期限清理</dd></div>
          <div><dt>恢复副本</dt><dd>{preview.restore_copies.length} 个整库副本将清空正文和文件</dd></div></dl>
        {!!preview.restore_copies.length&&<div className="retention-copy-note">以下恢复副本同时含其他案件的历史副本，将一并清空；其他案件的当前活动数据保留。<ul>{preview.restore_copies.map(r=><li key={r.relative_path}>{r.relative_path} · 含 {r.case_ids.length} 个案件</li>)}</ul></div>}
        {!!preview.blockers.length&&<div role="alert">以下内容尚不能安全处理，确认删除暂不可用：<ul>{preview.blockers.map((b,i)=><li key={i}>{blockerLabels[b.code]||'有尚未确认的资料对象'}{b.path?`：${b.path}`:''}</li>)}</ul></div>}
        <p>已下载到其他位置的报告、索引或备份由持有人另行处置。本应用不扫描离线副本，不承诺SSD物理擦除。</p>
        <label className="retention-confirm"><input type="checkbox" checked={ack} onChange={e=>setAck(e.target.checked)}/>我已核对上述范围，包括整库恢复副本的处理。</label>
        <label>输入完整案件名称<input aria-label="删除案件名称确认" value={title} autoComplete="off" onChange={e=>setTitle(e.target.value)} placeholder={preview.case_title}/></label>
        <button className="retention-delete" disabled={!!busy||!preview.can_delete||!ack||title!==preview.case_title} onClick={()=>void remove()}>{busy==='delete'?'正在移除资料':'确认删除本案活动数据'}</button>
      </div>}
    </div>}
    <details className="retention-history"><summary>保留与清理记录（{deletions.length}）</summary>
      <button disabled={!!busy} onClick={()=>void cleanup()}>{busy==='cleanup'?'正在处理':'重试清理到期备份与待清理文件'}</button>
      {!deletions.length?<p>尚无删除记录。</p>:<ul>{deletions.map(d=><li key={d.case_id}><code>{d.case_id}</code><br/>{date(d.deleted_at)} · {d.state==='completed'?'活动副本已清理':'仍有文件待清理'} · 本案最晚保留期限 {date(d.expires_at)}{d.last_error?` · ${d.last_error}`:''}</li>)}</ul>}
    </details>
  </section>;
}

import {useEffect,useRef,useState} from 'react';
import {Archive,CheckCircle2,Download,RefreshCw} from 'lucide-react';

type Backup={id:string;created_at:string;bytes:number;file_count:number;restricted?:boolean;status?:string;expires_at?:string|null};
type Verified={state:string;files_verified:number;table_counts:Record<string,number>;active_workspace_changed:boolean};
const base='/api/due-diligence/maintenance/backups';
async function request<T>(path:string,write=false):Promise<T>{
  const response=await fetch(path,write?{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}:{});
  const body=await response.json().catch(()=>null);
  if(!response.ok)throw new Error(body?.detail?.message||'本地备份操作未完成，请重试。');
  return body as T;
}
export default function MaintenancePanel(){
  const [items,setItems]=useState<Backup[]>([]),[busy,setBusy]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const alive=useRef(true),sequence=useRef(0);
  const load=async()=>{const ticket=++sequence.current;const result=await request<{backups:Backup[]}>(base);if(alive.current&&ticket===sequence.current)setItems(result.backups);};
  useEffect(()=>{
    alive.current=true;
    const refresh=()=>{void load().catch(e=>{if(alive.current)setError(String(e.message));});};
    refresh();window.addEventListener('due-diligence:retention-changed',refresh);
    return()=>{alive.current=false;sequence.current++;window.removeEventListener('due-diligence:retention-changed',refresh);};
  },[]);
  const create=async()=>{setBusy('create');setError('');setNotice('');try{await request(base,true);await load();setNotice('备份已创建并完成文件校验。');}catch(e){setError(e instanceof Error?e.message:'未完成备份。');}finally{setBusy('');}};
  const verify=async(id:string)=>{setBusy(id);setError('');setNotice('');try{const result=await request<Verified>(`${base}/${id}/verify`,true);setNotice(`恢复验证通过：${result.files_verified} 个文件、${Object.keys(result.table_counts).length} 张数据表已核对，当前工作空间未改变。`);}catch(e){setError(e instanceof Error?e.message:'恢复验证未完成。');}finally{setBusy('');}};
  return <section className="dd-card" aria-label="本地备份与恢复" style={{marginTop:20,padding:20}}>
    <div className="dd-section-heading"><div><h3><Archive size={16}/> 本地备份与恢复</h3><p style={{fontSize:12,color:'var(--dd-muted)',marginTop:5}}>保存本应用的合成案件、录音归档、事实、版本和导出。恢复检查使用新目录。</p></div><button disabled={!!busy} onClick={()=>void create()}>{busy==='create'?<RefreshCw size={14} className="dd-spin"/>:<Archive size={14}/>}创建备份</button></div>
    {error&&<p role="alert" className="dd-notice">{error}</p>}{notice&&<p role="status" className="dd-notice success"><CheckCircle2 size={14}/>{notice}</p>}
    <p style={{fontSize:11,color:'var(--dd-muted)',margin:'12px 0'}}>当前备份用于合成演示，未加密；日志不入包，已下载的副本需自行保管。正在录音或转写时不能创建备份。</p>
    <p style={{fontSize:11,color:'var(--dd-muted)',margin:'8px 0'}}>含已删除案件的旧备份受限保留7天，期间不能下载或恢复，到期清理；其他备份继续手动保管。</p>
    {!items.length?<p style={{fontSize:12,color:'var(--dd-muted)'}}>尚无备份。</p>:<ul style={{listStyle:'none',padding:0,margin:0}}>{items.map(item=>{
      const restricted=!!item.restricted||item.status==='expired'||item.status==='purged';
      return <li key={item.id} style={{display:'flex',gap:12,flexWrap:'wrap',alignItems:'center',padding:'12px 0',borderTop:'1px solid var(--dd-line)'}}><span style={{flex:1,minWidth:170,fontSize:12}}>{new Date(item.created_at).toLocaleString('zh-CN')}<small style={{display:'block',color:'var(--dd-muted)'}}>{(item.bytes/1048576).toFixed(2)} MB · {item.file_count} 个文件</small>{restricted&&<small style={{display:'block',color:'var(--dd-muted)'}}>含已删除案件 · 受限保留{item.expires_at?`至 ${new Date(item.expires_at).toLocaleString('zh-CN')}`:''}</small>}</span><button disabled={!!busy||restricted} onClick={()=>void verify(item.id)}>{restricted?'受限，不能恢复':busy===item.id?'正在检查':'验证恢复'}</button>{restricted?<span style={{color:'var(--dd-muted)',fontSize:12}}>不可下载</span>:<a href={`${base}/${item.id}/download`} download style={{color:'var(--dd-accent)',fontSize:12,display:'inline-flex',alignItems:'center',gap:5}}><Download size={13}/>下载</a>}</li>;
    })}</ul>}
  </section>;
}

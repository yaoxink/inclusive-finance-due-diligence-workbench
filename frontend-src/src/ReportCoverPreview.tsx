import {useEffect,useRef,useState} from 'react';
import {Check,ChevronDown,ImagePlus,RotateCcw,Save,SlidersHorizontal,ImageOff} from 'lucide-react';
import agriculturalCover from './assets/cover-agricultural-v2.png';
import './ReportCoverPreview.css';

type CoverSettings={title:string;subtitle:string;organization:string;author:string;date:string;imageMode:'scene'|'none';focusX:number;focusY:number;fit:'cover'|'contain';};
type StoredSettings={schema:1;settings:CoverSettings;localImagePreviouslyUsed:boolean;};
type Loaded={settings:CoverSettings;notice:string;};
const defaults=():CoverSettings=>({title:'农户经营尽职调查报告',subtitle:'经营情况与访谈记录',organization:'',author:'',date:'',imageMode:'scene',focusX:50,focusY:50,fit:'cover'});
const storageKey=(caseId:string)=>`due-diligence:report-cover:v1:${encodeURIComponent(caseId)}`;
const textLimits={title:48,subtitle:72,organization:60,author:40};
const isSettings=(value:unknown):value is CoverSettings=>{
  if(!value||typeof value!=='object')return false;
  const v=value as Record<string,unknown>;
  return Object.entries(textLimits).every(([key,max])=>typeof v[key]==='string'&&(v[key] as string).length<=max)
    &&typeof v.date==='string'&&(v.date===''||/^\d{4}-\d{2}-\d{2}$/.test(v.date))
    &&(v.imageMode==='scene'||v.imageMode==='none')&&(v.fit==='cover'||v.fit==='contain')
    &&typeof v.focusX==='number'&&Number.isFinite(v.focusX)&&v.focusX>=0&&v.focusX<=100
    &&typeof v.focusY==='number'&&Number.isFinite(v.focusY)&&v.focusY>=0&&v.focusY<=100;
};
async function fetchServerSettings(caseId:string):Promise<Partial<CoverSettings>|null>{
  try{
    const response=await fetch('/api/due-diligence/cases/'+encodeURIComponent(caseId)+'/report-cover');
    if(!response.ok)return null;
    const body=await response.json();
    return body&&body.settings&&isSettings(body.settings)?body.settings as Partial<CoverSettings>:null;
  }catch{return null;}
}
function loadSettings(caseId:string):Loaded{
  try{
    const raw=localStorage.getItem(storageKey(caseId));
    if(!raw)return {settings:defaults(),notice:''};
    const stored:unknown=JSON.parse(raw);
    if(!stored||typeof stored!=='object'||(stored as StoredSettings).schema!==1||!isSettings((stored as StoredSettings).settings))throw new Error('invalid');
    const value=stored as StoredSettings;
    return {settings:value.settings,notice:value.localImagePreviouslyUsed?'已恢复文字与裁切设置。上次自选图片仅临时使用，当前显示默认农田图；如需使用原图，请重新选择。':''};
  }catch{return {settings:defaults(),notice:'保存的封面设置暂时无法读取，已显示默认样式。你可以继续修改，保存时会再次提示结果。'};}
}
function displayDate(value:string){
  if(!value)return '日期待填写';
  const [year,month,day]=value.split('-');
  return `${year}年${Number(month)}月${Number(day)}日`;
}

export default function ReportCoverPreview({caseId,caseTitle}:{caseId:string;caseTitle:string}){
  const [loaded]=useState(()=>loadSettings(caseId));
  const [settings,setSettings]=useState<CoverSettings>(loaded.settings);
  // server-saved cover settings win over the local draft (export reads them)
  useEffect(()=>{let active=true;void fetchServerSettings(caseId).then(server=>{
    if(!active||!server)return;setSettings(current=>({...current,...server}));});return()=>{active=false;};},[caseId]);
  const [notice,setNotice]=useState(loaded.notice),[error,setError]=useState(''),[saved,setSaved]=useState(false);
  const [localImage,setLocalImage]=useState<string|null>(null),[localImageName,setLocalImageName]=useState(''),[reading,setReading]=useState(false),[imageError,setImageError]=useState(false);
  const fileInput=useRef<HTMLInputElement>(null),imageRequest=useRef(0);
  const update=<K extends keyof CoverSettings>(key:K,value:CoverSettings[K])=>{setSettings(current=>({...current,[key]:value}));setSaved(false);setError('');};
  const chooseDefault=()=>{imageRequest.current++;setReading(false);setLocalImage(null);setLocalImageName('');setImageError(false);update('imageMode','scene');setNotice('已恢复默认农田场景图。');};
  const save=()=>{
    setError('');
    if(!settings.title.trim()){setError('请先填写报告标题。');return;}
    try{
      const value:StoredSettings={schema:1,settings:{...settings,title:settings.title.trim()},localImagePreviouslyUsed:!!localImage&&settings.imageMode==='scene'};
      localStorage.setItem(storageKey(caseId),JSON.stringify(value));setSettings(value.settings);setSaved(true);
      setNotice(localImage&&settings.imageMode==='scene'?'文字和裁切设置已保存到当前浏览器。自选图片仅在本次打开时使用，重新打开后需要再次选择。':'本案封面样式已保存到当前浏览器，重新打开本案后可以继续修改。');
    }catch{setSaved(false);setError('当前浏览器未能保存样式。预览仍可继续使用，但关闭后本次修改可能无法保留。');}
  };
  const restore=()=>{imageRequest.current++;setSettings(defaults());setLocalImage(null);setLocalImageName('');setReading(false);setImageError(false);setSaved(false);setError('');setNotice('已恢复默认预览；点击“保存本案样式”后才会替换之前保存的设置。');};
  const chooseLocalImage=(file:File|undefined)=>{
    if(!file)return;
    setError('');setSaved(false);
    if(!['image/png','image/jpeg','image/webp'].includes(file.type)){setError('请选择 PNG、JPG 或 WebP 图片，原有配图未改变。');return;}
    if(file.size>5*1024*1024){setError('图片超过5 MB，请选择较小图片，原有配图未改变。');return;}
    const request=++imageRequest.current;setReading(true);
    const reader=new FileReader();
    reader.onerror=()=>{if(imageRequest.current===request){setReading(false);setError('图片未能读取，原有配图未改变。');}};
    reader.onload=()=>{
      if(imageRequest.current!==request)return;
      if(typeof reader.result!=='string'){setReading(false);setError('图片未能读取，原有配图未改变。');return;}
      const url=reader.result;const image=new Image();
      image.onerror=()=>{if(imageRequest.current===request){setReading(false);setError('这张文件无法作为图片显示，请更换图片。原有配图未改变。');}};
      image.onload=()=>{if(imageRequest.current===request){setReading(false);setLocalImage(url);setLocalImageName(file.name);setImageError(false);setSettings(current=>({...current,imageMode:'scene'}));setNotice('自选图片只在当前页面预览，不上传、不作为案件附件保存。重新打开后需再次选择。');}};
      image.src=url;
    };
    reader.readAsDataURL(file);
  };
  const imageVisible=settings.imageMode==='scene';
  return <section className="rc-workspace" aria-label="报告封面样式预览">
    <div className="dd-section-heading"><div><span className="dd-eyebrow">报告外观</span><h2>先把报告封面，调整到合适的样子。</h2><p className="rc-intro">A4竖版，210 × 297 mm。保存样式后，在报告区生成带来源的初稿。</p></div></div>
    <div className="rc-layout"><div className="rc-preview"><div className="rc-preview-label"><span>封面样式预览</span><span>A4 纵向</span></div>
      <article className="rc-paper rc-paper-fullbleed" aria-label="A4报告封面预览">
        <div className="rc-fullbleed-art">
          {imageVisible
            ? <img src={localImage||agriculturalCover} alt={localImage?'本次选择的封面图片':'农业尽调报告封面'} style={{objectFit:settings.fit,objectPosition:`${settings.focusX}% ${settings.focusY}%`}} onLoad={()=>setImageError(false)} onError={()=>setImageError(true)}/>
            : <div className="rc-fullbleed-textcover"><span className="rc-kicker">农户经营 / 尽职调查</span><h3>{settings.title||'报告标题待填写'}</h3><p className="rc-subtitle">{settings.subtitle||'副标题待填写'}</p></div>}
          {imageVisible&&!imageError&&<div className="rc-art-meta">
            <span>农户名称：{caseTitle.split('-')[0]}</span>
            <span>尽调人员：{settings.author||'待填写'}</span>
            <span>报告日期：{displayDate(settings.date)}</span>
          </div>}
          {imageError&&<span className="rc-image-error">配图暂未显示，请恢复默认配图或重新选择。</span>}
        </div>
      </article><p className="rc-preview-help">封面整页输出；尽调人员与报告日期印在封面留白处，其余案件信息在报告首页展示。</p>
    </div>
      <aside className="rc-editor"><details><summary><SlidersHorizontal size={16}/><strong>调整封面</strong><ChevronDown size={15}/></summary><div className="rc-editor-content">
      <label>报告标题<input aria-label="封面报告标题" value={settings.title} maxLength={48} onChange={event=>update('title',event.target.value)}/></label>
      <label>副标题<input aria-label="封面副标题" value={settings.subtitle} maxLength={72} onChange={event=>update('subtitle',event.target.value)}/></label>
      <label>出具单位<input aria-label="封面出具单位" value={settings.organization} maxLength={60} placeholder="填写机构名称" autoComplete="off" onChange={event=>update('organization',event.target.value)}/></label>
      <div className="rc-editor-pair"><label>经办人<input aria-label="封面经办人" value={settings.author} maxLength={40} placeholder="填写姓名" autoComplete="off" onChange={event=>update('author',event.target.value)}/></label><label>报告日期<input type="date" aria-label="封面报告日期" value={settings.date} onChange={event=>update('date',event.target.value)}/></label></div>
      <div className="rc-editor-divider"/>
      <label>封面配图<select aria-label="封面配图方式" value={settings.imageMode} onChange={event=>update('imageMode',event.target.value as CoverSettings['imageMode'])}><option value="scene">{localImage?'本次自选图片':'农田场景配图'}</option><option value="none">无配图 · 简洁文字封面</option></select></label>
      {imageVisible&&<><div className="rc-image-actions"><input ref={fileInput} className="rc-file-input" type="file" accept="image/png,image/jpeg,image/webp" aria-label="选择本地封面图片" onChange={event=>{chooseLocalImage(event.target.files?.[0]);event.target.value='';}}/><button type="button" onClick={()=>fileInput.current?.click()} disabled={reading}><ImagePlus size={14}/>{reading?'正在读取':'更换本地图片'}</button>{localImage&&<button type="button" onClick={chooseDefault}><RotateCcw size={13}/>恢复场景图</button>}</div>{localImageName&&<p className="rc-local-name" title={localImageName}>{localImageName}</p>}<p className="rc-field-help">PNG / JPG / WebP，最多5 MB。自选图片只在本次打开时使用。</p>
        <label>图片显示方式<select aria-label="封面图片显示方式" value={settings.fit} onChange={event=>update('fit',event.target.value as CoverSettings['fit'])}><option value="cover">铺满图片区域</option><option value="contain">显示完整图片</option></select></label>
        <label className="rc-range-label"><span>左右位置 <output>{settings.focusX}%</output></span><input aria-label="封面图片左右位置" type="range" min="0" max="100" value={settings.focusX} onChange={event=>update('focusX',Number(event.target.value))}/></label>
        <label className="rc-range-label"><span>上下位置 <output>{settings.focusY}%</output></span><input aria-label="封面图片上下位置" type="range" min="0" max="100" value={settings.focusY} onChange={event=>update('focusY',Number(event.target.value))}/></label></>}
      {!imageVisible&&<p className="rc-field-help rc-no-image"><ImageOff size={14}/>当前使用无配图封面，可随时切回农田场景。</p>}
      {error&&<p className="rc-message rc-error" role="alert">{error}</p>}{notice&&<p className="rc-message" role="status">{saved&&<Check size={14}/>}<span>{notice}</span></p>}
      <div className="rc-save-actions"><button className="dd-primary" type="button" onClick={save} disabled={reading}><Save size={14}/>{saved?'样式已保存':'保存本案样式'}</button><button type="button" onClick={restore}><RotateCcw size={14}/>恢复默认</button></div><p className="rc-field-help">设置仅保存在当前浏览器，按案件分别记录，不会生成业务报告版本。</p>
    </div></details></aside></div>
    <div className="rc-next-note"><strong>报告正文与导出</strong><span>在本页报告区查看来源、添加人工说明，并下载DOCX和证据索引。</span></div>
  </section>;
}

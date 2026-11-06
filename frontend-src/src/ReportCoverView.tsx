import {useEffect, useState} from 'react';
import agriculturalCover from './assets/cover-agricultural-v2.png';
import './ReportCoverPreview.css';

type CoverSettings={author:string;date:string};

/** Read-only cover preview: full-bleed artwork with the printed meta line.
 * No editing controls — settings come from the server-saved cover config. */
export default function ReportCoverView({caseId,caseTitle}:{caseId:string;caseTitle:string}){
  const [settings,setSettings]=useState<CoverSettings>({author:'',date:''});
  useEffect(()=>{
    let active=true;
    (async()=>{
      try{
        const response=await fetch('/api/due-diligence/cases/'+encodeURIComponent(caseId)+'/report-cover');
        if(!response.ok)return;
        const body=await response.json();
        if(active&&body?.settings)setSettings({author:body.settings.author||'',date:body.settings.date||''});
      }catch{/* preview falls back to placeholders */}
    })();
    return()=>{active=false;};
  },[caseId]);
  const date=settings.date?`${settings.date.split('-')[0]}年${Number(settings.date.split('-')[1])}月${Number(settings.date.split('-')[2])}日`:'待填写';
  return <section className="rc-workspace" aria-label="报告封面预览">
    <div className="rc-layout"><div className="rc-preview"><div className="rc-preview-label"><span>封面预览</span><span>A4 · 满页输出</span></div>
      <article className="rc-paper rc-paper-fullbleed" aria-label="A4报告封面预览">
        <div className="rc-fullbleed-art">
          <img src={agriculturalCover} alt="农业尽调报告封面"
               style={{objectFit:'cover',objectPosition:'50% 50%'}}/>
          <div className="rc-art-meta">
            <span>农户名称：{caseTitle.split('-')[0]}</span>
            <span>尽调人员：{settings.author||'待填写'}</span>
            <span>报告日期：{date}</span>
          </div>
        </div>
      </article>
    </div></div>
  </section>;
}

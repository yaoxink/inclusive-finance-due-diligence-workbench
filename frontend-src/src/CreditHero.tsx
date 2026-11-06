import {useEffect, useState} from 'react';
import {ShieldCheck} from 'lucide-react';
import './CreditHero.css';

type Props={score?:string|number|null;limit?:string|number|null;rating?:string|null;note?:string};

export default function CreditHero({score,limit,rating,note}:Props){
  const [ready,setReady]=useState(false);
  useEffect(()=>{const timer=window.setTimeout(()=>setReady(true),80);return ()=>window.clearTimeout(timer);},[]);
  const value=Number(score??0)||0;
  return <section className="ch-hero" aria-label="授信测算结果">
    <div className="ch-glow" aria-hidden="true"/>
    <div className="ch-gauge" role="img" aria-label={`模型评分 ${score ?? '—'} 分`}>
      <svg viewBox="0 0 120 120">
        <defs>
          <linearGradient id="chArc" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="#0c769a"/><stop offset="55%" stopColor="#27ae7c"/><stop offset="100%" stopColor="#7a5cff"/>
          </linearGradient>
        </defs>
        <circle cx="60" cy="60" r="52" className="ch-track"/>
        <circle cx="60" cy="60" r="52" className="ch-arc" stroke="url(#chArc)" transform="rotate(-90 60 60)"
          style={{strokeDasharray:`${ready?(value/100)*326.7:0} 326.7`}}/>
      </svg>
      <div className="ch-center"><strong>{score ?? '—'}</strong><span>模型评分</span></div>
    </div>
    <div className="ch-limit">
      <span className="ch-eyebrow">辅助测算授信额度</span>
      <strong className="ch-amount"><i>¥</i>{Number(limit??0).toLocaleString('zh-CN')}</strong>
      <div className="ch-meta">
        {rating&&<span className="ch-rating"><ShieldCheck size={14}/>信用等级 {rating}</span>}
        <span>{note||'授信参考 · 以行内审批为准'}</span>
      </div>
    </div>
  </section>;
}

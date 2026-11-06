import {useEffect, useState} from 'react';
import {Activity, AudioLines, FileClock, Sparkles} from 'lucide-react';
import './OnsiteOverview.css';

const API = '/api/due-diligence';

type Progress = {required: {done: number; applicable: number}; optional: {done: number; total: number};
  pending_judgement: number; not_applicable: number};
type Candidates = {candidates: unknown[]};
export type OverviewRecordings = {segments: number; seconds: number; count: number};

export default function OnsiteOverview({caseId, recordings}: {caseId: string; recordings: OverviewRecordings}) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const [candidates, setCandidates] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = () => {
      Promise.all([
        fetch(`${API}/cases/${encodeURIComponent(caseId)}/progress`).then(r => r.ok ? r.json() : null),
        fetch(`${API}/cases/${encodeURIComponent(caseId)}/extraction/candidates`).then(r => r.ok ? r.json() : null),
      ]).then(([p, c]) => {
        if (!alive) return;
        if (p) setProgress(p);
        if (c) setCandidates((c as Candidates).candidates?.length || 0);
      }).catch(() => {});
    };
    load();
    const timer = setInterval(load, 15000);
    return () => { alive = false; clearInterval(timer); };
  }, [caseId]);

  const bar = (done: number, total: number) =>
    <span className="oo-bar"><i style={{width: total ? `${Math.min(100, (done / total) * 100)}%` : '0%'}}/></span>;

  return <div className="oo-card" aria-label="访谈概览">
    <div className="oo-title"><Activity size={14}/>访谈概览</div>
    <div className="oo-grid">
      <div><span>录音段</span><strong>{recordings.count}</strong></div>
      <div><span>累计时长</span><strong>{Math.floor(recordings.seconds / 60)}<small> 分</small>{Math.round(recordings.seconds % 60)}<small> 秒</small></strong></div>
      <div><span>稳定转写</span><strong>{recordings.segments}<small> 句</small></strong></div>
      <div><span>待采纳候选</span><strong className={candidates ? 'oo-hot' : ''}>{candidates}</strong></div>
    </div>
    {progress && <>
      <div className="oo-progress"><span>必问 {progress.required.done}/{progress.required.applicable}</span>{bar(progress.required.done, progress.required.applicable)}</div>
      <div className="oo-progress"><span>选问 {progress.optional.done}/{progress.optional.total}</span>{bar(progress.optional.done, progress.optional.total)}</div>
      {(progress.pending_judgement > 0 || progress.not_applicable > 0) && (
        <div className="oo-flags">
          {progress.pending_judgement > 0 && <span><FileClock size={12}/>{progress.pending_judgement} 待判断</span>}
          {progress.not_applicable > 0 && <span>{progress.not_applicable} 不适用</span>}
        </div>)}
      {candidates > 0 && <p className="oo-hint"><Sparkles size={12}/>问卷中有 {candidates} 条录音候选可采纳</p>}
      {recordings.count > 0 && recordings.segments === 0 && <p className="oo-hint"><AudioLines size={12}/>录音已保存，等待稳定转写</p>}
    </>}
  </div>;
}

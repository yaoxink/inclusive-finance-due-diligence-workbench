import {useEffect, useState} from 'react';
import {BadgeCheck, ClipboardList, Mic} from 'lucide-react';
import './CaseHeaderBadges.css';

const API = '/api/due-diligence';

type Progress = {required: {done: number; applicable: number}; optional: {done: number; total: number}};
type Run = {state: string; stale: boolean; score: string | null; rating: string | null;
  effective_limit: string | null; current_effective_limit?: string | null};

/** Case-level status badges shown on the right of the case title header. */
export default function CaseHeaderBadges({caseId, interviews, recordings}: {
  caseId: string; interviews: number; recordings: number}) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const [run, setRun] = useState<Run | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => Promise.all([
      fetch(`${API}/cases/${encodeURIComponent(caseId)}/progress`).then(r => r.ok ? r.json() : null),
      fetch(`${API}/cases/${encodeURIComponent(caseId)}/calculation/v2`).then(r => r.ok ? r.json() : null),
    ]).then(([p, c]) => {
      if (!alive) return;
      if (p) setProgress(p);
      if (c) setRun(c.latest_run || null);
    }).catch(() => {});
    load();
    const timer = setInterval(load, 15000);
    return () => { alive = false; clearInterval(timer); };
  }, [caseId]);

  const qualified = !!run && run.state === 'valid' && !run.stale;
  const limit = qualified ? run!.current_effective_limit ?? run!.effective_limit : null;

  return <div className="chb-badges" aria-label="案件状态摘要">
    {progress && <div className="chb-badge">
      <ClipboardList size={13}/>
      <span>必问 <b>{progress.required.done}</b>/{progress.required.applicable}</span>
      <span className="chb-dot">·</span>
      <span>选问 <b>{progress.optional.done}</b>/{progress.optional.total}</span>
    </div>}
    {qualified
      ? <div className="chb-badge chb-credit" title="辅助测算结果，以行内审批为准">
          <BadgeCheck size={13}/>
          <b>¥{Number(limit).toLocaleString('zh-CN')}</b>
          <span>{run!.rating} · {Math.round(Number(run!.score))}分</span>
        </div>
      : <div className="chb-badge chb-muted"><BadgeCheck size={13}/><span>测算未完成</span></div>}
    <div className="chb-badge"><Mic size={13}/>
      <span>{interviews} 次访谈 · {recordings} 段录音</span></div>
  </div>;
}

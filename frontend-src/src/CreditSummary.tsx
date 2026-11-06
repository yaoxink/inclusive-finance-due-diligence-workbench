import {useEffect, useState} from 'react';
import {AlertTriangle, CalendarClock, Coins, Landmark, LoaderCircle, Receipt, ShieldCheck, Sprout, TrendingUp, Wheat} from 'lucide-react';
import './CreditSummary.css';

const API = '/api/due-diligence';

type Run = {state: string; stale: boolean; score: string | null; rating: string | null;
  effective_limit: string | null; current_effective_limit?: string | null; created_at: string;
  blocking_reasons?: unknown[]};
type FieldRecord = {value?: string | null; unit?: string | null};
type Workspace = {latest_run: Run | null; qualification?: {blocking_reasons?: unknown[]};
  draft: {inputs: {fields?: Record<string, FieldRecord>}};
  answers_recheck?: {pending_recheck: boolean; reason?: string}};

const INDICATORS: {key: string; label: string; icon: typeof Sprout; kind?: 'money'}[] = [
  {key: 'crop.main.area', label: '主营种植面积', icon: Sprout},
  {key: 'crop.main.yield', label: '主营亩产', icon: Wheat},
  {key: 'income.planting.annual_receipts', label: '年种植收入', icon: TrendingUp, kind: 'money'},
  {key: 'cost.total', label: '年经营总成本', icon: Receipt, kind: 'money'},
  {key: 'cashflow.quarter_interest', label: '季度存款结息', icon: Coins, kind: 'money'},
  {key: 'loan.requested_amount', label: '申请金额', icon: Landmark, kind: 'money'},
  {key: 'loan.term', label: '贷款期限', icon: CalendarClock},
];

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error('测算接口请求失败');
  return response.json() as Promise<T>;
}

const fmtMoney = (value: string | null | undefined) =>
  value == null ? '—' : Number(value).toLocaleString('zh-CN');

const LIMIT_STEPS = ['正在带入确认答案…', '正在运行评分模型…', '正在汇总生成额度…'];

export default function CreditSummary({caseId, onOpenWorkbench, generating = false, step = 0, error: generateError = '', onRetry}:
  {caseId: string; onOpenWorkbench(): void; generating?: boolean; step?: number; error?: string; onRetry?(): void}) {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [failed, setFailed] = useState(false);
  const [arcReady, setArcReady] = useState(false);

  useEffect(() => {
    let alive = true;
    setWorkspace(null); setFailed(false); setArcReady(false);
    request<Workspace>(`${API}/cases/${encodeURIComponent(caseId)}/calculation/v2`)
      .then(value => { if (alive) { setWorkspace(value); setTimeout(() => { if (alive) setArcReady(true); }, 60); } })
      .catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [caseId]);
  useEffect(() => { // the one-click chain finished (Shell-owned): refresh the result
    const refresh = () => { setArcReady(false);
      request<Workspace>(`${API}/cases/${encodeURIComponent(caseId)}/calculation/v2`)
        .then(value => { setWorkspace(value); setTimeout(() => setArcReady(true), 60); })
        .catch(() => setFailed(true)); };
    window.addEventListener('due-diligence:limit-generated', refresh);
    return () => window.removeEventListener('due-diligence:limit-generated', refresh);
  }, [caseId]);

  if (failed) return <section className="cs-summary" aria-label="授信测算摘要">
    <p className="cs-empty">测算状态暂不可读，可展开下方完整工作台重试。</p></section>;
  if (!workspace) return <section className="cs-summary" aria-label="授信测算摘要">
    <p className="cs-empty"><LoaderCircle size={16} className="dd-spin"/> 正在读取测算结果……</p></section>;

  const run = workspace.latest_run;
  const qualified = !!run && run.state === 'valid' && !run.stale;
  const limit = qualified ? run!.current_effective_limit ?? run!.effective_limit : null;
  const score = qualified ? Number(run!.score ?? 0) : 0;
  const rating = qualified ? run!.rating : null;
  const fields = workspace.draft?.inputs?.fields || {};
  const blockingCount = (run?.blocking_reasons ?? workspace.qualification?.blocking_reasons ?? []).length;

  return <section className="cs-summary" aria-label="授信测算摘要">
    {generating ? (
      <div className="cs-hero cs-generating" role="status" aria-label="正在生成额度">
        <span className="ab-limit-coin" aria-hidden="true"><i>¥</i></span>
        <div className="cs-generating-copy">
          <strong>AI 正在生成额度</strong>
          <small>{LIMIT_STEPS[step] ?? LIMIT_STEPS[LIMIT_STEPS.length-1]}</small>
          <span className="ab-limit-track" aria-hidden="true">
            {LIMIT_STEPS.map((_, index) => <i key={index} className={index <= step ? 'on' : ''}/>)}
          </span>
        </div>
      </div>
    ) : generateError ? (
      <div className="cs-hero cs-hero-blocked" role="alert">
        <AlertTriangle size={26}/>
        <div><strong>本次生成未完成</strong><p>{generateError}</p></div>
        <button onClick={onRetry}>重试生成</button>
      </div>
    ) : qualified ? (
      <div className="cs-hero">
        <div className="cs-gauge" role="img" aria-label={`模型评分 ${score} 分`}>
          <svg viewBox="0 0 120 120">
            <defs>
              <linearGradient id="csArc" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stopColor="#0c769a"/><stop offset="55%" stopColor="#27ae7c"/><stop offset="100%" stopColor="#7a5cff"/>
              </linearGradient>
            </defs>
            <circle cx="60" cy="60" r="52" className="cs-gauge-track"/>
            <circle cx="60" cy="60" r="52" className="cs-gauge-arc" stroke="url(#csArc)"
              transform="rotate(-90 60 60)"
              style={{strokeDasharray: `${arcReady ? (score / 100) * 326.7 : 0} 326.7`}}/>
          </svg>
          <div className="cs-gauge-center"><strong>{score}</strong><span>模型评分</span></div>
        </div>
        <div className="cs-limit">
          <span className="cs-limit-eyebrow">辅助测算授信额度</span>
          <strong className="cs-limit-amount"><i>¥</i>{fmtMoney(limit)}</strong>
          <div className="cs-limit-meta">
            {rating && <span className="cs-rating"><ShieldCheck size={14}/>信用等级 {rating}</span>}
            <span>授信参考 · 以行内审批为准</span>
          </div>
        </div>
      </div>
    ) : (
      <div className="cs-hero cs-hero-blocked" role="status">
        <AlertTriangle size={26}/>
        <div>
          <strong>{run ? '本次测算尚未通过' : '尚未生成测算'}</strong>
          <p>{blockingCount ? `还有 ${blockingCount} 项输入待补充或核对。` : '补齐输入并测算后，这里将展示授信额度。'}</p>
        </div>
        <button onClick={onOpenWorkbench}>打开完整工作台</button>
      </div>
    )}
    {qualified && workspace.answers_recheck?.pending_recheck && (
      <p className="cs-recheck" role="status"><AlertTriangle size={13}/>问卷答案在本次测算后有修改，请复核后重新测算。</p>
    )}
    <div className="cs-indicators">
      {INDICATORS.map(({key, label, icon: Icon, kind}) => {
        const record = fields[key];
        const value = record?.value;
        return <div className="cs-indicator" key={key}>
          <Icon size={16}/>
          <div><span>{label}</span>
            <strong>{value == null ? '—' : kind === 'money' ? `¥${fmtMoney(value)}` : value}
              {record?.unit ? <small> {record.unit}</small> : null}</strong></div>
        </div>;
      })}
    </div>
  </section>;
}

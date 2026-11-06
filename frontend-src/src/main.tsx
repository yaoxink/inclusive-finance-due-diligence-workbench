import React, {Suspense, lazy} from 'react';
import ReactDOM from 'react-dom/client';
import './style.css';
const benchmarkPage = window.location.pathname.replace(/\/$/, '') === '/asr-benchmark';
const Page = benchmarkPage ? lazy(() => import('./ASRBenchmark')) : lazy(() => import('./App'));
if (benchmarkPage) document.title = '语音模型测试台';
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><Suspense fallback={<p role="status" style={{padding:32}}>正在打开工作台…</p>}><Page/></Suspense></React.StrictMode>
);

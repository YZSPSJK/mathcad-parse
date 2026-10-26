"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, date, ListedDoc, size, stages } from "../lib/api";

export default function Home() {
  const [documents, setDocuments] = useState<ListedDoc[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [worker, setWorker] = useState("检测中");
  const input = useRef<HTMLInputElement>(null);
  const load = useCallback(async () => {
    try {
      const [docs, health] = await Promise.all([api<ListedDoc[]>("/api/documents"), api<{ worker: string }>("/api/health")]);
      setDocuments(docs); setWorker(health.worker === "online" ? "Worker 在线" : "Worker 离线");
    } catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); const timer = setInterval(() => void load(), 10000); return () => clearInterval(timer); }, [load]);
  async function upload(file?: File) {
    if (!file || uploading) return;
    if (!file.name.toLowerCase().endsWith(".mcdx")) { setError("请选择 .mcdx 工作表"); return; }
    setUploading(true); setError("");
    try {
      const body = new FormData(); body.append("file", file);
      const result = await api<{ id: string }>("/api/documents", { method: "POST", body });
      window.location.assign(`/documents/${result.id}`);
    } catch (e) { setError((e as Error).message); setUploading(false); }
  }
  return <>
    <header className="topbar"><Link className="brand" href="/"><span className="brand-mark">M</span><span>Mathcad <small>WORKBENCH</small></span></Link><span className="environment"><i />本地工作空间<span className="divider">/</span>{worker}</span></header>
    <main className="library">
      <div className="breadcrumb">工作空间 <span>/</span> 文档管理</div>
      <div className="page-heading"><div><p className="eyebrow">WORKSHEET LIBRARY</p><h1>工作表</h1><p className="muted">集中管理工程计算，让每一次参数变更都有迹可循。</p></div><div className="page-tools"><Link className="button latex-mode-link" href="/latex">LaTeX 区域解析 ↗</Link><span className="count">{documents.length} 份文档</span></div></div>
      {error && <div className="alert error" role="alert">{error}<button onClick={() => { setError(""); void load(); }}>重试</button></div>}
      <section className={`upload-zone ${dragging ? "dragging" : ""}`} aria-label="上传工作表" aria-busy={uploading} onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); void upload(event.dataTransfer.files[0]); }}>
        <div className="upload-symbol" aria-hidden="true">↑</div><div><h2>{uploading ? "正在上传并解析…" : "将 Mathcad 工作表拖到这里"}</h2><p>支持 .mcdx · 原始文件完整保留 · 上传后即可查看参数</p></div>
        <input ref={input} type="file" name="worksheet" id="worksheet-upload" accept=".mcdx" className="visually-hidden" disabled={uploading} onChange={event => void upload(event.target.files?.[0])} />
        <button className="primary" disabled={uploading} onClick={() => input.current?.click()}>＋ 选择工作表</button>
      </section>
      <div className="section-title"><h2>全部文档</h2><button className="text-button" onClick={() => void load()}>↻ 刷新列表</button></div>
      <div className="table-wrap"><table><thead><tr><th>文档名称</th><th>上传时间</th><th>解析状态</th><th>最新计算</th><th>操作</th></tr></thead><tbody>
        {documents.map(doc => <tr key={doc.id}><td><div className="file-cell"><span className="file-icon">M</span><div><strong>{doc.name}</strong><small>{size(doc.size)} · MCDX</small></div></div></td><td>{date(doc.uploaded_at)}</td><td><span className={`badge ${doc.parse_status === "parsed" ? "success" : "warning"}`}>{doc.parse_status === "parsed" ? "解析完成" : "部分解析"}</span></td><td>{doc.latest_job ? <><span className={`badge ${doc.latest_job.status === "succeeded" ? "success" : ""}`}>{stages[doc.latest_job.stage] || doc.latest_job.status}</span>{doc.latest_revision && <small>r{doc.latest_revision}</small>}</> : <span className="muted">尚未计算</span>}</td><td><div className="row-actions"><Link href={`/documents/${doc.id}`}>详情 →</Link><Link href={`/latex/documents/${doc.id}`}>LaTeX →</Link><a href={`/api/documents/${doc.id}/original`}>源文件 ↓</a></div></td></tr>)}
        {!documents.length && <tr><td colSpan={5}><div className="empty-list"><span className="empty-glyph">▤</span><h3>{loading ? "正在加载工作表…" : "从第一份工作表开始"}</h3><p>上传已有 Mathcad 文档，查看参数并创建计算版本。</p></div></td></tr>}
      </tbody></table></div>
      <footer className="library-footer"><span>源文件只读保存 · 计算版本独立存档</span><span>Mathcad Prime 12</span></footer>
    </main>
  </>;
}

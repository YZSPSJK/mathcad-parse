"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, date, ListedDoc, size, stages } from "../../lib/api";
import styles from "./latex.module.css";

export default function LatexLibrary() {
  const [documents, setDocuments] = useState<ListedDoc[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    try { setDocuments(await api<ListedDoc[]>("/api/documents")); setError(""); }
    catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  return <div className={styles.page}>
    <header className="topbar"><Link className="brand" href="/"><span className="brand-mark">M</span><span>Mathcad <small>WORKBENCH</small></span></Link><Link className="back-link" href="/">← 返回首页</Link></header>
    <main className={styles.library}>
      <div className="breadcrumb">工作空间 <span>/</span> LaTeX 区域解析</div>
      <div className={styles.hero}><div><p className="eyebrow">LATEX REGION WORKSPACE</p><h1>LaTeX 解析模式</h1><p className="muted">选择工作表，在独立页面查看区域公式、编辑 LaTeX 并运行 Mathcad 计算。</p></div><Link className="button" href="/">上传工作表 ↑</Link></div>
      {error && <div className="alert error" role="alert">{error}<button onClick={() => void load()}>重试</button></div>}
      <section className={styles.documentList} aria-label="可解析工作表">
        <div className={styles.listHeading}><h2>选择工作表</h2><button className="text-button" onClick={() => void load()}>↻ 刷新</button></div>
        {documents.map(doc => <article className={styles.documentRow} key={doc.id}>
          <span className="file-icon">M</span><div className={styles.documentInfo}><strong>{doc.name}</strong><small>{date(doc.uploaded_at)} · {size(doc.size)} · {doc.latest_revision ? `最新计算 r${doc.latest_revision}` : "尚未计算"}</small></div>
          {doc.latest_job && <span className="badge">{stages[doc.latest_job.stage] || doc.latest_job.status}</span>}
          <Link className="button" href={`/latex/documents/${doc.id}`}>打开区域解析 →</Link>
        </article>)}
        {!documents.length && <div className={styles.empty}><h2>{loading ? "正在加载工作表…" : "暂无工作表"}</h2><p>{loading ? "" : "先上传 .mcdx 文件，再返回这里打开 LaTeX 解析模式。"}</p>{!loading && <Link className="primary button" href="/">上传工作表</Link>}</div>}
      </section>
    </main>
  </div>;
}

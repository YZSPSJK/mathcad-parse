"use client";

import katex from "katex";
import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";
import LatexRegionEditor from "../../../../components/LatexRegionEditor";
import { api, Artifact, date, LatexRegion, LatexWorkspace, stages } from "../../../../lib/api";
import styles from "../../latex.module.css";

function Formula({ latex }: { latex: string }) {
  const html = useMemo(() => katex.renderToString(latex || String.raw`\text{无可显示的表达式}`, {
    displayMode: true, throwOnError: false, trust: false, strict: "warn",
  }), [latex]);
  return <div className={styles.formula} aria-label={`LaTeX 表达式 ${latex || "空"}`} dangerouslySetInnerHTML={{ __html: html }} />;
}

function shown(value: unknown) {
  if (value === null || value === undefined) return "暂无计算值";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (typeof value === "object" && "value" in value) {
    const result = (value as { value: unknown; units?: string; reason?: string }).value;
    if (result === null) return (value as { reason?: string }).reason || "暂无计算值";
    const units = (value as { units?: string }).units;
    return `${Array.isArray(result) ? result.map(row => Array.isArray(row) ? row.join(" · ") : row).join(" / ") : result}${units ? ` ${units}` : ""}`;
  }
  return JSON.stringify(value);
}

export default function LatexDocument({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [workspace, setWorkspace] = useState<LatexWorkspace | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState(false);
  const editingRef = useRef(false);
  const [saving, setSaving] = useState(false);
  const [calculating, setCalculating] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async (replace = false) => {
    const result = await api<LatexWorkspace>(`/api/documents/${id}/latex`);
    setWorkspace(result);
    setSelectedId(current => current && result.regions.some(region => region.region_id === current)
      ? current : result.regions[0]?.region_id || "");
    if (replace || !editingRef.current) {
      setEdits(Object.fromEntries(result.regions.map(region => [region.region_id, region.latex])));
      editingRef.current = false;
      setEditing(false);
    }
  }, [id]);

  useEffect(() => { void load(true).catch(e => setError((e as Error).message)); }, [load]);
  useEffect(() => {
    if (!workspace?.active_job) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { await load(); } catch (e) { if (!cancelled) setError((e as Error).message); }
      if (!cancelled) timer = setTimeout(poll, 1500);
    };
    timer = setTimeout(poll, 1000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [workspace?.active_job, load]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (editingRef.current) event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);

  const selected = workspace?.regions.find(region => region.region_id === selectedId) || null;
  const latex = selected ? (edits[selected.region_id] ?? selected.latex) : "";
  const changed = !!selected && latex !== selected.latex;
  const locked = saving || calculating || !!workspace?.active_job;
  const job = workspace?.jobs.find(item => item.id === workspace.active_job);
  const latestMcdx: Artifact | undefined = workspace?.artifacts.find(item =>
    item.kind === "mcdx" && item.revision === workspace.latest_revision);
  const mcdxCurrent = !!latestMcdx && !workspace?.latex_dirty && !editing;

  const onLatexChange = useCallback((value: string) => {
    if (!selectedId) return;
    setEdits(previous => ({ ...previous, [selectedId]: value }));
    const current = workspace?.regions.find(region => region.region_id === selectedId);
    const dirty = !!current && value !== current.latex;
    editingRef.current = dirty;
    setEditing(dirty);
    setError(""); setNotice("");
  }, [selectedId, workspace]);

  async function saveRegion() {
    if (!workspace || !selected || !changed || locked) return;
    setSaving(true); setError(""); setNotice("");
    try {
      const result = await api<{ latex_version: number }>(`/api/documents/${id}/latex/regions/${encodeURIComponent(selected.region_id)}`, {
        method: "PUT", body: JSON.stringify({ expected_version: workspace.latex_version, latex }),
      });
      editingRef.current = false; setEditing(false);
      setNotice(`区域 ${selected.region_id} 的 LaTeX 已保存为草稿 v${result.latex_version}。`);
      await load(true);
    } catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  }

  async function calculate() {
    if (!workspace || locked) return;
    if (editing) { setError("请先保存当前区域的 LaTeX，再开始计算。"); return; }
    setCalculating(true); setError(""); setNotice("");
    try {
      const result = await api<{ job_id: string; revision: number }>(`/api/documents/${id}/latex/calculate`, {
        method: "POST", body: JSON.stringify({ expected_version: workspace.latex_version }),
      });
      setNotice(`已提交 Mathcad 计算，目标版本 r${result.revision}。结果与 MCDX 生成后会自动刷新。`);
      await load();
    } catch (e) { setError((e as Error).message); }
    finally { setCalculating(false); }
  }

  if (!workspace) return <main className="library"><Link href="/latex">← 返回 LaTeX 解析</Link><p role={error ? "alert" : "status"}>{error || "正在加载工作表…"}</p><button onClick={() => void load(true).catch(e => setError((e as Error).message))}>重新加载</button></main>;

  return <div className={styles.page}>
    <header className="topbar"><Link className="brand" href="/"><span className="brand-mark">M</span><span>Mathcad <small>WORKBENCH</small></span></Link><Link className="back-link" href="/latex">← 返回工作表</Link></header>
    <div className={styles.documentHeader}>
      <div><p className="eyebrow">LATEX REGION WORKSPACE</p><h1>{workspace.name}</h1><p className="muted">{date(workspace.uploaded_at)} · {workspace.regions.length} 个工作表区域 · 草稿 v{workspace.latex_version}{workspace.latest_revision ? ` · 最新计算 r${workspace.latest_revision}` : " · 尚未计算"}</p></div>
      <div className={styles.actions}><Link className="button" href={`/documents/${id}`}>原始详情</Link><button disabled={locked || !selected?.editable || !changed} onClick={() => void saveRegion()}>{saving ? "正在保存…" : "保存 LaTeX 草稿"}</button><button className="primary" disabled={locked || editing} onClick={() => void calculate()}>{calculating || workspace.active_job ? `◌ ${stages[job?.stage || "queued"] || "计算中"}` : "▶ 调用 Mathcad 计算"}</button>{latestMcdx && mcdxCurrent && <a className="button" href={`${latestMcdx.url}?download=true`}>导出最新 MCDX ↓</a>}</div>
    </div>
    <div className={`${styles.status} ${workspace.latex_dirty || editing ? styles.warning : ""}`} role="status" aria-live="polite">
      <span>{workspace.active_job ? `${stages[job?.stage || "queued"] || "任务处理中"} · Mathcad 正在计算` : workspace.latex_dirty ? "已保存的 LaTeX 草稿尚未计算" : workspace.latest_revision ? `✓ 最新计算 r${workspace.latest_revision}` : "○ 选择一个可编辑区域开始转换和编辑"}</span>
      <span>{mcdxCurrent ? `最新 MCDX · r${latestMcdx.revision}` : workspace.latex_dirty || editing ? "草稿计算后可导出新的 MCDX" : "计算成功后可导出 MCDX"}</span>
    </div>
    {error && <div className="alert error" role="alert">{error}<button onClick={() => { editingRef.current = false; void load(true).then(() => setError("")).catch(e => setError((e as Error).message)); }}>重新载入</button></div>}
    {notice && <div className="alert" role="status">{notice}</div>}
    {workspace.jobs[0]?.error && !workspace.active_job && <div className="alert error" role="alert">{workspace.jobs[0].error.message}</div>}
    <main className={styles.workspace}>
      <section className={styles.regionPanel} aria-label="工作表区域">
        <div className={styles.panelTitle}><div><h2>区域与公式</h2><small>选择区域查看 LaTeX 表达式和结果</small></div><button className="text-button" onClick={() => void load()}>↻ 刷新</button></div>
        {!workspace.regions.length && <div className={styles.empty}><h2>没有找到可展示区域</h2><p>此 MCDX 的 worksheet.xml 中没有 region 节点。</p></div>}
        <div className={styles.regionList}>{workspace.regions.map(region => {
          const displayLatex = edits[region.region_id] ?? region.latex;
          const active = selectedId === region.region_id;
          const coords = [region.geometry.left && `左 ${region.geometry.left}`, region.geometry.top && `上 ${region.geometry.top}`].filter(Boolean).join(" · ");
          return <button type="button" key={region.region_id} className={`${styles.regionCard} ${active ? styles.active : ""}`} onClick={() => { if (editingRef.current && !active) { setError("请先保存当前区域的 LaTeX，再切换到其他区域。"); return; } setSelectedId(region.region_id); setError(""); }} aria-pressed={active}>
            <span className={styles.regionMeta}><strong>{region.display_name}</strong><small>region {region.region_id}{coords ? ` · ${coords}` : ""}</small><span className={`badge ${region.editable ? "success" : "warning"}`}>{region.editable ? "可编辑" : "只读"}</span></span>
            <Formula latex={displayLatex} />
            <span className={styles.regionBottom}>{region.fields.length ? region.fields.map(field => `${field.direction === "input" ? "输入" : "输出"} ${field.alias}`).join(" · ") : region.result_ref ? `resultRef ${region.result_ref}` : "未配置 I/O"}{region.value && <b>{shown(region.value)}</b>}</span>
            {!region.editable && <span className={styles.unsupported}>{region.reason}</span>}
          </button>;
        })}</div>
      </section>
      <aside className={styles.editorPanel}>
        <div className={styles.panelTitle}><div><h2>区域编辑器</h2><small>{selected ? `${selected.display_name} · region ${selected.region_id}` : "选择一个区域"}</small></div><span className={`badge ${selected?.editable ? "success" : "warning"}`}>{selected?.editable ? "MathLive" : "只读"}</span></div>
        {selected ? <>
          {selected.editable ? <>
            <p className={styles.editorHelp}>在编辑框修改数学表达式，公式以 LaTeX 保存；点击“保存 LaTeX 草稿”后可调用 Mathcad 计算。</p>
            <LatexRegionEditor key={selected.region_id} value={latex} disabled={locked} onChange={onLatexChange} />
            <div className={styles.previewTitle}>KaTeX 预览</div><Formula latex={latex} />
            <button className={`primary ${styles.saveButton}`} disabled={locked || !changed} onClick={() => void saveRegion()}>{saving ? "正在保存…" : changed ? "保存当前区域" : "草稿已保存"}</button>
          </> : <div className={styles.empty}><p>{selected.reason}</p></div>}
          <section className={styles.resultBox}><h3>当前区域信息</h3><dl><dt>region</dt><dd>{selected.region_id}</dd><dt>resultRef</dt><dd>{selected.result_ref || "无"}</dd><dt>Mathcad 输出</dt><dd>{shown(selected.value)}</dd></dl>{selected.fields.map(field => <div key={`${field.direction}-${field.alias}`} className={styles.fieldResult}><small>{field.direction === "input" ? "输入" : "输出"} · {field.alias}</small><strong>{shown(field.value)}</strong></div>)}</section>
          <details className={styles.source}><summary>原始区域 XML</summary><pre>{selected.expression_xml}</pre></details>
        </> : <div className={styles.empty}><p>选择左侧区域以查看公式。</p></div>}
      </aside>
    </main>
  </div>;
}

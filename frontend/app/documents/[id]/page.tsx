"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useRef, useState } from "react";
import { api, Artifact, date, Doc, Field, stages, Value } from "../../../lib/api";
import FormulaField from "../../../components/FormulaField";

const numberPattern = "[+\\-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+\\-]?[0-9]+)?";
function valid(value: string) { return new RegExp(`^${numberPattern}$`).test(value) && Number.isFinite(Number(value)); }

function NumericField({ field, value, disabled, onChange }: { field: Field; value: Value; disabled: boolean; onChange: (value: Value) => void }) {
  const [errors, setErrors] = useState<Record<string, boolean>>({});
  const editable = field.direction === "input" && field.supported;
  function input(text: string, cell: string, update: (text: string) => void) {
    const id = `${field.id}-${cell}`;
    return <input id={id} name={id} type="text" inputMode="decimal" autoComplete="off" spellCheck={false} required={editable} pattern={editable ? numberPattern : undefined} maxLength={128}
      value={text} readOnly={!editable} disabled={disabled && editable} aria-label={cell === "value" ? `${field.display_name} (${field.alias})` : `${field.alias} 第 ${cell} 单元格`}
      aria-invalid={errors[cell] || undefined} aria-describedby={errors[cell] ? `${field.id}-error` : undefined}
      onChange={event => { setErrors(prev => ({ ...prev, [cell]: false })); event.currentTarget.setCustomValidity(""); update(event.target.value); }}
      onBlur={event => { const invalid = editable && !valid(text); setErrors(prev => ({ ...prev, [cell]: invalid })); event.currentTarget.setCustomValidity(invalid ? "请输入有限实数，支持科学计数法" : ""); }} />;
  }
  return <fieldset className={`parameter ${editable ? "" : "readonly"}`}>
    <legend><span>{field.display_name || field.alias}</span><span className="value-type">{value.type === "matrix" ? `${value.rows} × ${value.cols} 矩阵` : value.type === "real" ? "实数" : "未支持"}</span></legend>
    <div className="alias">{field.alias}<span>{field.units || "无量纲"}</span></div>
    {value.type === "real" && typeof value.value === "string" ? <div className="scalar-value"><label className="visually-hidden" htmlFor={`${field.id}-value`}>{field.display_name}数值</label>{input(value.value, "value", text => onChange({ ...value, value: text }))}<span>{field.units}</span></div>
      : value.type === "matrix" && Array.isArray(value.value) ? <div className="matrix-scroll"><table className="matrix"><caption className="visually-hidden">{field.display_name}，{value.rows} 行 {value.cols} 列</caption><tbody>{value.value.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{input(cell, `${r + 1},${c + 1}`, text => { const cells = (value.value as string[][]).map(row => [...row]); cells[r][c] = text; onChange({ ...value, value: cells }); })}</td>)}</tr>)}</tbody></table></div>
      : <div className="unsupported">{value.reason || field.reason || "尚无有效结果"}</div>}
    {Object.values(errors).some(Boolean) && <p className="field-error" id={`${field.id}-error`} role="alert">请输入有限实数，支持科学计数法。</p>}
    <small className="value-origin">{field.origin}{!editable && field.direction === "input" ? " · 只读" : ""}</small>
    {(field.reason || value.error_code) && <small className="field-error">{field.reason || `Mathcad 错误码 ${value.error_code}`}</small>}
    <details><summary>源表达式与映射</summary><p>region {field.region_id} · resultRef {field.result_ref ?? "无"}</p><pre>{field.expression_xml}</pre>{value.raw_xml && <pre>{value.raw_xml}</pre>}</details>
  </fieldset>;
}

export default function Detail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [doc, setDoc] = useState<Doc | null>(null);
  const [inputs, setInputs] = useState<Record<string, Value>>({});
  const [edited, setEdited] = useState(false);
  const editedRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pdfLoading, setPdfLoading] = useState(false);
  const [pdfNonce, setPdfNonce] = useState(0);
  const [selectedPdf, setSelectedPdf] = useState<Artifact | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const load = useCallback(async (replace = false) => {
    const result = await api<Doc>(`/api/documents/${id}/form`);
    setDoc(result);
    if (replace || !editedRef.current) {
      setInputs(result.inputs); setEdited(false); editedRef.current = false;
    }
    const pdf = result.artifacts.find(a => a.kind === "pdf");
    setSelectedPdf(previous => pdf && (!previous || pdf.revision > previous.revision) ? pdf : previous);
  }, [id]);
  useEffect(() => { void load(true).catch(e => setError(e.message)); }, [load]);
  useEffect(() => { if (selectedPdf) setPdfLoading(true); }, [selectedPdf?.id]);
  useEffect(() => {
    if (!doc?.active_job) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { await load(); } catch (e) { if (!cancelled) setError((e as Error).message); }
      if (!cancelled) timer = setTimeout(poll, 1500);
    };
    timer = setTimeout(poll, 1000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [doc?.active_job, load]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (editedRef.current) event.preventDefault(); };
    window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn);
  }, []);
  async function action(kind: "save" | "calculate" | "preview" | "export") {
    if (!doc || busy || doc.active_job) return;
    if ((kind === "save" || kind === "calculate") && !form.current?.reportValidity()) return;
    if ((kind === "preview" || kind === "export") && (edited || doc.dirty)) { setError("输入已修改，请先计算，再生成最新预览或导出。"); return; }
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<{ job_id?: string; artifact?: Artifact }>(`/api/documents/${id}/${kind === "save" ? "inputs" : kind}`, {
        method: kind === "save" ? "PUT" : "POST",
        body: JSON.stringify(kind === "save" || kind === "calculate" ? { expected_version: doc.draft_version, inputs } : { expected_version: doc.draft_version, revision: doc.latest_revision }),
      });
      if (result.artifact?.kind === "pdf") { setPdfLoading(true); setSelectedPdf(result.artifact); setPdfNonce(n => n + 1); }
      if (result.artifact?.kind === "mcdx") window.location.assign(result.artifact.url);
      if (result.job_id) setNotice("任务已加入队列。导出产物生成后可点击下载。");
      if (kind === "save") setNotice("草稿已保存。计算后才能更新结果与文件。");
      editedRef.current = false; setEdited(false);
      await load(true);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  if (!doc) return <main className="library"><Link href="/">← 返回工作表</Link><p role={error ? "alert" : "status"}>{error || "正在加载工作表…"}</p><button onClick={() => void load(true).catch(e => setError(e.message))}>重新加载</button></main>;
  const locked = busy || !!doc.active_job;
  const dirty = edited || doc.dirty;
  const job = doc.jobs.find(j => j.id === doc.active_job);
  const lastJob = doc.jobs[0];
  const stalePdf = selectedPdf && (dirty || selectedPdf.revision !== doc.latest_revision);
  return <div className="detail-page">
    <header className="topbar"><Link className="brand" href="/"><span className="brand-mark">M</span><span>Mathcad <small>WORKBENCH</small></span></Link><Link className="back-link" href="/">← 返回工作表</Link></header>
    <div className="document-header"><div className="document-title"><span className="file-icon">M</span><div><h1>{doc.name}</h1><p>{date(doc.uploaded_at)}<span>·</span>{doc.latest_revision ? `计算版本 r${doc.latest_revision}` : "尚未计算"}<span>·</span>草稿 v{doc.draft_version}</p></div></div><div className="document-actions"><a className="button" href={`/api/documents/${id}/original`}>源文件 ↓</a><button disabled={locked} onClick={() => void action("preview")}>预览</button><button disabled={locked} onClick={() => void action("export")}>导出最新 MCDX ↓</button><button className="primary" type="submit" form="parameters" disabled={locked}>▶ 计算</button></div></div>
    <div className={`state-strip ${dirty ? "warning-strip" : ""}`} role="status" aria-live="polite"><span>{doc.active_job ? `◌ ${stages[job?.stage || "queued"] || job?.stage}` : dirty ? "● 输入已修改，尚未计算" : doc.latest_revision ? `✓ 已计算至 r${doc.latest_revision}` : "○ 文件中保存的值，尚未进行本系统计算"}</span><span>{dirty ? "旧输出与 PDF 可能已过期" : "原始文件完整保留"}</span></div>
    {error && <div className="alert error" role="alert">{error}<button onClick={() => { editedRef.current = false; void load(true).then(() => setError("")).catch(e => setError(e.message)); }}>重新载入</button></div>}
    {notice && <div className="alert" role="status">{notice}</div>}
    {lastJob?.error && !doc.active_job && <div className="alert error" role="alert">{lastJob.error.message}<span className="muted">（{lastJob.error.code}）</span><button disabled={locked} onClick={() => void action(lastJob.kind === "preview" ? "preview" : "calculate")}>重试{lastJob.kind === "preview" ? "预览" : "计算"}</button></div>}
    <main className="workspace"><section className="preview-panel" aria-label="PDF 预览"><div className="panel-heading"><h2>工作表预览</h2><span>{selectedPdf ? `PDF · r${selectedPdf.revision}${stalePdf ? " · 已过期" : ""}` : "PDF"}</span>{selectedPdf && <div className="preview-links"><a href={selectedPdf.url} target="_blank" rel="noreferrer">打开 ↗</a><a href={`${selectedPdf.url}?download=true`}>下载 ↓</a><button className="text-button" onClick={() => { setPdfLoading(true); setPdfNonce(n => n + 1); }}>重新加载</button></div>}</div>
      {stalePdf && <div className="preview-warning">当前展示旧版预览 r{selectedPdf?.revision}。{dirty ? "请先计算，再生成最新预览。" : "点击预览为最新版本生成 PDF。"}</div>}
      <div className="pdf-surface">{selectedPdf ? <>{pdfLoading && <div className="pdf-loading" role="status">正在加载 PDF… 若浏览器无法显示，请使用“打开”或“下载”。</div>}<iframe key={`${selectedPdf.id}-${pdfNonce}`} title={`工作表 PDF，版本 r${selectedPdf.revision}`} src={selectedPdf.url} onLoad={() => setPdfLoading(false)} onError={() => { setPdfLoading(false); setError("PDF 加载失败，请重新加载或在新窗口打开。"); }} /></> : <div className="preview-empty"><div className="paper-glyph" aria-hidden="true">M<span>PDF</span></div><h2>让工作表在这里呈现</h2><p>生成保留 Mathcad 原始版式的 PDF 预览。<br />首次生成将执行真实计算，并建立计算版本。</p><button className="primary" disabled={locked} onClick={() => void action("preview")}>{doc.active_job ? "正在处理…" : "生成预览"}</button><small>上传文件不会自动启动 Mathcad</small></div>}</div>
    </section><aside className="parameters-panel"><div className="panel-heading"><h2>参数与结果</h2><span>{doc.fields.length} 个字段</span></div><form id="parameters" ref={form} method="post" action={`/api/documents/${id}/calculate`} onSubmit={event => { event.preventDefault(); void action("calculate"); }}>
      {!doc.fields.length && <div className="no-fields"><h3>未配置输入/输出</h3><p>请在 Mathcad 中设置后重新上传。</p><p>此工作表仍可保存和生成预览。</p></div>}
      {["input", "output"].map(direction => <section key={direction} className="parameter-group"><h3><span className={`group-dot ${direction}`} />{direction === "input" ? "输入参数" : "计算输出"}<small>{direction === "input" ? "可编辑值 · 单位固定" : `只读${doc.latest_revision ? ` · r${doc.latest_revision}` : " · 文件中保存的值"}`}</small></h3>{doc.fields.filter(f => f.direction === direction).map(field => {
        const onChange = (value: Value) => { editedRef.current = true; setEdited(true); setInputs(prev => {
          const next = { ...prev, [field.id]: value };
          doc.fields.filter(other => other.direction === "input" && other.region_id === field.region_id && other.id in next).forEach(other => { next[other.id] = value; });
          return next;
        }); };
        return field.value_type === "formula" && field.supported
          ? <FormulaField key={field.id} documentId={id} field={field} value={inputs[field.id] || field.value} disabled={locked} onChange={onChange} />
          : <NumericField key={field.id} field={field} value={inputs[field.id] || field.value} disabled={locked} onChange={onChange} />;
      })}</section>)}
      {doc.fields.some(f => f.direction === "input" && f.supported) && <button className="save-draft" type="button" disabled={locked} onClick={() => void action("save")}>保存草稿</button>}
      {!!doc.diagnostics.length && <details className="diagnostics"><summary>解析诊断（{doc.diagnostics.length}）</summary>{doc.diagnostics.map((item, n) => <p key={n}>{item.part} {item.region_id}：{item.message}</p>)}</details>}
    </form><div className="version-history"><h3>任务与版本</h3>{!doc.jobs.length && <p className="muted">计算后在此查看任务记录。</p>}{doc.jobs.map(job => <div className="history-row" key={job.id}><span className={`history-dot ${job.status}`} /><div><strong>{stages[job.stage] || job.stage}</strong><small>{date(job.created_at)} · {job.kind === "preview" ? "PDF 预览" : "计算"}</small>{job.error && <p className="field-error">{job.error.message}</p>}</div></div>)}{doc.artifacts.map(artifact => <a key={artifact.id} className="artifact-link" href={`${artifact.url}?download=true`}>r{artifact.revision} · {artifact.kind.toUpperCase()} <span>下载 ↓</span></a>)}</div></aside></main>
  </div>;
}

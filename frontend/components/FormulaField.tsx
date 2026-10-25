"use client";

import { useEffect, useRef, useState } from "react";
import { api, Field, Value } from "../lib/api";

type Converted = { expression: string; xml: string; references: string[] };

export default function FormulaField({ documentId, field, value, disabled, onChange }: {
  documentId: string; field: Field; value: Value; disabled: boolean; onChange: (value: Value) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const requestVersion = useRef(0);
  const [xml, setXml] = useState(field.formula_xml || "");
  const [importXml, setImportXml] = useState("");
  const [error, setError] = useState("");
  const [importError, setImportError] = useState("");
  const [message, setMessage] = useState("");
  const [converting, setConverting] = useState(false);
  const text = typeof value.value === "string" ? value.value : "";
  const prefix = `${field.id}-formula`;
  useEffect(() => { setXml(field.formula_xml || ""); }, [field.formula_xml]);
  useEffect(() => () => { requestVersion.current += 1; }, []);

  function changed(expression: string) {
    requestVersion.current += 1;
    setError(""); setImportError(""); setMessage(""); setXml("");
    input.current?.setCustomValidity("");
    onChange({ type: "formula", value: expression, units: value.units });
  }

  async function convert(fromXml = false) {
    const version = ++requestVersion.current;
    setConverting(true);
    try {
      const result = await api<Converted>(`/api/documents/${documentId}/formulas/${field.id}/convert`, {
        method: "POST", body: JSON.stringify(fromXml ? { xml: importXml } : { expression: text }),
      });
      if (version !== requestVersion.current) return;
      setError(""); setImportError(""); input.current?.setCustomValidity("");
      setXml(result.xml);
      if (fromXml) {
        onChange({ type: "formula", value: result.expression, units: value.units });
        setMessage("已从 XML 更新公式，保存草稿或计算后生效。");
      }
    } catch (e) {
      if (version !== requestVersion.current) return;
      const description = (e as Error).message;
      if (fromXml) setImportError(description);
      else { setError(description); input.current?.setCustomValidity(description); }
    } finally {
      if (version === requestVersion.current) setConverting(false);
    }
  }

  return <fieldset className="parameter formula-parameter" disabled={disabled}>
    <legend><span>{field.display_name}</span><span className="value-type">公式</span></legend>
    <div className="alias">{field.alias}<span>由 Mathcad 计算</span></div>
    <p className="formula-help" id={`${prefix}-help`}>支持 + - * / 和括号，乘法使用 *。<br />可引用的前置变量：{field.allowed_variables.length ? field.allowed_variables.join("、") : "无"}</p>
    <label className="formula-label" htmlFor={prefix}>{field.display_name} 的右侧公式</label>
    <input ref={input} id={prefix} name={prefix} type="text" className="formula-input" required maxLength={1024}
      autoComplete="off" autoCapitalize="off" autoCorrect="off" spellCheck={false} value={text}
      aria-describedby={`${prefix}-help${error ? ` ${prefix}-error` : ""}`} aria-invalid={!!error || undefined}
      onChange={event => { setConverting(false); changed(event.target.value); }} onBlur={() => void convert()}
      onInvalid={event => { if (!event.currentTarget.value.trim()) setError("请输入公式"); }} />
    {error && <p className="field-error" id={`${prefix}-error`} role="alert">{error}</p>}
    <small className="value-origin">{field.origin}</small>
    {field.effective_value?.value !== null && field.effective_value?.value !== undefined && <p className="formula-effective">上次计算值：<strong>{typeof field.effective_value.value === "string" ? field.effective_value.value : JSON.stringify(field.effective_value.value)}</strong> {field.effective_value.units}</p>}
    <details className="formula-xml"><summary>当前公式 XML</summary>
      <button type="button" className="text-button" disabled={converting} onClick={() => void convert()}>校验并转换为 XML</button>
      <pre>{xml || "编辑后离开输入框，或点击上方按钮生成 XML。"}</pre>
    </details>
    <details className="formula-xml"><summary>从 XML 导入公式</summary>
      <label className="formula-label" htmlFor={`${prefix}-xml`}>当前变量的 define 或右侧表达式 XML</label>
      <textarea id={`${prefix}-xml`} name={`${prefix}-xml`} rows={5} maxLength={16000} value={importXml}
        spellCheck={false} aria-invalid={!!importError || undefined} aria-describedby={importError ? `${prefix}-import-error` : undefined}
        onChange={event => { requestVersion.current += 1; setConverting(false); setImportXml(event.target.value); setImportError(""); }} />
      <button type="button" disabled={converting} onClick={() => void convert(true)}>从 XML 更新公式</button>
      {importError && <p className="field-error" id={`${prefix}-import-error`} role="alert">{importError}</p>}
    </details>
    <p className="value-origin" role="status" aria-live="polite">{converting ? "正在校验公式…" : message}</p>
    <details><summary>原始表达式与映射</summary><p>region {field.region_id} · resultRef {field.result_ref ?? "无"}</p><pre>{field.expression_xml}</pre></details>
  </fieldset>;
}

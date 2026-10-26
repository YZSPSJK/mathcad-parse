"use client";

import { useEffect, useRef } from "react";
import styles from "../app/latex/latex.module.css";

type Mathfield = HTMLElement & { value: string };

export default function LatexRegionEditor({ value, disabled, onChange }: {
  value: string; disabled?: boolean; onChange: (value: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const valueRef = useRef(value);
  const onChangeRef = useRef(onChange);
  valueRef.current = value;
  onChangeRef.current = onChange;

  useEffect(() => {
    let field: Mathfield | null = null;
    let active = true;
    void import("mathlive").then(() => {
      if (!active || !host.current) return;
      field = document.createElement("math-field") as Mathfield;
      field.className = "mathlive-field";
      field.setAttribute("virtual-keyboard-mode", "manual");
      field.toggleAttribute("read-only", !!disabled);
      field.setAttribute("aria-label", "区域 LaTeX 编辑器");
      field.value = valueRef.current;
      field.addEventListener("input", () => onChangeRef.current(field?.value || ""));
      host.current.replaceChildren(field);
    });
    return () => { active = false; host.current?.replaceChildren(); };
  }, []);

  useEffect(() => {
    const field = host.current?.firstElementChild as Mathfield | null;
    if (field && field.value !== value) field.value = value;
  }, [value]);

  useEffect(() => {
    const field = host.current?.firstElementChild as Mathfield | null;
    if (field) field.toggleAttribute("read-only", !!disabled);
  }, [disabled]);

  return <div ref={host} className={styles.mathliveHost} />;
}

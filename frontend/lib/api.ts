export type Value = { type: string; value: string | string[][] | null; units: string; rows?: number; cols?: number; status?: string; reason?: string; raw_xml?: string; error_code?: number };
export type Artifact = { id: string; kind: "pdf" | "mcdx"; revision: number; url: string; size: number };
export type Job = { id: string; status: string; stage: string; kind: string; error?: { code: string; message: string }; result: Record<string, unknown>; created_at: string };
export type Field = { id: string; direction: string; alias: string; display_name: string; region_id: string; result_ref: string | null; value_type: string; units: string; supported: boolean; reason: string | null; expression_xml: string; value: Value; origin: string; allowed_variables: string[]; formula_xml: string | null; effective_value: Value | null };
export type Doc = { id: string; name: string; uploaded_at: string; size: number; sha256: string; parse_status: string; draft_version: number; latest_revision: number | null; active_job: string | null; dirty: boolean; diagnostics: { message: string; part?: string; region_id?: string }[]; fields: Field[]; inputs: Record<string, Value>; artifacts: Artifact[]; jobs: Job[]; run: { revision: number; status: string; artifact_status: string; error?: { message: string } } | null };
export type ListedDoc = Pick<Doc, "id" | "name" | "uploaded_at" | "size" | "parse_status" | "latest_revision"> & { latest_job: Job | null };

let csrf: Promise<string> | undefined;
async function token(): Promise<string> {
  if (!csrf) csrf = fetch("/api/csrf", { cache: "no-store" }).then(async response => {
    if (!response.ok) throw new Error("无法连接服务");
    return (await response.json()).token as string;
  }).catch(error => { csrf = undefined; throw error; });
  return csrf;
}

export async function api<T>(url: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.method && init.method !== "GET") headers.set("x-csrf-token", await token());
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(url, { ...init, headers, cache: "no-store" });
  const body = await response.json();
  if (!response.ok) {
    if (response.status === 403) csrf = undefined;
    throw new Error(body.error?.message || `请求失败 (${response.status})`);
  }
  return body as T;
}

export const stages: Record<string, string> = { queued: "排队中", connecting: "连接 Mathcad", setting_inputs: "设置参数", calculating: "计算中", reading_outputs: "读取结果", saving: "保存工作表", validating: "校验文件", printing: "生成 PDF", completed: "任务完成", failed: "任务失败", interrupted: "任务已中断", timeout: "任务超时" };
export function date(value: string) { return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Shanghai" }).format(new Date(value)); }
export function size(value: number) { return value > 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.ceil(value / 1024))} KB`; }

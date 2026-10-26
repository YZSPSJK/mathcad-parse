import type { Metadata } from "next";
import "mathlive/fonts.css";
import "mathlive/static.css";
import "./globals.css";
import "katex/dist/katex.min.css";

export const metadata: Metadata = { title: "Mathcad · 工作表管理", description: "本地工作表、参数计算与版本预览" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}

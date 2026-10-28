/**
 * 导出：canvas → PNG 下载。
 *
 * **与预览走同一个 canvas**（`draw.ts`）—— 这是 `docs/spec/v1.1` §3.1 的硬规定：
 * 不写"导出专用"的绘制代码，否则预览与导出迟早不一致。
 *
 * 不落库、不上传：导出就是浏览器下载（`docs/spec/v1.1` §2.5 / 裁 C）。
 */

const ILLEGAL = new Set(['\\', '/', ':', '*', '?', '"', '<', '>', '|']);

/** 文件名：`2026-09-25-建溪1.png` 之类。非法字符换成 `-`。 */
export function exportFileName(stem: string): string {
  const safe = [...stem.trim()]
    .map((ch) => (ILLEGAL.has(ch) || /\s/.test(ch) ? '-' : ch))
    .join('')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
  return `${safe || 'card'}.png`;
}

export async function downloadPng(canvas: HTMLCanvasElement, filename: string): Promise<void> {
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'));
  if (!blob) throw new Error('canvas.toBlob 返回空（画布尺寸可能为 0）');
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    // 立即 revoke 在部分浏览器上会让下载失败；等一会儿再释放。
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

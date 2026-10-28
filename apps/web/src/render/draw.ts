/**
 * 渲染器：`ScenePlan + 图片` → canvas 像素。
 *
 * **无状态**：不缓存、不记住"上次画了什么"、没有选中项。同一个 plan + 同一批
 * 图片，画出来必须逐像素相同（`docs/spec/v1.1.1` §1.1）。
 *
 * 两条硬规定（对应测试与验收）：
 *
 * 1. **缺图必须可见** —— 占位框 + 文字，不许静默留白（`docs/spec/v1.1` §2.3）；
 * 2. **文字为空只在导出时省略**，预览时画虚线框（否则编辑器里看不见那个槽位）。
 */

import type { SceneSlot } from '../shared/types';
import { assetUrlBySha, fitRect, type PlannedSlot, type ScenePlan } from './scene';

export type ImageMap = Record<string, HTMLImageElement>;

const FONT_STACK = '"Noto Serif SC", "Songti SC", "SimSun", serif';

/** 预加载场景里所有资产。失败的图**不抛**：渲染时按"缺图"处理。 */
export async function loadImages(refs: readonly string[]): Promise<ImageMap> {
  const loaded = await Promise.all(
    refs.map(
      (ref) =>
        new Promise<readonly [string, HTMLImageElement | null]>((resolve) => {
          const img = new Image();
          img.onload = () => resolve([ref, img] as const);
          img.onerror = () => resolve([ref, null] as const);
          img.src = assetUrlBySha(ref);
        }),
    ),
  );
  const out: ImageMap = {};
  for (const [ref, img] of loaded) {
    if (img) out[ref] = img;
  }
  return out;
}

export interface DrawOptions {
  /** `preview` 时空文字槽画虚线框；`export` 时省略。 */
  mode?: 'preview' | 'export';
}

export function drawScene(
  canvas: HTMLCanvasElement,
  plan: ScenePlan,
  images: ImageMap,
  options: DrawOptions = {},
): void {
  canvas.width = plan.width;
  canvas.height = plan.height;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const mode = options.mode ?? 'preview';

  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, plan.width, plan.height);
  drawBackground(ctx, plan, images);
  for (const op of plan.ops) {
    drawSlot(ctx, op, plan.dpi, images, mode);
  }
  ctx.restore();
}

function drawBackground(
  ctx: CanvasRenderingContext2D,
  plan: ScenePlan,
  images: ImageMap,
): void {
  const bg = plan.background;
  if (!bg) {
    // 契约默认纯白；先铺白，透明背景由调用方另选（M1 不做透明导出）。
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, plan.width, plan.height);
    return;
  }
  if (bg.type === 'solid') {
    ctx.fillStyle = bg.color;
    ctx.fillRect(0, 0, plan.width, plan.height);
    return;
  }
  const img = images[bg.asset];
  if (!img) {
    // 背景缺图不该把整张卡变成白板：留白 + 一条提示条。
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, plan.width, plan.height);
    ctx.fillStyle = '#b00';
    ctx.font = `12px ${FONT_STACK}`;
    ctx.fillText('背景图缺失', 8, 16);
    return;
  }
  const fitted = fitRect(
    { width: img.naturalWidth, height: img.naturalHeight },
    { width: plan.width, height: plan.height },
    bg.fit ?? 'cover',
  );
  ctx.save();
  ctx.globalAlpha = bg.opacity ?? 1;
  ctx.beginPath();
  ctx.rect(0, 0, plan.width, plan.height);
  ctx.clip();
  ctx.drawImage(img, fitted.dx, fitted.dy, fitted.width, fitted.height);
  ctx.restore();
}

function drawSlot(
  ctx: CanvasRenderingContext2D,
  op: PlannedSlot,
  dpi: number,
  images: ImageMap,
  mode: DrawMode,
): void {
  const { slot, box } = op;
  ctx.save();
  ctx.globalAlpha = slot.opacity ?? 1;

  if (slot.rotation) {
    const cx = box.left + box.width / 2;
    const cy = box.top + box.height / 2;
    ctx.translate(cx, cy);
    ctx.rotate((slot.rotation * Math.PI) / 180);
    ctx.translate(-cx, -cy);
  }

  if (slot.kind === 'text') {
    if (slot.text) {
      drawText(ctx, slot, box, dpi);
    } else if (mode === 'preview') {
      drawPlaceholder(ctx, box, '空文字槽');
    }
    ctx.restore();
    return;
  }

  const img = slot.asset ? images[slot.asset] : undefined;
  if (!img) {
    drawPlaceholder(ctx, box, slot.label ?? '缺图');
    ctx.restore();
    return;
  }

  const fitted = fitRect(
    { width: img.naturalWidth, height: img.naturalHeight },
    { width: box.width, height: box.height },
    slot.fit ?? 'contain',
  );
  ctx.save();
  ctx.beginPath();
  ctx.rect(box.left, box.top, box.width, box.height);
  ctx.clip();
  ctx.drawImage(img, box.left + fitted.dx, box.top + fitted.dy, fitted.width, fitted.height);
  ctx.restore();
  ctx.restore();
}

type DrawMode = 'preview' | 'export';

function drawPlaceholder(
  ctx: CanvasRenderingContext2D,
  box: { left: number; top: number; width: number; height: number },
  label: string,
): void {
  const pad = Math.max(2, Math.round(Math.min(box.width, box.height) * 0.02));
  ctx.save();
  ctx.strokeStyle = 'rgba(190, 60, 60, 0.55)';
  ctx.fillStyle = 'rgba(190, 60, 60, 0.06)';
  ctx.lineWidth = Math.max(1, Math.round(Math.min(box.width, box.height) * 0.006));
  ctx.setLineDash([
    Math.max(3, Math.round(box.width * 0.03)),
    Math.max(3, Math.round(box.width * 0.03)),
  ]);
  ctx.fillRect(box.left + pad, box.top + pad, box.width - pad * 2, box.height - pad * 2);
  ctx.strokeRect(box.left + pad, box.top + pad, box.width - pad * 2, box.height - pad * 2);
  ctx.setLineDash([]);
  if (box.width > 40 && box.height > 18) {
    const size = Math.max(9, Math.min(16, Math.round(box.width * 0.09)));
    ctx.font = `${size}px ${FONT_STACK}`;
    ctx.fillStyle = 'rgba(150, 40, 40, 0.85)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, box.left + box.width / 2, box.top + box.height / 2);
  }
  ctx.restore();
}

function drawText(
  ctx: CanvasRenderingContext2D,
  slot: SceneSlot,
  box: { left: number; top: number; width: number; height: number },
  dpi: number,
): void {
  const text = slot.text ?? '';
  if (!text) return;
  const sizeMm = slot.font_size_mm ?? 3;
  const sizePx = Math.max(1, Math.round((sizeMm / 25.4) * dpi));
  ctx.save();
  ctx.font = `${sizePx}px ${slot.font_family ?? FONT_STACK}`;
  ctx.fillStyle = slot.color ?? '#111111';
  ctx.textAlign = slot.align ?? 'left';
  ctx.textBaseline =
    slot.valign === 'middle' ? 'middle' : slot.valign === 'bottom' ? 'bottom' : 'top';

  if (slot.writing_mode === 'vertical') {
    drawVertical(ctx, text, slot, box, sizePx);
    ctx.restore();
    return;
  }

  const x =
    slot.align === 'center'
      ? box.left + box.width / 2
      : slot.align === 'right'
        ? box.left + box.width
        : box.left;
  const y =
    slot.valign === 'middle'
      ? box.top + box.height / 2
      : slot.valign === 'bottom'
        ? box.top + box.height
        : box.top;
  ctx.fillText(text, x, y, box.width);
  ctx.restore();
}

/** 竖排：逐字堆叠。中文竖排的最简实现 —— 不做标点挤压与行高调整。 */
function drawVertical(
  ctx: CanvasRenderingContext2D,
  text: string,
  slot: SceneSlot,
  box: { left: number; top: number; width: number; height: number },
  sizePx: number,
): void {
  const chars = [...text];
  const line = sizePx * 1.08;
  const total = chars.length * line;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  const cx =
    slot.align === 'center'
      ? box.left + box.width / 2
      : slot.align === 'right'
        ? box.left + box.width - sizePx / 2
        : box.left + sizePx / 2;
  const startY =
    slot.valign === 'middle' ? box.top + (box.height - total) / 2 : box.top;
  chars.forEach((ch, i) => {
    ctx.fillText(ch, cx, startY + i * line);
  });
}

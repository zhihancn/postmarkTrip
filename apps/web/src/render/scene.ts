/**
 * 场景图（CardScene）的类型与**布局计划** —— 纯函数，不碰 DOM。
 *
 * 这是 v1.1 渲染器的前半段（`docs/spec/v1.1.1`）：
 *
 *   scene + dpi  ──[planScene]──▶  每个槽位的像素框 + 图层顺序
 *                                      │
 *                                      ▼
 *                           [draw.ts] 真正画到 canvas
 *
 * 为什么要把"算"和"画"分开：布局（mm→px、fit、旋转）是**确定性的**，
 * 可以直接用测试钉住；canvas 绘制只能靠眼睛（浏览器里看）。把可测的那半
 * 单独成层，是"预览与导出不一致"这类 bug 的唯一便宜防线。
 *
 * ⚠️ 这里**不做**字体度量：文本的宽高只有 canvas 才能量，布局层只给框。
 */

import type { CardScene, Fit, SceneBackground, SceneSlot } from '../shared/types';

export const MM_PER_INCH = 25.4;
export const FALLBACK_DPI = 300;

/**
 * mm → px。**取整规则只在这里**。
 *
 * 先算左/上边与宽高各自的像素，再让右/下边 = 左+宽，避免"两个边各自 round"
 * 导致宽高差 1px（那会让相邻槽位出现一条缝）。
 */
export function mmToPx(mm: number, dpi: number): number {
  return Math.round((mm / MM_PER_INCH) * dpi);
}

export interface PxRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export function slotBox(slot: SceneSlot, dpi: number): PxRect {
  const left = mmToPx(slot.x, dpi);
  const top = mmToPx(slot.y, dpi);
  return {
    left,
    top,
    width: mmToPx(slot.x + slot.w, dpi) - left,
    height: mmToPx(slot.y + slot.h, dpi) - top,
  };
}

export interface FitResult {
  width: number;
  height: number;
  /** 相对槽位左上角的偏移（居中时为正） */
  dx: number;
  dy: number;
}

/**
 * 把内容框放进槽位框。与 CSS `object-fit` 同语义。
 *
 * `cover` 会**溢出**槽位 —— 调用方必须裁剪（`ctx.clip`）。这里不替它裁，
 * 因为裁剪是绘制层的决定（比如将来要画"贴纸溢出"的效果）。
 */
export function fitRect(
  content: { width: number; height: number },
  box: { width: number; height: number },
  fit: Fit = 'contain',
): FitResult {
  const cw = Math.max(content.width, 0);
  const ch = Math.max(content.height, 0);
  if (cw === 0 || ch === 0 || box.width <= 0 || box.height <= 0) {
    return { width: box.width, height: box.height, dx: 0, dy: 0 };
  }
  if (fit === 'fill') {
    return { width: box.width, height: box.height, dx: 0, dy: 0 };
  }
  if (fit === 'none') {
    return {
      width: cw,
      height: ch,
      dx: (box.width - cw) / 2,
      dy: (box.height - ch) / 2,
    };
  }
  const scale =
    fit === 'cover'
      ? Math.max(box.width / cw, box.height / ch)
      : Math.min(box.width / cw, box.height / ch);
  const width = cw * scale;
  const height = ch * scale;
  return {
    width,
    height,
    dx: (box.width - width) / 2,
    dy: (box.height - height) / 2,
  };
}

export interface PlannedSlot {
  slot: SceneSlot;
  box: PxRect;
  z: number;
}

export interface ScenePlan {
  width: number;
  height: number;
  dpi: number;
  background: SceneBackground | null;
  /** 已按 z 升序排好（渲染顺序 = 数组顺序）。 */
  ops: PlannedSlot[];
}

/**
 * 把场景算成"画布尺寸 + 每个槽位的像素框"。
 *
 * 排序规则：`z` 升序；同 z 保持数组顺序（JS 的 sort 自 ES2019 起是稳定的）。
 * 契约写明"渲染顺序由 z 决定，与数组顺序无关"—— 这里的稳定排序只是让
 * 同 z 的槽位有可预期的顺序，而不是让数组顺序参与决策。
 */
export function planScene(scene: CardScene, dpi?: number): ScenePlan {
  const target = dpi ?? scene.canvas.dpi ?? FALLBACK_DPI;
  const ops: PlannedSlot[] = scene.slots.map((slot) => ({
    slot,
    box: slotBox(slot, target),
    z: slot.z ?? 0,
  }));
  ops.sort((a, b) => a.z - b.z);
  return {
    width: Math.max(mmToPx(scene.canvas.width_mm, target), 1),
    height: Math.max(mmToPx(scene.canvas.height_mm, target), 1),
    dpi: target,
    background: scene.background ?? null,
    ops,
  };
}

/** 场景里出现过的资产引用（去重，用于预加载）。 */
export function assetRefs(scene: CardScene): string[] {
  const out = new Set<string>();
  if (scene.background?.type === 'image' && scene.background.asset) {
    out.add(scene.background.asset);
  }
  for (const slot of scene.slots) {
    if (slot.asset) out.add(slot.asset);
    if (slot.kind === 'postmark' && slot.source === 'generated' && slot.template_asset) {
      out.add(slot.template_asset);
    }
  }
  return [...out];
}

/** 缺图的槽位（渲染器要画占位；界面上要能数出来）。 */
export function missingSlots(scene: CardScene): SceneSlot[] {
  return scene.slots.filter(
    (s) => (s.kind === 'image' || s.kind === 'postmark' || s.kind === 'stamp') && !s.asset,
  );
}

/** `sha256:<hex>` → 取图 URL（与后端 `GET /api/assets/by-sha256/{sha}/raw` 对应）。 */
export function assetUrlBySha(ref: string): string {
  const sha = ref.startsWith('sha256:') ? ref.slice('sha256:'.length) : ref;
  return `/api/assets/by-sha256/${sha}/raw`;
}

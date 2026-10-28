/**
 * 卡片详情 →「电子卡」面板（v1.1 M1）。
 *
 * 只做三件事（`docs/spec/v1.1` §17 M1）：
 *   1. 用内置模板把这张卡渲染出来（套模板 = 离散操作，dry-run 先看）；
 *   2. 导出 PNG（与预览同一个 canvas）；
 *   3. 在整卡图上**人工框一次**邮票 → `stamp_cut`（不是识别）。
 *
 * **没有编辑器**：拖拽/撤销/存模板是 M2。这里的「换模板」只是把模板场景
 * 复制一份、按 `kind + 序号` 绑定（后端做），预览满意才写库。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../../shared/api';
import { ErrorState, LoadingState } from '../../shared/components/States';
import { useToast } from '../../shared/components/Toast';
import type { CardRead, CardScene, TemplateRead } from '../../shared/types';
import { drawScene, loadImages } from '../../render/draw';
import { downloadPng, exportFileName } from '../../render/export';
import { assetRefs, missingSlots, planScene } from '../../render/scene';

const DPI_CHOICES = [150, 300, 600] as const;

interface Props {
  card: CardRead;
  onChanged: () => void;
}

export function ECardPanel({ card, onChanged }: Props) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  const [dpi, setDpi] = useState<number>(300);
  const [candidate, setCandidate] = useState<CardScene | null>(null);
  const [candidateTemplate, setCandidateTemplate] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState<string>('');
  const [boxOpen, setBoxOpen] = useState(false);
  const [rendering, setRendering] = useState(false);

  const sceneQuery = useQuery({
    queryKey: ['card-scene', card.id],
    queryFn: () => api.ecard.getScene(card.id),
    retry: false,
  });
  const templatesQuery = useQuery({
    queryKey: ['templates'],
    queryFn: () => api.ecard.templates(),
    retry: false,
  });

  const saved = sceneQuery.data?.scene ?? null;
  const shown = candidate ?? saved;
  const templates = useMemo(
    () => (templatesQuery.data ?? []).filter((t) => t.scope === 'builtin' || t.scope === 'user'),
    [templatesQuery.data],
  );

  // ---------------------------------------------------------------- 渲染

  useEffect(() => {
    if (!shown || !canvasRef.current) return;
    let cancelled = false;
    setRendering(true);
    const plan = planScene(shown, dpi);
    void loadImages(assetRefs(shown))
      .then((images) => {
        if (cancelled || !canvasRef.current) return;
        drawScene(canvasRef.current, plan, images, { mode: 'preview' });
      })
      .finally(() => {
        if (!cancelled) setRendering(false);
      });
    return () => {
      cancelled = true;
    };
  }, [shown, dpi]);

  // ---------------------------------------------------------------- 操作

  const applyMutation = useMutation({
    mutationFn: async (p: { templateId: string; dryRun: boolean }) =>
      api.ecard.applyTemplate(card.id, p.templateId, p.dryRun),
    onSuccess: (data, p) => {
      if (p.dryRun) {
        setCandidate(data.scene);
        setCandidateTemplate(p.templateId);
        setWarnings(data.warnings);
        if (data.warnings.length) toast.info(data.warnings.join('；'));
      } else {
        setCandidate(null);
        setCandidateTemplate(null);
        setWarnings([]);
        void queryClient.invalidateQueries({ queryKey: ['card-scene', card.id] });
        toast.success('电子卡已保存');
      }
    },
    onError: (err) => toast.error(toApiError(err).message),
  });

  const stampCutMutation = useMutation({
    mutationFn: (box: [number, number, number, number]) => api.ecard.createStampCut(card.id, box),
    onSuccess: async (data) => {
      setBoxOpen(false);
      setWarnings([]);
      toast.success(
        data.stamp_created
          ? `已框出邮票（${data.width}×${data.height}）并新建了一条邮票记录`
          : `已框出邮票（${data.width}×${data.height}）`,
      );
      onChanged();
      // 电子卡已经在的话，把新贴片重新绑进场景（M1 没有编辑器，重新套模板是安全的；
      // M2 有了局部编辑之后应改成"只更新那个槽位"）。
      const templateId = saved?.template_id;
      if (templateId) {
        try {
          await api.ecard.applyTemplate(card.id, templateId, false);
        } catch {
          // 重新绑定失败不影响框票本身；用户仍可手动「预览换模板 → 保存」。
        }
      }
      void queryClient.invalidateQueries({ queryKey: ['card-scene', card.id] });
    },
    onError: (err) => toast.error(toApiError(err).message),
  });

  const onExport = useCallback(async () => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const stamp = card.stamps[0]?.catalog_ref ?? '';
    const stem = [card.visit_date, stamp || card.title || card.id.slice(0, 8)].join('-');
    try {
      await downloadPng(canvas, exportFileName(stem));
      toast.success('已导出 PNG（与预览同一份渲染）');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '导出失败');
    }
  }, [card.id, card.stamps, card.title, card.visit_date, toast]);

  if (sceneQuery.isLoading) return <LoadingState text="读取电子卡…" />;
  if (sceneQuery.error) return <ErrorState error={sceneQuery.error} />;

  const missing = shown ? missingSlots(shown) : [];

  return (
    <section className="panel" id="ecard">
      <div className="panel__head">
        <div className="panel__title">电子卡</div>
        <div className="small muted">
          按模板把贴片摆成一张卡。渲染是只读的 —— 只有「保存」会改动这张卡
        </div>
        <div className="right">
          <button type="button" className="btn btn--sm" onClick={() => setBoxOpen(true)}>
            框出邮票
          </button>
        </div>
      </div>

      <div className="panel__body ecard">
        {templatesQuery.isLoading ? <LoadingState text="读取模板…" /> : null}

        {!shown ? (
          <div className="ecard__empty">
            <div className="small muted" style={{ marginBottom: 8 }}>
              这张卡还没有电子卡。选一个模板开始（内置模板随仓库分发，没有资产绑定）。
            </div>
            <div className="ecard__templates">
              {templates.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  className={`btn ${selectedTemplate === t.id ? 'btn--primary' : ''}`}
                  onClick={() => {
                    setSelectedTemplate(t.id);
                    applyMutation.mutate({ templateId: t.id, dryRun: true });
                  }}
                  disabled={applyMutation.isPending}
                >
                  {t.name}
                  <span className="small muted">
                    {' '}
                    · {canvasLabel(t)} · {t.slot_count} 槽
                  </span>
                </button>
              ))}
              {templates.length === 0 ? (
                <span className="small faint">
                  没有可用模板。检查 `public-data/templates/` 是否随仓库存在。
                </span>
              ) : null}
            </div>
          </div>
        ) : (
          <div className="ecard__layout">
            <div className="ecard__canvas-wrap">
              <canvas ref={canvasRef} className="ecard__canvas" />
              {rendering ? <div className="ecard__busy small muted">渲染中…</div> : null}
            </div>

            <div className="ecard__side">
              <div className="field">
                <label className="field__label" htmlFor="ecard-template">
                  模板
                </label>
                <select
                  id="ecard-template"
                  className="input"
                  value={selectedTemplate || shown.template_id || ''}
                  onChange={(e) => setSelectedTemplate(e.target.value)}
                >
                  <option value="">（不换）</option>
                  {templates.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </div>

              <div className="toolbar__row">
                <button
                  type="button"
                  className="btn"
                  disabled={!selectedTemplate || applyMutation.isPending}
                  onClick={() => {
                    if (!selectedTemplate) return;
                    applyMutation.mutate({ templateId: selectedTemplate, dryRun: true });
                  }}
                >
                  预览换模板
                </button>
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={!candidate || !candidateTemplate || applyMutation.isPending}
                  onClick={() => {
                    if (!candidateTemplate) return;
                    applyMutation.mutate({ templateId: candidateTemplate, dryRun: false });
                  }}
                >
                  保存为这张卡的电子卡
                </button>
                {candidate ? (
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => {
                      setCandidate(null);
                      setCandidateTemplate(null);
                      setWarnings([]);
                    }}
                  >
                    取消预览
                  </button>
                ) : null}
              </div>

              <div className="toolbar__row">
                <label className="field__label" htmlFor="ecard-dpi">
                  导出
                </label>
                <select
                  id="ecard-dpi"
                  className="input"
                  value={dpi}
                  onChange={(e) => setDpi(Number(e.target.value))}
                >
                  {DPI_CHOICES.map((v) => (
                    <option key={v} value={v}>
                      {v} dpi
                    </option>
                  ))}
                </select>
                <button type="button" className="btn" onClick={() => void onExport()}>
                  导出 PNG
                </button>
              </div>

              <div className="small muted">
                {shown.canvas.width_mm}×{shown.canvas.height_mm} mm · 出图{' '}
                {planScene(shown, dpi).width}×{planScene(shown, dpi).height} px
              </div>

              {missing.length ? (
                <div className="small" style={{ color: '#a33' }}>
                  有 {missing.length} 个槽位没有图，画面上是红色占位框（不会静默留白）
                </div>
              ) : null}
              {warnings.length ? (
                <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                  {warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              ) : null}
              {!card.stamps.some((s) => s.image_asset_id) ? (
                <div className="small faint">
                  这张卡还没有票贴片 —— 点右上「框出邮票」在整卡图上框一次。
                </div>
              ) : null}
            </div>
          </div>
        )}
      </div>

      {boxOpen && card.image_asset_id ? (
        <BoxSelector
          imageUrl={`/api/assets/${card.image_asset_id}/raw`}
          busy={stampCutMutation.isPending}
          onCancel={() => setBoxOpen(false)}
          onConfirm={(box) => stampCutMutation.mutate(box)}
        />
      ) : null}
    </section>
  );
}

function canvasLabel(t: TemplateRead): string {
  const w = Number(t.canvas?.width_mm ?? 0);
  const h = Number(t.canvas?.height_mm ?? 0);
  return w && h ? `${w}×${h} mm` : '模板';
}

// --------------------------------------------------------------------------- //
//  框票：在整卡图上拖一个矩形（归一化坐标交给服务端）
// --------------------------------------------------------------------------- //

interface BoxProps {
  imageUrl: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: (box: [number, number, number, number]) => void;
}

function BoxSelector({ imageUrl, busy, onCancel, onConfirm }: BoxProps) {
  const [rect, setRect] = useState<{ x: number; y: number; w: number; h: number } | null>(null);
  const [start, setStart] = useState<{ x: number; y: number } | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  //: 图片**实际渲染尺寸**。包装元素的宽度不能靠 inline-block 自适应：
  //: 图片被 `max-height: 62vh` 收窄时，包装元素仍会按 max-content 撑满可用宽度，
  //: 于是"除以包装宽度"会把 x 压扁 —— 2026-10-21 实测：拖整枚邮票只抠到左半（473×735）。
  const [imgSize, setImgSize] = useState<{ w: number; h: number } | null>(null);

  const syncSize = useCallback(() => {
    const img = imgRef.current;
    if (!img) return;
    if (img.clientWidth > 0 && img.clientHeight > 0) {
      setImgSize({ w: img.clientWidth, h: img.clientHeight });
    }
  }, []);

  useEffect(() => {
    const img = imgRef.current;
    if (!img) return;
    syncSize();
    const ro = new ResizeObserver(syncSize);
    ro.observe(img);
    return () => ro.disconnect();
  }, [syncSize]);

  const rel = (e: ReactPointerEvent<HTMLDivElement>): { x: number; y: number } => {
    // 一律以 **img** 的 rect 为准（不是包装元素）
    const el = imgRef.current ?? e.currentTarget;
    const r = el.getBoundingClientRect();
    return {
      x: clamp((e.clientX - r.left) / r.width),
      y: clamp((e.clientY - r.top) / r.height),
    };
  };

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 760 }}>
        <div className="modal__head">
          <div className="modal__title">框出邮票（人工，不走识别）</div>
        </div>
        <div className="modal__body">
          <div className="small muted" style={{ marginBottom: 8 }}>
            沿邮票齿孔外沿拖一个矩形；框一次即可，之后可从票库复用。
          </div>
          <div
            className="boxsel"
            style={imgSize ? { width: imgSize.w, height: imgSize.h } : undefined}
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId);
              const p = rel(e);
              setStart(p);
              setRect({ x: p.x, y: p.y, w: 0, h: 0 });
            }}
            onPointerMove={(e) => {
              if (!start) return;
              const p = rel(e);
              setRect({
                x: Math.min(start.x, p.x),
                y: Math.min(start.y, p.y),
                w: Math.abs(p.x - start.x),
                h: Math.abs(p.y - start.y),
              });
            }}
            onPointerUp={() => setStart(null)}
          >
            <img ref={imgRef} src={imageUrl} alt="整卡图" draggable={false} onLoad={syncSize} />
            {rect && rect.w > 0 && rect.h > 0 ? (
              <div
                className="boxsel__rect"
                style={{
                  left: `${rect.x * 100}%`,
                  top: `${rect.y * 100}%`,
                  width: `${rect.w * 100}%`,
                  height: `${rect.h * 100}%`,
                }}
              />
            ) : null}
          </div>
        </div>
        <div className="modal__foot">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy || !rect || rect.w < 0.01 || rect.h < 0.01}
            onClick={() => {
              if (!rect) return;
              onConfirm([rect.x, rect.y, rect.w, rect.h]);
            }}
          >
            {busy ? '裁图中…' : '用这个框'}
          </button>
        </div>
      </div>
    </div>
  );
}

function clamp(v: number): number {
  return Math.min(1, Math.max(0, v));
}

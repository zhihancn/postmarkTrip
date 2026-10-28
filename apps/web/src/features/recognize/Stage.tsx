import { useEffect, useMemo, useRef, useState } from 'react';

import { assetPreviewUrl } from '../../shared/api';
import { Alert } from '../../shared/components/States';
import type { Decision, RecognizedPageRead } from '../../shared/types';
import { worstDecision } from './recognize';

// --------------------------------------------------------------------------- //
//  颜色（**只按 decision**，不按 confidence）
// --------------------------------------------------------------------------- //

/** 实测「福建南火」置信度 0.90 却是错的 —— 按置信度上色会把错值显示成可信。 */
const BY_DECISION: Record<Decision, string> = {
  auto_accept: 'var(--success)',
  needs_review: 'var(--warn)',
  reject: 'var(--danger)',
};

const CIRCLE_COLOR = 'var(--primary)';

function decisionColor(decision: Decision): string {
  return BY_DECISION[decision];
}

/**
 * 一张卡的外框：**四边形**（不是 bbox）。
 *
 * 卡在扫描时可能倾斜（实测 `更多样本/005.jpg` 底行就有），轴对齐的框会与
 * 实际边界差很多 —— 而"切得准不准"正是最难从字段值看出来的失效模式。
 */
function quadPoints(card: RecognizedPageRead['cards'][number]): string {
  if (card.quad.length === 4) {
    return card.quad.map(([x, y]) => `${x},${y}`).join(' ');
  }
  const [x, y, w, h] = card.bbox;
  return `${x},${y} ${x + w},${y} ${x + w},${y + h} ${x},${y + h}`;
}

/**
 * 页面上"最坏决策"的分布 —— 给图例上的数字用。
 *
 * 为什么图例要有数字：折叠成摘要之后，图上如果只有颜色没有计数，
 * "这一页到底几张要我看"就得自己数。数字是免费的。
 */
function useCounts(page: RecognizedPageRead | null): Record<Decision, number> {
  return useMemo(() => {
    const counts: Record<Decision, number> = { auto_accept: 0, needs_review: 0, reject: 0 };
    if (page === null) return counts;
    for (const card of page.cards) {
      counts[worstDecision(card)] += 1;
    }
    return counts;
  }, [page]);
}

interface StageProps {
  page: RecognizedPageRead;
  assetId: string;
  chartWidth: number;
  onChartWidth: (width: number) => void;
}

/**
 * 左栏：整页预览 + **叠加**。
 *
 * ## 坐标系（这里最容易错，而且错了不报错）
 *
 * 识别返回的 `quad` / `circle` 都在**工作尺度**的坐标里，而工作尺度就是原图
 * 按比例缩放。所以叠层的 `viewBox` 就是 `0 0 work_size`，图是等比缩放的预览图
 * —— 它与预览图实际有多少像素无关，只要比例不变就严丝合缝。
 *
 * 第一版原型在这个地方栽过：`verdict.circle` 是**卡局部**坐标
 * （`classify_postmark` 跑在正射图上），画到整页上之后 7 张卡的圆全部重叠在
 * 左上角。那个错不报错，只是看起来"圆检测很乱"，很容易被误判成检测器不好。
 * 修法是**在管线里**把 `circle_page` 填好（不是放在 API 路由里），
 * 所以这里拿到的就是页面坐标。
 *
 * ## 为什么把圆画出来
 *
 * "圆定位对不对"是最难从字段值看出来的失效模式：实测有一张卡的圆落在了
 * 明信片印刷抬头「中国邮政 CHINA POST 邮戳卡」上，字段全错但置信度不低。
 * `docs/frontDemo/_check/p005_overlay.jpg` 显示更糟的一种：圆抓到了**邮票上
 * 的圆形地球图案**，连"只有邮票、没有邮戳"的那张卡都报了一个圆。
 */
export function Stage({ page, assetId, chartWidth, onChartWidth }: StageProps) {
  const [width, setWidth] = useState(1100);
  const wrapRef = useRef<HTMLDivElement>(null);
  const counts = useCounts(page);

  // 预览图长边 1600px（后端 `get_thumb` 的上限）—— 等比缩放，所以叠层不受影响
  const src = assetPreviewUrl(assetId, 1600);
  const [workW, workH] = page.work_size;

  // 量一次容器的宽度：叠层的线宽要按**显示宽度**折算，否则在不同屏幕上一个
  // 1.3px 的细线要么看不见、要么糊成一片
  useEffect(() => {
    const node = wrapRef.current;
    if (node === null) return;
    const measure = () => setWidth(node.clientWidth || 1100);
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const scale = workW > 0 ? width / workW : 1;
  // 线宽要按**显示宽度**折算：同样的 stroke-width 在 400px 和 2000px 宽的预览上
  // 相差 5 倍。取"屏幕上约 2px"作为目标，再夹到一个下界（太细看不见）。
  const stroke = Math.max(1.2, 2 / Math.max(scale, 0.01));
  // 白描边打底 + 彩色描边：深色垫底的单卡样本上，纯彩色描边会看不清
  const halo = stroke * 2.4;
  const fontSize = Math.max(stroke * 9, 40);

  return (
    <div className="confirm-stage">
      <div className="confirm-stage__head">
        <span className="small muted">
          {page.card_count} 张卡 · {workW}×{workH} px（工作尺度）
        </span>
        <span className="confirm-stage__zoom">
          <label className="small muted" htmlFor="stage-width">
            预览宽
          </label>
          <input
            id="stage-width"
            type="range"
            min={480}
            max={2200}
            step={40}
            value={chartWidth}
            onChange={(event) => onChartWidth(Number(event.target.value))}
          />
        </span>
      </div>

      <div className="stage-scroll" ref={wrapRef}>
        <div className="stage-frame" style={{ width: chartWidth > 0 ? chartWidth : undefined }}>
          <img className="stage-frame__img" src={src} alt="扫描件预览（工作尺度）" />
          <svg
            className="stage-frame__svg"
            viewBox={`0 0 ${workW} ${workH}`}
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            {page.cards.map((card) => {
              const color = decisionColor(worstDecision(card));
              const points = quadPoints(card);
              return (
                <g key={card.index}>
                  <polygon points={points} fill="none" stroke="#fff" strokeWidth={halo} />
                  <polygon points={points} fill="none" stroke={color} strokeWidth={stroke} />
                  {card.circle ? (
                    <>
                      <circle
                        cx={card.circle.cx}
                        cy={card.circle.cy}
                        r={card.circle.r}
                        fill="none"
                        stroke="#fff"
                        strokeWidth={halo / 2}
                      />
                      <circle
                        cx={card.circle.cx}
                        cy={card.circle.cy}
                        r={card.circle.r}
                        fill="none"
                        stroke={CIRCLE_COLOR}
                        strokeWidth={stroke}
                        strokeDasharray={`${stroke * 4} ${stroke * 3}`}
                      />
                    </>
                  ) : null}
                  <text
                    x={card.bbox[0] + stroke * 3}
                    y={card.bbox[1] + stroke * 12}
                    fill={color}
                    stroke="#fff"
                    strokeWidth={stroke * 3}
                    paintOrder="stroke"
                    fontSize={fontSize}
                    fontWeight={700}
                    fontFamily="monospace"
                  >
                    {card.index}
                  </text>
                </g>
              );
            })}
          </svg>
        </div>
      </div>

      <div className="confirm-legend">
        <span>
          <i style={{ background: BY_DECISION.auto_accept }} />
          可自动采纳 <b>{counts.auto_accept}</b>
        </span>
        <span>
          <i style={{ background: BY_DECISION.needs_review }} />
          待确认 <b>{counts.needs_review}</b>
        </span>
        <span>
          <i style={{ background: BY_DECISION.reject }} />
          未采信 <b>{counts.reject}</b>
        </span>
        <span>
          <i className="confirm-legend__circle" style={{ borderColor: CIRCLE_COLOR }} />
          邮戳圆
        </span>
      </div>

      {page.stage_notes.length > 0 ? (
        <div className="confirm-notes">
          {page.stage_notes.map((note) => (
            <div key={note}>· {note}</div>
          ))}
        </div>
      ) : null}

      {page.used_vlm ? (
        <Alert kind="warn" title="这一页走了云端">
          云端结果**不可复现**（模型随时会变），也不能离线跑，而且是按次计费的。
          所以每张卡上会标一个「云端」。
        </Alert>
      ) : null}
    </div>
  );
}

import { useState } from 'react';

import { assetCardCropUrl, assetRawUrl } from '../../shared/api';
import { Modal } from '../../shared/components/Modal';
import type { RecognizedCardRead } from '../../shared/types';
import { zoomViewFor, type PageView } from './recognize';

interface CardZoomProps {
  page: PageView;
  card: RecognizedCardRead;
  onClose: () => void;
}

/** 放大视图的宽度（CSS 像素）—— 720 在笔记本屏上够看，也不至于把弹层撑满 */
const VIEW_W = 720;

/**
 * 放大看一张卡：**整卡**与**邮戳**两个视图（`ok/15` q1）。
 *
 * ## 为什么不做成"后端把抠图返给前端"
 *
 * `pipeline` 里其实**已经算了**两张抠图（戳 + 票），但它们的像素**不进响应体**：
 * 一页 9 张的 base64 就是几 MB 的 JSON，而 `PageResult.to_dict()` 的注释里
 * 写着同一个理由（"把 base64 像素塞进 observation 会让表迅速膨胀"）。
 *
 * 要"放大看清戳面小字"，**原图裁切就够了**：`/api/assets/{id}/raw` 是原始件，
 * 而响应里的 `bbox` / `circle` 都是 work 尺度坐标 —— 把 work 尺度换算成
 * 原图像素（`× 1/work_scale`）就能裁出**原始分辨率**的那一块。
 * 所以这里是**零新端点、零新增载荷**；浏览器自己缩放，还能顺手做"到原始
 * 分辨率为止"的封顶（再放大只是插值，没有新信息）。
 *
 * 抠图像素真正该去的地方是**画廊叠层**（需要 alpha 通道）—— 那应该在
 * **确认时**把抠图落成 asset 并挂到 `Postmark.image_asset_id`，不是塞进识别响应。
 */
export function CardZoom({ page, card, onClose }: CardZoomProps) {
  //: 默认「整卡」（用户 2026-10-12：点进来先看到戳面，还得再点一下才看得到整卡，
  //: 而"这张卡有没有切对"是更常见的第一个问题）。戳面视图仍在，一键切换。
  const [mode, setMode] = useState<'card' | 'postmark'>('card');
  const [workW, workH] = page.work_size;

  //: 裁哪一块、放大多少倍 —— 纯函数，测试在 `tests/recognize.test.mjs`
  const view = zoomViewFor(card, { mode, viewWidth: VIEW_W, workScale: page.work_scale });
  //: 「整卡」走服务端的透视校正裁图（无黑边）；几何缺失时才回落到 CSS 直角裁切
  const cropUrl =
    card.quad.length === 4 && page.work_scale > 0
      ? assetCardCropUrl(page.assetId, card.quad, page.work_scale)
      : '';
  const k = view ? view.scale : 1;
  const style = view
    ? {
        width: `${Math.round(view.box[2] * k)}px`,
        height: `${Math.round(view.box[3] * k)}px`,
        backgroundImage: `url('${assetRawUrl(page.assetId)}')`,
        backgroundSize: `${workW * k}px ${workH * k}px`,
        backgroundPosition: `${-view.box[0] * k}px ${-view.box[1] * k}px`,
      }
    : undefined;

  return (
    <Modal open title={`第 ${card.index + 1} 张 · 放大`} onClose={onClose} size="wide">
      <div className="card-zoom">
        <div className="card-zoom__tabs">
          <button
            type="button"
            className={`btn btn--sm${mode === 'postmark' ? ' btn--primary' : ''}`}
            onClick={() => setMode('postmark')}
            disabled={card.circle === null}
            title={card.circle === null ? '这一张没定位到邮戳圆' : '裁邮戳那一块（原始分辨率）'}
          >
            邮戳
          </button>
          <button
            type="button"
            className={`btn btn--sm${mode === 'card' ? ' btn--primary' : ''}`}
            onClick={() => setMode('card')}
            title="裁整张卡"
          >
            整卡
          </button>
        </div>

        {mode === 'card' && cropUrl !== '' ? (
          //: 「整卡」= **服务端透视校正**过的这一张（`ok/19` §三 3.2 路径 C）：
          //: 输出框就是卡自身，所以不再有黑边；也不再需要前端算几何。
          <img className="card-zoom__view" src={cropUrl} alt={`第 ${card.index + 1} 张`} />
        ) : view ? (
          <div className="card-zoom__view" style={style} />
        ) : (
          <div className="state-box small muted">这一张没定位到邮戳圆，看「整卡」吧。</div>
        )}

        <div className="card-zoom__meta small faint">
          卡尺寸 {card.card_size[0]}×{card.card_size[1]} px（work 尺度，
          {page.work_scale > 0 ? ` 原图约 ${Math.round(workW / page.work_scale)}px 长边` : ''}）
          {card.circle ? (
            <>
              {' · '}戳半径/卡宽 {card.circle.radius_over_card_width.toFixed(3)}
              （正常约 0.23；明显更大说明圆可能抓错了东西）
            </>
          ) : null}
          <br />
          显示倍率 ×{k.toFixed(2)}
          {view?.capped ? '（**已到原始分辨率**，再放大只是插值，没有新信息）' : ''}
          {' · '}来源：原始件裁切，非识别产物
        </div>
      </div>
    </Modal>
  );
}

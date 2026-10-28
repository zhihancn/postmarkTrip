"""确认端点：把**人工确认过**的识别结果落库。

## 为什么它必须是独立的端点

spec A6：**所有自动结果无一例外经过人工确认才落库**。
所以职责被切成两半，而且这个切分是**结构上**的：

| 端点 | 能不能写库 |
| --- | --- |
| `POST /api/recognize` | **只读**（有测试断言它没有任何写操作） |
| `POST /api/cards/confirm` | **本端点**，唯一的写入点 |

## 落库的三层（L0/L2/L3）

* **L0** 原始件 —— 上传时由 `/api/assets` 留下（`Card.image_asset_id` 指回它）
* **L2** `observation` —— **机器说的是什么**。每次识别插一批新行，
  **从不 UPDATE**。这样模型升级后可以对同一张图重跑，新旧结果并存
  （spec A7），人可以对比后择优采纳
* **L3** `postmark` —— **这枚戳是什么**（2026-10-11 补，`docs/ok/16` §五）。
  此前本端点**不建戳**，于是"戳"不是实体：卡片页永远显示「（缺戳面原文）」、
  卡列表摘要为空。装配规则在 `domain/postmark_assembly.py`（含"局名只在
  place 子树内匹配"这条，见那里的说明）
* **L3** `correction` —— **人把机器值改成了什么**。
  **只在真的改了的时候写**。它是本项目长期最有价值的标注资产
  （`db/models.py` 的原话："一等公民，不是用完即弃的日志"）

## 为什么 `source` / `status` / `visit_date` 必须由请求带

它们是 `Card` 上 **NOT NULL** 的列，而且**来自人的选择**、不是识别输出：

    visit_date  ← 跑戳日（可用识别日期**预填**，但它是人的确认值）
    source      ← 来源：field_trip / mail_request / ...
    status      ← 状态：collected / pending / wishlist

所以本端点**拒绝**在 `visit_date` 缺失时替用户编一个日期 —— 那会污染
`ix_card_visit_date_status` 这个索引背后的统计。

## ⚠️ 一个真实的设计弱点：机器值是**客户端回传**的

请求里同时带"机器值"与"最终值"。机器值本该是服务端的记忆，但
`/api/recognize` 是**无状态**的（它不落库，见 A6），所以机器值只能
**经由客户端绕一圈回来**。

后果：**`correction` 的可信度取决于客户端。** 一个有 bug（或恶意）的客户端
可以送来任意的 `machine_value`，而 `correction` 会被当成"机器说是 X、人改成 Y"
的标注数据记下来 —— 那是本项目的长期资产，被污染了不容易发现。

**更严的做法**（不在本轮范围）：识别时就把 observation 以 `pending` 状态落库，
确认时只做"采纳/修正"；或者确认时**服务端重新识别一次**以自证机器值。

当前选择"客户端回传"是因为它让 `/api/recognize` 保持**纯只读**——
那个性质有测试守着，而它是 A6 的结构性保证。**两害相权，我选了保住只读。**

## 已知限制（明写，不藏）

**没有幂等键。** 前端重复提交会建出重复的卡。
`db/models.py` 里也没有可用于去重的唯一约束（`observation` 允许同
`(card, field)` 多行，因为 A7 要求并存），所以真正的幂等需要一个
客户端令牌列 —— 那是 schema 变更，不在本轮范围。

**当前的部分防护**：若该 `asset_id` 下**已经存在卡**，则返回 409，
除非请求显式带 `confirm_again=true`。这挡住了"双击提交"这个最常见的误操作。
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from postmarktrip.api.deps import CurrentUser, DbDep, StoreDep
from postmarktrip.api.http import UNPROCESSABLE
from postmarktrip.db.models import (
    Asset,
    Card,
    Correction,
    Observation,
    Postmark,
    PostOffice,
    Tag,
)
from postmarktrip.domain import postmark_assembly, tagging
from postmarktrip.domain.schemas import (
    ConfirmResult,
    ConfirmSubmission,
)
from postmarktrip.domain.vocab import TagCategory
from postmarktrip.recognition.vocab import load_place_vocab, office_forms

router = APIRouter(tags=["recognition"])


def _crop_card(
    db: object,
    store: object,
    page: Asset,
    quad: list[tuple[float, float]] | None,
    work_scale: float | None,
) -> Asset | None:
    """按四点透视裁出"这一张卡"（**原始分辨率**），落成 `kind='card_crop'` 的派生资产。

    返回新资产；任何一步做不了都返回 `None`（**调用方照常落库**）。

    ## 为什么坐标要先除 `work_scale`

    识别返回的 `quad` 是**工作尺度**（管道把原图缩到 `work_scale` 再算几何），
    而裁图要在**原图**上做 —— 否则裁出来的是缩略图，卡片页会糊。
    `ok/15` 第 2 条修"多卡页送该卡原图"时踩过同一个换算。

    ## 为什么 here 而不是 `domain/`

    cv2 / numpy 属于**可选依赖**（`INSTALL_RECOGNITION`）。核心落库逻辑不该
    因为"没装识别栈"而 import 失败，所以是**函数内**的延迟导入 + 全面兜底。
    """
    if not quad or not work_scale or work_scale <= 0:
        return None
    try:
        import cv2
        import numpy as np

        from postmarktrip.domain.ingest import register_derived_asset
        from postmarktrip.recognition.split import warp_card
    except Exception:  # pragma: no cover - 识别栈没装时的正常分支
        return None

    page_path = store.abs_path(page.storage_path)  # type: ignore[attr-defined]
    image = cv2.imread(str(page_path))
    if image is None:
        return None
    points = np.asarray(quad, dtype=np.float32) / np.float32(work_scale)
    try:
        cropped = warp_card(image, points)
    except Exception:  # pragma: no cover - 几何异常时不该挡住落库
        return None
    #: ⚠️ **裁完必须是一张"像卡"的图**（2026-10-12 用户报"详情图损坏打不开"后补）。
    #:
    #: 退化的四边形（三点共线、或切分器把卡框错了）会让 `warp_card` 输出一张
    #: 极小/极扁的图，而 `imencode` **照样成功** → 它就被当成"这张卡的图"落库，
    #: 卡片页显示的就是一张坏图。宁可**不落库、退回整页**（整页至少是可信的证据）。
    height_px, width_px = cropped.shape[:2]
    if width_px < 200 or height_px < 200:
        return None
    ratio = width_px / height_px if height_px else 0.0
    if not 0.15 < ratio < 6.0:
        return None
    ok, buf = cv2.imencode(".jpg", cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:  # pragma: no cover - 编码失败极罕见
        return None
    return register_derived_asset(
        db,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        bytes(buf),
        kind="card_crop",
        mime="image/jpeg",
        parent_asset_id=page.id,
    )


def _persist_postmark_cut(
    db: object,
    store: object,
    page: Asset,
    circle: tuple[float, float, float] | None,
    work_scale: float | None,
) -> Asset | None:
    """抠出戳贴片（**圆外透明**）并落成 `kind='postmark_cut'` 的派生资产。

    返回新资产；任何一步做不了都返回 `None`（**调用方照常落库**）。

    ## 为什么用整页原图、而不是切好的那张卡

    贴片的用途是"叠在别的画面上"（画廊叠层），它只需要**圆片 + alpha**。
    从 warped 卡上抠就得把圆心过一遍单应变换（还得跟着旋转），纯属白找麻烦；
    而圆心在整页上是直接可用的（只需除 `work_scale`）。

    ## 为什么单独一个函数

    它和 `_crop_card` 一样，是"识别栈可选"的边界：cv2 没装就安静地不做，
    落库不受影响（与 `/api/recognize` 报 503 的哲学一致：不假装能跑）。
    """
    if not circle or not work_scale or work_scale <= 0:
        return None
    try:
        import cv2

        from postmarktrip.domain.ingest import register_derived_asset
        from postmarktrip.recognition.cutout import Circle, cut_postmark
    except Exception:  # pragma: no cover - 识别栈没装时的正常分支
        return None

    page_path = store.abs_path(page.storage_path)  # type: ignore[attr-defined]
    image = cv2.imread(str(page_path))
    if image is None:
        return None
    cx, cy, r = (float(v) / work_scale for v in circle)
    cut = cut_postmark(
        image,
        #: `Circle` 还要一个 `quality`（拟合质量）。抠图**不用它**（只读 cx/cy/r），
        #: 而载荷里也没带 —— 给 1.0 表示"人确认过这张卡，圆是可信的"。
        #: ⚠️ 别省这个参数：漏了是 `TypeError` → 500（2026-10-12 实测）。
        Circle(cx=cx, cy=cy, r=r, quality=1.0),
        dpi=page.dpi,
    )
    if cut is None:
        return None
    ok, buf = cv2.imencode(".png", cut.image)
    if not ok:  # pragma: no cover - 编码失败极罕见
        return None
    return register_derived_asset(
        db,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        bytes(buf),
        kind="postmark_cut",
        #: PNG 才有 alpha —— 贴片圆外必须透明，jpeg 做不到
        mime="image/png",
        parent_asset_id=page.id,
    )


def _mismatch_warning(picked: PostOffice, office_text: str) -> str:
    """人选的那家局与戳面读到的局名**对不上**时返回提醒（对得上就空串）。

    来自 A5 的一次真实误挂（`docs/ok/18` §2.3）：戳面读 `新建1` 却挂到
    `建溪邮政支局`（该局没有别名 → 不可能是解析器干的），而界面一声不响。

    ⚠️ 判据必须与解析器**同一套**（`vocab.office_forms`：全名/短名/别名都算对上），
    否则会出现"解析说对得上、提醒说对不上"的分叉 —— 本项目为这类分叉吃过两次亏。

    **不阻止**（人对自己的收藏有最终决定权），只提醒。
    """
    text = (office_text or "").strip()
    if not text:
        return ""
    if text in office_forms(picked.name, picked.aliases):
        return ""
    return (
        f"⚠️ 人选的是「{picked.name}」，而戳面读到的局名是「{text}」——两者对不上"
        f"（可能选错了局）。请在卡片页核对这一枚。"
    )


def _confidence(value: float | None) -> Decimal | None:
    """置信度转成 `Numeric(5,4)` 能接受的东西，并**夹到 [0,1]**。

    三件事都必须做，而且都有理由：

    * **必须是 `Decimal`，不能是 float** —— 列是 `Numeric(5,4)`。
      `review.observation_decimals` 的 docstring 记过这个坑的后果：
      某些驱动上 float 会被**截成 0**，而"所有置信度都是 0"看起来只是
      "识别质量差"，根本不会怀疑到类型上
    * **必须夹到 [0,1]** —— 表上有 `CheckConstraint("confidence >= 0 AND <= 1")`。
      OCR 偶尔给出略超 1 的值，不夹住会变成 500（约束违规），
      而那个报错与"置信度超范围"毫无字面关联
    * **量化到 4 位** —— 与列声明的精度一致，避免 DB 侧静默舍入

    注意这里**没有**复用 `review.observation_decimals`：那个函数的入参是
    整个 `ReviewBundle`，返回一批行，是给"机器原样落库"用的。
    本端点要落的是**人工确认过**的值（可能被改），所以逐字段转换。
    """
    if value is None:
        return None
    clamped = max(0.0, min(1.0, float(value)))
    return Decimal(str(clamped)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


@router.post("/cards/confirm", response_model=ConfirmResult)
def confirm_recognition(
    db: DbDep,
    store: StoreDep,
    user: CurrentUser,
    payload: ConfirmSubmission,
) -> ConfirmResult:
    """把人工确认过的识别结果写进 L2/L3。**这是唯一的写入点。**

    请求里的每个字段同时带**机器值**与**最终值**：

    * 机器值 → 写 L2 `observation`（无论人有没有改）
    * 最终值 ≠ 机器值 → 额外写 L3 `correction`

    所以"人改了什么"是**请求本身**说明的，而不是靠服务端去比对新旧行 ——
    后者会在重跑后误判（A7 允许多批 observation 并存）。
    """
    asset = db.get(Asset, payload.asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"资产不存在：{payload.asset_id}")

    existing = (
        db.scalar(
            select(func.count())
            .select_from(Card)
            .where(Card.image_asset_id == payload.asset_id, Card.deleted_at.is_(None))
        )
        or 0
    )
    if existing and not payload.confirm_again:
        # 挡住双击提交。真正的幂等需要客户端令牌列（见模块 docstring 的"已知限制"）
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"该资产下已有 {existing} 张卡。若确实要再确认一次"
            f"（例如分批录入），请带 confirm_again=true",
        )

    included = [c for c in payload.cards if not c.excluded]
    if not included:
        raise HTTPException(
            UNPROCESSABLE,
            "本次没有任何要入库的卡（全部被屏蔽？）—— 那就不该调用确认端点",
        )

    created: list[uuid.UUID] = []
    n_obs = 0
    n_corr = 0
    n_pm = 0
    notes: list[str] = []

    #: 词表与 tag **都按需加载**（不是每张卡一次，也不是每次请求一次）：
    #
    #   * `load_place_vocab` 要读 3214 个地点 + 5.4 万个局名并建索引，只在
    #     **真的需要按文本解析地点**时才付这个代价（前端接上选择器之后，
    #     id 由人选定，这一步就整段跳过）
    #   * 「主题邮局」tag 只在**判到**那四个字时才取/建 —— 否则一次普通确认
    #     会凭空创建 tag（顺带把"全部屏蔽 → 什么都不写"那条测试打破）
    place_vocab: object | None = None
    theme_tag: Tag | None = None

    for cin in included:
        if cin.visit_date is None:
            # **不替用户编日期**：它会污染按 visit_date 的统计与索引
            raise HTTPException(
                UNPROCESSABLE,
                f"卡 #{cin.index} 缺 visit_date（跑戳日）。识别没给出日期时"
                f"必须由人填 —— 替用户编一个日期会污染按日期做的统计。",
            )

        #: 人确认后的值（`final_value`）。`None` 表示"这一项不采纳"，与空串不同
        #: （见 `ConfirmFieldIn` 的说明），建戳时两者都当"没有"。
        final: dict[str, str] = {f.field_name: (f.final_value or "") for f in cin.fields}

        #: **戳面原文是必填的证据**（`Postmark.inscription` NOT NULL）。空 → 422，
        #: 与 `visit_date` 同一条哲学：服务端不替用户编一个原文。
        if not final.get("inscription", "").strip():
            raise HTTPException(
                UNPROCESSABLE,
                f"卡 #{cin.index} 缺戳面原文（inscription）。它是这枚戳的**证据**"
                f"（`Postmark.inscription` 不能为空），识别没读到时请人工填写。",
            )

        card = Card(
            visit_date=cin.visit_date,
            source=payload.defaults.source,
            status=payload.defaults.status,
            title=cin.title,
            note=cin.note,
            image_asset_id=payload.asset_id,
        )
        db.add(card)
        db.flush()  # 需要 card.id 才能挂 observation / postmark
        created.append(card.id)

        # ---- L0'：把**切好的那张卡**落成派生资产（`docs/ok/18` §3.5）----
        #
        # 这一步以前不存在，于是卡片页/画廊显示的是**整页扫描件**（9 张卡挤在
        # 一张图里）—— A5 实测里用户报的正是这个："卡片原图是 9 个卡片扫描出来的，
        # 不是切分之后的卡片"。
        #
        # 三个约束：
        #   * **不阻断落库**：识别栈没装、几何缺失、读图失败时全都保持"整页"
        #     并继续（图是增强，不是证据链的前提）。
        #   * **原始整页不丢**：`Card.image_asset_id` 改成指向切图，而切图带着
        #     `parent_asset_id` 指回整页（迁移 `0003_asset_parent`），证据链完整。
        #   * **内容寻址**：同一张卡重复确认不会存两份（`register_derived_asset`
        #     与采集层共用同一套去重）。
        cropped = _crop_card(db, store, asset, cin.quad, payload.work_scale)
        if cropped is not None:
            card.image_asset_id = cropped.id
        #: 戳贴片：从**整页原图**抠（`ok/19` §三 3.1）—— 不走单应变换，
        #: 因为贴片就是"圆片 + alpha"；从 warped 卡上抠反而要把圆心映射过去。
        postmark_cut = _persist_postmark_cut(db, store, asset, cin.circle, payload.work_scale)

        # ---- L3：**建这枚戳**（`docs/ok/16` §五）----
        #        # 这一步以前**根本不存在** —— 确认只写卡与观察档案，于是"戳"不是实体，
        # 卡片页永远显示「（缺戳面原文）」（症状见 `docs/ok/15` §四）。
        place_id = cin.place_id
        office_id = cin.post_office_id
        place_text = final.get("place", "")
        office_text = final.get("office", "")
        if place_id is None and office_id is None:
            #: 两边都没指定 → 走**与识别页显示结果完全同一个**函数
            #: （`resolve_card_links`）：界面显示"会挂到 X"、库里就挂 X，
            #: 不会出现"显示与落库分叉"（`docs/ok/18` §3.3）。
            if place_vocab is None:
                #: 只在真要解析时才建词表（见上面的说明）
                place_vocab, _office_vocab = load_place_vocab(db)
            links = postmark_assembly.resolve_card_links(
                db, place_vocab, place_text=place_text, office_text=office_text
            )
            place_id, office_id = links.place_id, links.office_id
            if links.note:
                notes.append(f"卡 #{cin.index} {links.note}")
        else:
            #: 人指定了至少一边 —— 以**人的选择**为前提去补另一边
            #: （人选的 place 决定局名在哪个子树里找，这是"人优先"的关键）
            if place_id is None and place_text.strip():
                if place_vocab is None:
                    place_vocab, _office_vocab = load_place_vocab(db)
                place_id, note = postmark_assembly.resolve_place_id(place_vocab, place_text)
                if note:
                    notes.append(f"卡 #{cin.index} {note}")
            if office_id is None:
                office_id, note = postmark_assembly.resolve_office_id(db, office_text, place_id)
                if note:
                    notes.append(f"卡 #{cin.index} {note}")
            else:
                #: **人选的局名与戳面读到的对不上 → 当场说出来。**
                #:
                #: 这一条来自 A5 的真实误挂（`docs/ok/18` §2.3）：戳面读 `新建1`，
                #: 却挂到了 `建溪邮政支局`（该局没有别名，所以不可能是解析器干的
                #: —— 是人手选时选错了），而界面**一声不响**，于是错数据就这么落库了。
                #: 服务端不阻止（人对自己的收藏有最终决定权），但必须**留下提示**。
                picked = db.get(PostOffice, office_id)
                if picked is not None:
                    warning = _mismatch_warning(picked, office_text)
                    if warning:
                        notes.append(f"卡 #{cin.index} {warning}")

        postmark = Postmark(
            card_id=card.id,
            **postmark_assembly.postmark_values_from(final),
            place_id=place_id,
            post_office_id=office_id,
            #: `note` **机器永不写**（`db/models.py` 的语义约束）。解析结果走
            #: 响应的 `notes`，由界面转达给人。
            note=None,
            #: 戳自己的抠图还没有（`ok/15` §五 第 5 条才把它返给前端），
            #: 所以这里**不指向整页** —— 那是卡的图，不是这枚戳的图。
            #: 戳自己的**贴片**（圆外透明，`ok/19` §三 3.1）：画廊叠层要用它的 alpha。
            #: 抠不出来时留 None（与切图一样，图是增强、不是证据链的前提）。
            image_asset_id=postmark_cut.id if postmark_cut is not None else None,
        )
        db.add(postmark)
        db.flush()  # 需要 postmark.id 才能挂 tag_link
        n_pm += 1

        tag_ids: set[uuid.UUID] = set(cin.tag_ids or [])
        if postmark_assembly.looks_like_theme_office(
            final["inscription"], final.get("office")
        ):
            if theme_tag is None:
                theme_tag = postmark_assembly.ensure_tag(
                    db, postmark_assembly.THEME_TAG_NAME, TagCategory.THEME
                )
            tag_ids.add(theme_tag.id)
            notes.append(
                f"卡 #{cin.index} 戳面含「{postmark_assembly.THEME_MARKER}」"
                f"→ 已自动打 tag「{postmark_assembly.THEME_TAG_NAME}」"
            )
        if tag_ids:
            tagging.set_tags(db, tagging.TARGET_POSTMARK, postmark.id, list(tag_ids))

        for f in cin.fields:
            obs = Observation(
                asset_id=payload.asset_id,
                card_id=card.id,
                pipeline_version=f.pipeline_version or payload.pipeline_version,
                model_name=f.model_name,
                model_version=f.model_version,
                field_name=f.field_name,
                raw_value=f.machine_value,
                normalized_value=f.machine_value,
                confidence=_confidence(f.machine_confidence),
                bbox=f.bbox,
            )
            db.add(obs)
            db.flush()  # 需要 obs.id 供 correction 引用
            n_obs += 1

            # **只在真的改了的时候写 correction**（A8）
            if f.final_value is not None and f.final_value != (f.machine_value or ""):
                db.add(
                    Correction(
                        card_id=card.id,
                        field_name=f.field_name,
                        machine_value=f.machine_value,
                        human_value=f.final_value,
                        machine_confidence=_confidence(f.machine_confidence),
                        observation_id=obs.id,
                        corrected_by=getattr(user, "id", None),
                    )
                )
                n_corr += 1

    db.commit()

    return ConfirmResult(
        created_cards=len(created),
        excluded_cards=len(payload.cards) - len(included),
        observations=n_obs,
        corrections=n_corr,
        postmarks=n_pm,
        notes=notes,
        card_ids=created,
    )

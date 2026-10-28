"""识别路由：把一张扫描图变成"待确认的识别结果"。

## 为什么这个端点**不写库**

spec A6：**所有自动结果无一例外经过人工确认才落库**。
所以这里只做**无状态预览** —— 识别完就把结果返回，不产生任何
``observation`` / ``card`` 行。落库在用户确认之后的端点里发生。

这样 A6 是**结构上不可能违反**的（没有写库的代码路径），
而不是"靠人记得别写"。

## 入口：`asset_id`（首选）或 `file`

**首选 `asset_id`**：先用既有的 `POST /api/assets` 上传（内容寻址、自动去重、
原始件留档），再拿 `asset_id` 来识别。这样：

* **原始件有档案**（它是 L0 证据，spec 说"只进不出"），校正时能回看原图
* **复用既有上传路径** —— 项目已经因为"两份等价逻辑"栽过（`ok/06` 缺陷 #9/#10）
* 确认落库时 `card.asset_id` 的归属天然正确

`file` 保留是为了**开发/排障时的临时投喂**。两条入口共用**同一段识别逻辑**，
所以这不是"两份等价实现"，只是两个输入适配器。

## 这个端点是**只读**的

spec A6 要求"所有自动结果无一例外经过人工确认才落库"。
本端点需要一个 db 会话（要按 `asset_id` 取图），但**它只读**。
守卫测试断言的是**模块里没有任何写操作** —— 见 `test_recognize_route_is_read_only`。

（早先的版本连 db 依赖都不允许。方案 A 要求读资产，所以那条断言必须放宽到
“不写” —— 但**不能删掉**：它要拦的正是"顺手把 observation 存一下"这种改动。）

## 云端是**按请求开关**的，默认关

云端识别要花钱，且结果**不可复现**。所以：

* 默认 ``use_vlm=false`` —— 只跑本地，零成本、可复现
* 请求显式 ``use_vlm=true`` 且服务端 ``settings.vlm_enabled`` 也打开时才走云端
* 响应里回传 ``used_vlm`` 与每张卡的 ``field_source``

## 识别 extra 没装时**明确报 503**，不假装能跑

``recognition`` 是可选依赖（``INSTALL_RECOGNITION``）。没装时返回 503
并**说明缺什么** —— 而不是 500 或者一个空结果。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from postmarktrip.api.deps import CurrentUser, DbDep, StoreDep
from postmarktrip.api.http import UNPROCESSABLE
from postmarktrip.config import settings
from postmarktrip.db.models import Asset
from postmarktrip.domain.postmark_assembly import resolve_card_links
from postmarktrip.domain.schemas import (
    CardLinksRead,
    RecognizedCardRead,
    RecognizedCircleRead,
    RecognizedFieldRead,
    RecognizedPageRead,
    ReviewItemRead,
)
from postmarktrip.recognition.split import DEFAULT_EXPECTED_CARDS, GEOMETRY_MODE
from postmarktrip.storage import StorageError

router = APIRouter(tags=["recognition"])


def _require_recognition() -> None:
    """识别不可用时 503，并且**分清是哪一种不可用**。

    两种情况都是 503，但**要做的事完全不同**，所以消息必须分开：

    * **未启用** —— ``PMTRIP_OCR_ENGINE`` 还是默认的 ``none``。
      这是**配置开关**，改环境变量即可
    * **依赖没装** —— 引擎选了，但 ``recognition`` extra 没装。
      这时才需要装包

    第一版把两者合成一句"识别依赖未安装" —— 于是在
    ``OCR_ENGINE=none``（**默认状态**）下会叫人去装一个已经装好的包。
    """
    from postmarktrip.providers import probe_cv, probe_ocr

    disabled: list[str] = []
    missing: list[str] = []
    for cap in (probe_cv(), probe_ocr()):
        if cap.ready:
            continue
        if cap.enabled:
            missing.append(f"{cap.name}: {cap.detail}")
        else:
            disabled.append(f"{cap.name}: {cap.detail}")

    if missing:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "识别依赖未安装（recognition extra）—— "
            + "；".join(missing)
            + "。安装方式见 docs/ok/08-P2-运维手册.md 的 P2-1。",
        )
    if disabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "识别未启用 —— "
            + "；".join(disabled)
            + "。识别默认关闭（可选依赖不该在升级后自动开始跑）；"
            + "要启用请在 .env 里设 OCR_ENGINE=rapidocr（也认 PMTRIP_OCR_ENGINE，"
            + "两个都设时以前者以外的带前缀那个优先）并安装 recognition extra。",
        )


def _to_card(card: object) -> RecognizedCardRead:
    """把 ``CardResult`` 映射成响应模型。

    字段直接取自 ``review.ReviewBundle.observations`` ——
    那里**已经**算好了 ``decision`` 与 ``reason``（含词表那道防线），
    在 API 层重算一遍必然会与校对队列的判断不一致。
    """
    fields: list[RecognizedFieldRead] = []
    queue: list[ReviewItemRead] = []
    bundle = card.bundle  # type: ignore[attr-defined]
    if bundle is not None:
        fields = [
            RecognizedFieldRead(
                field_name=o.field_name,
                value=o.normalized_value or o.raw_value,
                confidence=o.confidence,
                decision=o.decision,
                reason=o.reason,
                pipeline_version=o.pipeline_version,
                # 命中区划时从树里补出来的层级链（从粗到细）。
                # ⚠️ 只有**最后一级**是 OCR 读到的，上面几级是数据库补的 ——
                # 用户要的"智能识别它属于哪个省"就是这个（见 ok/13）。
                place_path=list(o.place_path),
            )
            for o in bundle.observations
        ]
        queue = [
            ReviewItemRead(
                field_name=q.field_name, value=q.value, confidence=q.confidence, why=q.why
            )
            for q in bundle.queue
        ]

    circle = None
    verdict = card.verdict  # type: ignore[attr-defined]
    card_size = card.card_size  # type: ignore[attr-defined]
    if verdict is not None and verdict.circle is not None:
        card_w = max(1, card_size[0])
        # 坐标变换已在**管线里**做好（`CardResult.circle_page`）——
        # 放这里曾经让另一个消费者漏掉它，所以移走了。
        page = card.circle_page  # type: ignore[attr-defined]
        assert page is not None
        circle = RecognizedCircleRead(
            cx=round(page[0], 1),
            cy=round(page[1], 1),
            r=round(page[2], 1),
            # 这个比值是**尺度无关**的，所以用卡局部的半径算（与坐标空间无关）
            radius_over_card_width=round(verdict.circle.r / card_w, 3),
        )

    return RecognizedCardRead(
        index=card.index,  # type: ignore[attr-defined]
        bbox=card.bbox,  # type: ignore[attr-defined]
        quad=[(round(x, 1), round(y, 1)) for x, y in card.quad],  # type: ignore[attr-defined]
        card_size=card_size,
        size_suspect=card.size_suspect,  # type: ignore[attr-defined]
        postmark_type=verdict.kind if verdict is not None else "",
        postmark_reason=verdict.reason if verdict is not None else "",
        circle=circle,
        fields=fields,
        queue=queue,
        ocr_lines=[(t, round(c, 4)) for t, c in card.ocr_lines],  # type: ignore[attr-defined]
        field_source=card.field_source,  # type: ignore[attr-defined]
        geometry_source=card.geometry_source,  # type: ignore[attr-defined]
        notes=card.notes,  # type: ignore[attr-defined]
    )


@router.post("/recognize", response_model=RecognizedPageRead)
async def recognize_page(
    db: DbDep,
    store: StoreDep,
    _: CurrentUser,
    asset_id: Annotated[uuid.UUID | None, Query()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    dpi: Annotated[int | None, Query(ge=72, le=2400)] = None,
    use_vlm: Annotated[bool, Query()] = False,
    long_side: Annotated[int, Query(ge=400, le=6000)] = 2000,
    max_cards: Annotated[int, Query(ge=0, le=200)] = DEFAULT_EXPECTED_CARDS,
) -> RecognizedPageRead:
    """识别一张扫描图（或单卡图），返回**待确认**的结果。**不写库。**

    * ``dpi`` 不给就走切分器的反推逻辑；给了就作为物理量的基准
    * ``use_vlm`` 默认关。云端要花钱且结果不可复现，所以必须显式打开
    * ``max_cards`` = "这一页该有几张卡"的先验，**几何兜底判据要看它**。
      整页扫描用默认 **9**；**单卡入口传 0**（= 不知道）——
      否则每张单卡图都会因为"少于 9 张"被判可疑，白花一次云端调用，
      而单卡图恰恰最不需要云端几何兜底。

    **坐标系**：``cards[].quad`` / ``bbox`` 与 ``cards[].circle`` 都是
    **页面坐标**（圆已从卡局部映射回来），所以前端可以把它们与页面预览
    图直接叠加。
    """
    _require_recognition()

    import cv2
    import numpy as np

    from postmarktrip.recognition.pipeline import recognize_page as _recognize
    from postmarktrip.recognition.vocab import load_place_vocab

    raw = b""
    if asset_id is not None:
        asset = db.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"资产不存在：{asset_id}")
        try:
            path = store.abs_path(asset.storage_path)
        except StorageError as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, f"资产路径不可解析：{exc}"
            ) from exc
        if not path.exists():
            # 库里有行、盘上没文件 —— 说明两者不同步，必须说清而不是当成解码失败
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"资产文件缺失：{asset.storage_path}（数据库有记录但盘上没有）",
            )
        raw = path.read_bytes()
    elif file is not None:
        raw = await file.read()

    if asset_id is None and file is None:
        raise HTTPException(
            UNPROCESSABLE,
            "必须给 asset_id（首选）或 file —— 前者能留档原始件，后者只用于开发排障",
        )
    if not raw:
        raise HTTPException(UNPROCESSABLE, "空文件")
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"文件超过上限 {settings.max_upload_bytes} 字节",
        )

    buf = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "解码失败：不是识别支持的图片格式（或文件已损坏）",
        )

    vlm_config = None
    used_vlm = False
    if use_vlm and settings.vlm_enabled:
        from postmarktrip.recognition.vlm import resolve_config

        vlm_config, _why = resolve_config(
            provider=settings.vlm_provider,
            base_url=settings.vlm_base_url,
            api_key=settings.vlm_api_key,
            model=settings.vlm_model,
            timeout_s=settings.vlm_timeout_s,
            max_tokens=settings.vlm_max_tokens,
            enabled=True,
        )
        used_vlm = vlm_config is not None

    # 词表：决定 4 的绿勾规则靠它（开放文本字段"高置信度**且**在词表里"）。
    #
    # ⚠️ 这里以前**没有传**它，于是 `review.decide()` 拿到的 `known_values` 是
    # None，第二条防线整段不执行 —— 而症状很轻：界面颜色看起来都对，
    # 只是「福建南火」（置信度 0.948，真值福建南平）被判成"可自动采纳"并落了库。
    # 实测拿到过这条现场证据后才接上的。
    #
    # 传的是**查询器**（不是扁平集合）：它按区划树判"是不是地名"，还能补出
    # 完整层级链（用户要的"智能识别它属于哪个省"）。
    place_vocab, office_vocab = load_place_vocab(db)

    result = _recognize(
        img,
        dpi=dpi,
        long_side=long_side,
        place_vocab=place_vocab,
        office_vocab=office_vocab,
        vlm_config=vlm_config,
        #: **必须与 `vlm_config` 一起传**（2026-10-11 修）。
        #:
        #: `vlm_primary` 是"云端优先"那个开关：它决定管线走
        #: **整卡一次调用读完戳型与全部字段**（见 pipeline 里那段长注释）。
        #: 而这里以前只传 `vlm_config`、**没传它** —— 于是：
        #:   * 云端只用来判型（`PROMPT_CLASSIFY_TYPE` 一次）
        #:   * 字段仍旧是本地 OCR 的（`field_source='local'`）
        #:   * "整卡读全部字段"那段**从来没执行过**（死代码）
        #:
        #: 症状极隐蔽：`used_vlm=True`、戳型也确实被云端纠正了
        #: （`风景戳特写` 从 unknown 变 scenic），看起来"云端在工作" ——
        #: 但字段还是本地那套（`福建南火`）。
        #: **教训：「云端已启用」不等于「字段来自云端」**，要看 `field_source`。
        vlm_primary=used_vlm,
        #: 几何兜底判据的"期望张数"先验。**调用方给什么就是什么**：
        #: 整页扫描默认 9，单卡入口显式传 0（"不知道"）。
        expected_cards=max_cards,
    )

    return RecognizedPageRead(
        pipeline_version=result.pipeline_version,
        work_scale=round(result.work_scale, 4),
        work_size=result.work_size,
        card_count=result.card_count,
        cards=[_with_links(db, place_vocab, _to_card(c)) for c in result.cards],
        stage_notes=result.stage_notes,
        used_vlm=used_vlm,
        geometry_source=result.split.geometry_source if result.split else "local",
        geometry_mode=GEOMETRY_MODE.value,
    )


def _with_links(
    db: Session,
    place_vocab: object,
    card: RecognizedCardRead,
) -> RecognizedCardRead:
    """给一张卡补上**结构化地点/邮局的自动解析结果**（`docs/ok/18` §3.3）。

    用户 2026-10-12："尽管不填【结构化】，只要能在【局名】【地名】命中，
    就能补充到结构化字段上面去"。这件事确认时**本来就在做**，缺的是界面
    **看不见** —— 于是人在 A5 里手填了 14 次结构化选择（大多本来就解析得出来）。

    ## 三条约束

    1. **不写库**：这个端点是只读的（spec A6），解析结果只是**回报**给界面。
    2. **同一条规则**：走 `resolve_card_links`，与确认落库那条**完全同一个函数**
       —— 否则界面显示"会挂 A"、库里挂上 B，而人看不到这个分叉。
    3. **只按机器读到的文本解析**：人一改文本，界面必须把预填的 id 作废
       （前端做，前端测 —— 见 `RecognizedPage` 的 `withFieldValue` 联动）。
    """
    place_text = _field_value(card, "place")
    office_text = _field_value(card, "office")
    if not place_text and not office_text:
        return card
    card.links = CardLinksRead.model_validate(
        resolve_card_links(db, place_vocab, place_text=place_text, office_text=office_text)
    )
    return card


def _field_value(card: RecognizedCardRead, name: str) -> str:
    """取某个字段的**机器值**（没有就是空串）。"""
    for f in card.fields:
        if f.field_name == name:
            return f.value or ""
    return ""

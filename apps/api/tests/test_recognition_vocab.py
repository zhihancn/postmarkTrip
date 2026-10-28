"""词表（`recognition/vocab.py`）的测试。

**为什么这个模块需要测试**：它决定 4 的"绿勾规则"能不能生效 ——
开放文本字段（`place` / `office`）= 高置信度 **且 在词表里**。
而词表**喂错形状**的表现是静默的：

* 喂得太窄（忘了拼"省+市"）→ **正确的**地名也被判"未登记"→ 全标黄，
  人会以为识别质量差，其实是词表形状不对；
* 喂得太宽（把"省"后缀也拼进去）→ 该被拦的错值反而匹配上 → 防线失效。

所以这里盯的是**形状**：戳面上真实出现的写法要在，
而明显不该在的（带"省/市"后缀、单独一个戳号）不要混进去。

⚡ 这组测试的价值在"将来有人往字典里灌真实数据"时体现：形状错了，
A5 的点击数会莫名其妙地高，而原因不在识别上。
"""

from __future__ import annotations

import uuid

import pytest

from postmarktrip.recognition.vocab import (
    DIVISION_SUFFIXES,
    MAX_ANCESTOR_DEPTH,
    PlaceVocab,
    describe_place,
    build_office_names,
    build_place_names,
    build_vocab,
    place_short_name,
    strip_division_suffix,
)

# --------------------------------------------------------------------------- #
#  strip_division_suffix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("南平市", "南平"),
        ("福建省", "福建"),
        ("玉山县", "玉山"),
        ("三清山", "三清山"),  # 「山」不是后缀，别乱去
        ("建溪", "建溪"),
        ("北京市", "北京"),
        ("鼓楼区", "鼓楼"),
        ("三清乡", "三清"),
    ],
)
def test_strip_suffix(raw: str, expected: str) -> None:
    assert strip_division_suffix(raw) == expected


def test_strip_suffix_handles_autonomous_region_in_one_pass() -> None:
    """「广西壮族自治区」只能剥一次，而且剥的必须是「自治区」。

    循环剥（或 `rstrip("区")`）会得到「广西壮族自治」—— 那种值永远不会
    出现在戳面上，只会白白撑大词表。
    """
    assert strip_division_suffix("广西壮族自治区") == "广西壮族"
    assert strip_division_suffix("内蒙古自治区") == "内蒙古"
    assert strip_division_suffix("新疆维吾尔自治区") == "新疆维吾尔"


def test_strip_suffix_keeps_short_names_intact() -> None:
    """整名就是一个后缀时不能剥成空串（「市」这种脏数据不该造出空词）。"""
    assert strip_division_suffix("省") == "省"
    assert strip_division_suffix("") == ""


def test_suffix_table_is_longest_first() -> None:
    """顺序有语义：长的必须在短的前面。

    这条测试存在的理由很具体：`自治区` 与 `区` 同时存在时，若 `区` 排在前面，
    `strip_division_suffix("广西壮族自治区")` 会先匹配到 `区` 而剥错。
    """
    for longer, shorter in (("自治区", "区"), ("自治州", "州"), ("特别行政区", "区")):
        assert longer in DIVISION_SUFFIXES
        if shorter in DIVISION_SUFFIXES:
            assert DIVISION_SUFFIXES.index(longer) < DIVISION_SUFFIXES.index(shorter)


# --------------------------------------------------------------------------- #
#  build_place_names：戳面上的写法要造出来
# --------------------------------------------------------------------------- #


def test_builds_province_plus_city() -> None:
    """「福建省」+「南平市」要能造出戳面上的「福建南平」。

    这是本模块存在的**唯一理由**：`place` 表存的是标准名，戳面上写的是
    去掉"省/市"的连写。
    """
    province = uuid.uuid4()
    city = uuid.uuid4()
    names = build_place_names(
        [
            (province, None, "福建省"),
            (city, province, "南平市"),
        ]
    )
    assert "福建南平" in names, f"没造出福建南平：{sorted(names)}"
    # 标准名本身也要在（用户可能就那么登记）
    assert "福建省" in names
    assert "南平市" in names
    assert "南平" in names


def test_builds_province_plus_county_when_province_is_direct_parent() -> None:
    """直辖市那种"省级 → 县级"（中间层留空）也要能拼。"""
    province = uuid.uuid4()
    county = uuid.uuid4()
    names = build_place_names(
        [
            (province, None, "北京市"),
            (county, province, "东城区"),
        ]
    )
    assert "北京东城" in names


def test_joined_names_use_the_suffix_stripped_forms_only() -> None:
    """连写只产出「福建南平」，**不产出**「福建省南平市」。

    这条钉的是"词表能多窄就多窄"：戳面上没有"两个标准名直接粘起来"的写法，
    而每多一条就多一次"错值恰好命中"的机会 —— 词表越宽，防线越松。
    少一条的代价是正确值可能被标黄（多看两眼），
    多一条的代价是错值被当成可信放过去（正是「福建南火」溜过去的那条路）。
    """
    province = uuid.uuid4()
    city = uuid.uuid4()
    names = build_place_names(
        [
            (province, None, "福建省"),
            (city, province, "南平市"),
        ]
    )
    assert "福建南平" in names
    # 单名两种写法都要在（用户可能就那么登记，OCR 也可能读到）
    assert "福建省" in names
    assert "福建" in names
    assert "南平市" in names
    assert "南平" in names
    # 但连写不许出现带后缀的组合
    for bad in ("福建省南平市", "福建省南平", "福建南平市"):
        assert bad not in names, f"词表里不该有 {bad}"


def test_autonomous_region_uses_the_explicit_short_name() -> None:
    """自治区的戳面短名靠**显式表**，不靠机械去后缀。

    实测：`广西壮族自治区` 机械去后缀得到 `广西壮族`，而戳面上写的是 **`广西`**。
    所以这条钉的是"索引里必须同时有 `广西` 和 `广西南宁`，而不是 `广西壮族南宁`"。
    """
    province = uuid.uuid4()
    city = uuid.uuid4()
    names = build_place_names([(province, None, "广西壮族自治区"), (city, province, "南宁市")])
    assert "广西" in names, f"缺少显式短名：{sorted(names)}"
    assert "广西南宁" in names, "戳面连写应当是「广西南宁」"
    assert "广西壮族南宁" not in names, "这是旧实现的 bug 产物，戳面上不存在"


def test_builds_three_level_chain() -> None:
    """省 → 地级市 → 县级：祖先链要能拼到第 2 层（省+县）。"""
    province = uuid.uuid4()
    city = uuid.uuid4()
    county = uuid.uuid4()
    names = build_place_names(
        [
            (province, None, "福建省"),
            (city, province, "南平市"),
            (county, city, "武夷山市"),
        ]
    )
    assert "福建南平" in names
    assert "福建武夷山" in names, "省+县 的写法在戳面上存在（跨过地级市）"
    assert "南平武夷山" in names


def test_does_not_walk_past_max_depth() -> None:
    """不能无限往上拼 —— 再往上是国名，不会出现在戳上，只是噪声。"""
    ids = [uuid.uuid4() for _ in range(MAX_ANCESTOR_DEPTH + 2)]
    rows = []
    for index, place_id in enumerate(ids):
        rows.append(
            (
                place_id,
                None if index == 0 else ids[index - 1],
                f"层级{index}",
            )
        )
    names = build_place_names(rows)
    # 最深的那个（index = MAX+1）最多只能看到第 MAX 层祖先
    # 即 index 3 能看到 index 2 与 index 1，看不到 index 0
    assert "层级2层级3" in names
    assert "层级1层级3" in names
    assert "层级0层级3" not in names, "越过深度上限了"


def test_broken_parent_link_is_skipped_not_guessed() -> None:
    """父节点不在结果里时**不猜** —— 数据不全就少造词，不要造错词。

    这条对应本项目的"不知道比基于噪声的答案好"：宁可把正确的值标成待确认，
    也不要凭空造出一个不存在的写法让它蒙对。
    """
    orphan = uuid.uuid4()
    missing_parent = uuid.uuid4()
    names = build_place_names([(orphan, missing_parent, "南平市")])
    assert "南平" in names
    assert "南平市" in names
    # 没有父名可用，所以只该有这两条（外加去后缀那条）
    assert not any(len(n) > 3 for n in names), f"不该造出更长的词：{sorted(names)}"


def test_empty_dictionary_gives_empty_set() -> None:
    """空字典是合法状态（实测数据库里 place 就是 0 行）。"""
    assert build_place_names([]) == frozenset()


def test_no_empty_strings_in_result() -> None:
    """任何情况下都不许产出空串 —— 空串会让 `decide()` 的 `value in known` 变味。"""
    province = uuid.uuid4()
    names = build_place_names([(province, None, "省")])
    assert "" not in names


# --------------------------------------------------------------------------- #
#  build_office_names
# --------------------------------------------------------------------------- #


def test_office_names_include_aliases() -> None:
    names = build_office_names(
        [
            ("建溪", ["建溪支局", "建溪邮政所"]),
            ("四号航站楼", []),
        ]
    )
    assert names == {"建溪", "建溪支局", "建溪邮政所", "四号航站楼"}


def test_office_names_strip_whitespace_and_skip_blanks() -> None:
    names = build_office_names([("  建溪  ", ["  ", "", "别名"])])
    assert names == {"建溪", "别名"}


def test_office_names_tolerate_non_list_aliases() -> None:
    """`aliases` 是 JSONB，历史数据里可能是 null 或别的类型 —— 不许崩。"""
    assert build_office_names([("建溪", None)]) == {"建溪"}
    assert build_office_names([("建溪", "不是列表")]) == {"建溪"}


def test_office_names_never_contain_a_bare_postmark_number() -> None:
    """戳号不进词表。

    「建溪 1」的 `1` 是字段 `office_no`，而 `decide()` 对那个字段**不做**
    词表校验（`review.MECHANICALLY_VALIDATED`）。把纯数字塞进局名词表，
    只会让"某个局名恰好等于某个戳号"这种巧合变成一次错误的放行。
    """
    names = build_office_names([("建溪", ["1", "2"])])
    assert "1" not in names
    assert "2" not in names


# --------------------------------------------------------------------------- #
#  load_known_places / load_known_offices：与数据库相接的那一层
#
#  ⚠️ 这一组是**补上的**，因为先前那一版 `load_*` 里有个只在字典非空时
#     才出现的错，而纯函数测试与 `_dev.sh all` 全绿都没抓到它。
#     教训：**纯函数的测试再多，也证明不了"那一层真的接上了"**。
# --------------------------------------------------------------------------- #


class _FakeSession:
    """模拟 `Session.execute(...).tuples().all()` 的结果。

    ⚠️ 形状必须与**真实**的 `Result` 一致：多列查询的元素是 `Row`，可以按下标
    取（`row[0]`/`row[1]`/`row[2]`）。替身形状写错会"证明"一个真实环境里跑
    不通（或相反）的写法 —— 这一组测试第一版就写错了两回，见下面那条测试。
    """

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def execute(self, stmt: object) -> _FakeSession:  # noqa: ARG002
        return self

    def tuples(self) -> _FakeSession:
        return self

    def all(self) -> list[tuple[object, ...]]:
        return self._rows


def test_load_known_places_reads_a_non_empty_dictionary() -> None:
    """字典**非空**时必须真的解析出多列。

    这条是那条"只在非空时暴露"的 bug 的回归测试：空字典下循环体一次都不执行，
    所以任何形状错误都被掩盖 —— 单测与 `_dev.sh all` 当时全绿。
    """
    from postmarktrip.recognition.vocab import load_known_places

    province = uuid.uuid4()
    city = uuid.uuid4()
    db = _FakeSession(
        [
            (province, None, "福建省", "省级"),
            (city, province, "南平市", "地级市"),
        ]
    )
    names = load_known_places(db)  # type: ignore[arg-type]
    assert "福建南平" in names, f"没解析出父子关系：{sorted(names)}"
    assert "南平" in names


def test_load_known_offices_reads_a_non_empty_dictionary() -> None:
    from postmarktrip.recognition.vocab import load_known_offices

    db = _FakeSession([("建溪", ["建溪支局"])])
    names = load_known_offices(db)  # type: ignore[arg-type]
    assert names == {"建溪", "建溪支局"}


def test_load_known_places_on_empty_dictionary() -> None:
    """空字典是合法状态（实测库里 place 就是 0 行）。"""
    from postmarktrip.recognition.vocab import load_known_places

    assert load_known_places(_FakeSession([])) == frozenset()  # type: ignore[arg-type]


def test_scalars_result_has_no_tuples() -> None:
    """`ScalarResult`（`db.scalars()` 的返回）**没有** `.tuples()`。

    这条钉住的是"走哪条路读多列"这个决定，以及它的**理由**：

    * `db.scalars(select(A, B, C))` —— 只给第一列（元素是 UUID，不可下标）
    * `db.scalars(...).tuples()`   —— `AttributeError`（就是这个方法不存在）
    * `db.execute(...).tuples()`   —— ✅ 元素是 Row

    ⚠️ 我在这上面连错两次，第二次是**用 `hasattr` 得出的结论**：
    见 `ScalarResult` 没有 `.tuples()`，就推断"那 scalars 应该返回行元组"，
    于是回退掉了正确的修复。`hasattr` 只能回答"有没有这个方法"，
    回答不了"它返回什么" —— 要回答那个，只能真的取一个元素看看。

    所以这条测试只是**辅助**：它保证"用 execute().tuples()"这个选择的前提
    没变（SQLAlchemy 升级后若 ScalarResult 有了 .tuples()，这里会提醒重看）。
    真正的不变量由上面那三条 `load_*` 测试 + 真库验证共同守着。
    """
    from sqlalchemy.engine import Result, ScalarResult

    assert not hasattr(ScalarResult, "tuples"), (
        "ScalarResult 有了 .tuples() —— SQLAlchemy 升级了，这条假设要重看"
    )
    #: ⚠️ 这里**不再**断言 `Result` 有 `.tuples()` —— SQLAlchemy 2.1 起它已废弃
    #: （`Result.tuples() method is deprecated`），正确写法是直接解包
    #: `db.execute(...).all()`。原来那条断言反而会把**过时用法**钉死，
    #: 所以改成断言我们真正依赖的性质。
    assert hasattr(Result, "all"), "Result 必须有 all()"


# --------------------------------------------------------------------------- #
#  PlaceVocab：按区划树判定并补全层级
#
#  这一组是**规则的可执行说明书**。它的每一条对应 `ok/13` §一 里定下的规则，
#  以及用户 2026-10-08 拍板的两个形态（"省+市"连写、单纯市名）。
#  规则错的表现是静默的：放宽一点就放过错值，收紧一点就通篇标黄。
# --------------------------------------------------------------------------- #


def _tree() -> PlaceVocab:
    """一棵小树：两省 + 两市 + 一县 + 一直辖市，够覆盖所有形态。

    刻意包含 `广西壮族自治区`（自治区短名靠显式表）与 `北京市`（直辖市，
    中间层留空），因为这两类是规则最容易出错的地方。
    """
    fj, gx, bj = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    np_, fz, nn, yushan = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    return PlaceVocab(
        [
            (fj, None, "福建省"),
            (gx, None, "广西壮族自治区"),
            (bj, None, "北京市"),
            (np_, fj, "南平市"),
            (fz, fj, "福州市"),
            (nn, gx, "南宁市"),
            (yushan, fj, "玉山县"),
        ]
    )


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        # 形态一：值本身就是地名（全名或短名）
        ("福建南平", "pair"),
        ("南宁", "single_exact"),
        ("福州", "single_exact"),
        ("南平市", "single_exact"),
        ("玉山", "single_exact"),
        # 自治区的短名必须来自显式表
        ("广西", "single_exact"),
        ("广西南宁", "pair"),
        ("内蒙古自治区", None),  # 不在树里
        # 形态二：上级+本级连写
        ("福建玉山", "pair"),
        # 拒绝：实测的真错值（这就是这套机制存在的理由）
        ("福建南火", None),
        ("薛南平", None),
        ("福建南烫", None),
        # 拒绝：三段拼不出来（旧实现的 bug 产物）
        ("广西壮族南宁", None),
        # 拒绝：两段都是地名但**不是**父子关系（不能随便拼）
        ("福建南宁", None),
        ("广西福州", None),
        # 拒绝：空值
        ("", None),
    ],
)
def test_lookup_accepts_exactly_two_legal_shapes(value: str, kind: str | None) -> None:
    result = _tree().lookup(value)
    if kind is None:
        assert not result.known, f"{value!r} 不该被接受，却命中了 {result.hit}"
    else:
        assert result.known, f"{value!r} 应当被接受（形态 {kind}）"
        assert result.hit is not None
        assert result.hit.kind == kind


def test_lookup_fills_in_the_ancestor_chain() -> None:
    """命中后要**补出完整层级链** —— 这是用户要的"智能识别它属于哪个省"。

    `南宁` 这个值本身不含省名，但数据库知道它在广西辖下。
    """
    result = _tree().lookup("南宁")
    assert result.known and result.hit is not None
    assert result.hit.path == ("广西壮族自治区", "南宁市"), result.hit.path
    assert result.place_path == ("广西壮族自治区", "南宁市")


def test_lookup_keeps_the_ocr_value_untouched() -> None:
    """补全**不许改写** OCR 原样 —— 原样是证据，要做 correction 标注。

    这条挡的是一类很自然的想法："既然知道它属于广西，干脆把值也改写成
    「广西南宁」"。那样 `Correction.machine_value` 就不再是"机器看到了什么"。
    """
    result = _tree().lookup("南宁")
    assert result.value == "南宁", "命中的值被改写成了补全结果"


def test_lookup_does_not_do_prefix_probing() -> None:
    """**不许**把"开头是某个地名、余下不是"当成"只读到上级"。

    早期实现这么做过，结果把「福建南火」判成"漏字"（可接受）而不是
    "错字"（必须拦）—— 而拦它正是这套机制存在的唯一理由。
    """
    result = _tree().lookup("福建南火")
    assert not result.known


def test_same_named_province_and_city_with_full_suffixes() -> None:
    """`吉林省吉林市` —— 用户 2026-10-08 亲口举的例子，戳上真的这么写。

    省与市同名时必须写全称才分得清，所以"两段"是 `吉林省` + `吉林市`。
    按裸串切会得到 `(吉林, 省吉林)` 或 `(吉林省吉, 林市)`，都命中不了。
    """
    province = uuid.uuid4()
    city = uuid.uuid4()
    vocab = PlaceVocab(
        [
            (province, None, "吉林省", "省级"),
            (city, province, "吉林市", "地级市"),
        ]
    )
    result = vocab.lookup("吉林省吉林市")
    assert result.known, "省+市同名时写全称，必须能识别"
    assert result.hit is not None
    assert result.hit.kind == "pair_suffixed"
    assert result.hit.path == ("吉林省", "吉林市")


def test_suffixed_form_does_not_accept_a_bogus_suffix() -> None:
    """带后缀的形态**不许**把 `福建南宁市` 当合法连写。

    这是"只认自己那一级的后缀"要防的事：若用通用后缀表去剥，`福建` 也能被
    剥成 `福`，于是不是上下级连写的值也被接受 —— 防线就失效了。
    """
    fj, np_, fz = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    vocab = PlaceVocab(
        [
            (fj, None, "福建省", "省级"),
            (np_, fj, "南平市", "地级市"),
            (fz, fj, "福州市", "地级市"),
        ]
    )
    # 两段都在树里，但 `福建` 不是"带省级后缀的写法"（它没有 `省` 字）
    assert not vocab.lookup("福建南宁市").known
    # 而真正带全称的写法要认
    assert vocab.lookup("福建省南平市").known


def test_direct_municipality_has_no_middle_level() -> None:
    """直辖市：省级 → 县级，中间层留空。层级链要如实反映，不能硬造一级。"""
    result = _tree().lookup("北京市")
    assert result.known and result.hit is not None
    assert result.hit.path == ("北京市",), "直辖市不该被硬塞一个中间层"


def test_pair_key_never_shadows_a_single_name() -> None:
    """连写键不许与任何**单名**相同。

    否则同一个键有两种含义（既是某个地名本身，又是另外两个的连写），
    查询时就会取到错误的那一个 —— 而且是静默的。
    """
    vocab = _tree()
    overlap = set(vocab._single) & set(vocab._pair)
    assert not overlap, f"这些键既是单名又是连写：{sorted(overlap)}"


def test_office_vocab_is_a_plain_list() -> None:
    """局名不做树判定（局名没有层级可拼），就是一张名单。"""
    _, office_vocab = build_vocab([], [("建溪", ["建溪支局"]), ("中亭街", [])])
    assert office_vocab.lookup("建溪").known
    assert office_vocab.lookup("建溪支局").known
    assert office_vocab.lookup("中亭街").known
    assert not office_vocab.lookup("建溪1").known, "戳号不参与局名匹配"


def test_place_short_name_prefers_the_explicit_table() -> None:
    assert place_short_name("广西壮族自治区") == "广西"
    assert place_short_name("新疆维吾尔自治区") == "新疆"
    assert place_short_name("南平市") == "南平"
    assert place_short_name("三清山") == "三清山"


# --------------------------------------------------------------------------- #
#  自治单位的短名 —— 机械去后缀在**155 个**自治单位上系统性跑偏
#
#  这批用例是拿民政部 2024 年那份真实数据（3213 行）跑出来的，
#  每一条都对应一个真实存在的行政区划。
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("full", "short"),
    [
        # 单民族：去后缀会得到「青龙满族」
        ("青龙满族自治县", "青龙"),
        ("孟村回族自治县", "孟村"),
        ("岫岩满族自治县", "岫岩"),
        # 多民族：去后缀会得到「围场满族蒙古族」
        ("围场满族蒙古族自治县", "围场"),
        ("彭水苗族土家族自治县", "彭水"),
        # 长链：去后缀会得到 10 个字的怪物
        ("双江拉祜族佤族布朗族傣族自治县", "双江"),
        ("积石山保安族东乡族撒拉族自治县", "积石山"),
        # 「族」是民族名的一部分 —— 不能在族字处截断
        ("鄂温克族自治旗", "鄂温克"),
        # 民族名后面**没有**「族」字，直接接「自治」
        ("鄂伦春自治旗", "鄂伦春"),
        # 「各族」不是单个民族名，必须单列
        ("龙胜各族自治县", "龙胜"),
        ("隆林各族自治县", "隆林"),
        # 达斡尔 / 柯尔克孜 这类长民族名
        ("莫力达瓦达斡尔族自治旗", "莫力达瓦"),
    ],
)
def test_autonomous_units_use_the_ethnic_name_cut(full: str, short: str) -> None:
    """自治单位的短名 = **第一个民族名之前的部分**。

    这是「在族字处截断」与「在民族名处截断」的区别 —— 前者会把
    `鄂温克族自治旗` 切成 `鄂温克族`（多一个「族」），后者才对。
    """
    assert place_short_name(full) == short


@pytest.mark.parametrize(
    ("full", "short"),
    [
        # 单字县名：去后缀只剩一个字，那不是戳面写法
        ("赵县", "赵县"),
        ("涉县", "涉县"),
        ("磁县", "磁县"),
        ("邱县", "邱县"),
        # 正常两字以上县名照常去后缀
        ("玉山县", "玉山"),
        ("东城区", "东城"),
        # 名字不含任何可去后缀时原样返回
        ("三清山", "三清山"),
    ],
)
def test_single_char_short_names_fall_back_to_the_full_name(full: str, short: str) -> None:
    """短名不足两个字时**保留原名**。

    实测数据里有 104 个单字县名（赵县、涉县、磁县…）。去后缀得到「赵」
    既不是戳面写法，又会与大量别的名字撞车。
    """
    assert place_short_name(full) == short


def test_describe_place_is_the_single_source_of_forms() -> None:
    """`describe_place` 是写法的**唯一产出点** —— 两条路径必须一致。

    这条挡的是一个真实发生过的 bug：`PlaceVocab` 与 `build_place_names`
    各写一遍短名规则，一条漏了显式表，于是同一棵树在两条路径上产出
    `广西壮族南宁` 与 `广西南宁` 两种不同的词表，而且不报错。
    """
    described = describe_place("广西壮族自治区")
    assert described.full == "广西壮族自治区"
    assert described.short == "广西"
    assert described.forms == frozenset({"广西壮族自治区", "广西"})

    # 索引里（值集合）与查询器（PlaceVocab）必须都认这两个写法
    province = uuid.uuid4()
    rows = [(province, None, "广西壮族自治区")]
    names = build_place_names(rows)
    maybe_known = PlaceVocab(rows)
    for form in described.forms:
        assert form in names, f"build_place_names 少了 {form!r}"
        assert maybe_known.lookup(form).known, f"PlaceVocab 不认 {form!r}"

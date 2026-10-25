"""展示层本地化（i18n）——用用户的语言渲染 Fact，但绝不改动规范的英文 predicate
（槽位键，跨语言冲突解析依赖它）。数据模型 = 规范形式；展示 = 本地化。这只是展示层：
检索、去重和冲突逻辑均不变。

auto 模式仅在 fact 内容确实包含 CJK 时（即用户用中文记录）才本地化为中文——
以英文记录的 fact（如 LongMemEval 语料）原样保留，绝不强行翻译用户的数据。
"""
from __future__ import annotations

# 规范 predicate -> 中文渲染模板。以宾语为中心的表述（主语隐含为"你"）；
# {o} = object。覆盖常用 predicate；长尾回退到可读的通用渲染。
_ZH: dict[str, str] = {
    "name": "名字是 {o}", "age": "年龄 {o}", "age_range": "年龄段 {o}", "gender": "性别 {o}",
    "occupation": "职业是 {o}", "job": "职业是 {o}", "profession": "职业是 {o}",
    "works_at": "在 {o} 工作", "employer": "在 {o} 工作", "company": "在 {o} 工作",
    "works_on": "在做 {o}", "works_in": "在 {o} 工作",
    "lives_in": "住在 {o}", "home": "家在 {o}", "based_in": "常驻 {o}", "lives_at": "住在 {o}",
    "born_in": "出生于 {o}", "birthplace": "出生于 {o}", "birthday": "生日是 {o}",
    "nationality": "国籍 {o}", "speaks": "会说 {o}", "language": "语言 {o}",
    "education": "教育：{o}", "studied_at": "就读于 {o}", "graduated_from": "毕业于 {o}", "degree": "学位 {o}",
    "married_to": "配偶是 {o}", "spouse": "配偶是 {o}", "partner": "伴侣是 {o}",
    "has_child": "有孩子 {o}", "has_children": "有孩子 {o}", "have_child": "有孩子 {o}",
    "have_children": "有孩子 {o}", "children": "孩子：{o}", "kids": "孩子：{o}",
    "job_title": "职位是 {o}", "title": "职位是 {o}", "position": "职位是 {o}", "role": "职位是 {o}",
    "friends_with": "和 {o} 是朋友", "knows": "认识 {o}", "colleague": "同事 {o}", "boss": "老板是 {o}",
    "likes": "喜欢 {o}", "loves": "很喜欢 {o}", "enjoys": "喜欢 {o}", "prefers": "偏好 {o}",
    "interested_in": "对 {o} 感兴趣", "fan_of": "是 {o} 的粉丝", "fond_of": "喜欢 {o}",
    "dislikes": "不喜欢 {o}", "hates": "讨厌 {o}", "avoids": "避免 {o}",
    "allergic_to": "对 {o} 过敏", "cannot_eat": "不能吃 {o}",
    "owns": "拥有 {o}", "has": "有 {o}", "has_pet": "养了 {o}",
    "visited": "去过 {o}", "traveled_to": "去过 {o}", "been_to": "去过 {o}", "went_to": "去过 {o}",
    "bought": "买了 {o}", "ordered": "点了 {o}", "plans": "计划 {o}", "plans_to": "计划 {o}",
    "wants": "想要 {o}", "needs": "需要 {o}", "goal": "目标：{o}", "uses": "用 {o}",
    "has_disease": "患有 {o}", "medication": "在用药 {o}", "salary": "薪资 {o}", "income": "收入 {o}",
    "plays": "会 {o}", "watches": "看 {o}", "reads": "读 {o}", "listens_to": "听 {o}",
    "studies": "学 {o}", "learns": "学 {o}", "learning": "在学 {o}", "drives": "开 {o}",
    "practices": "练 {o}", "eats": "吃 {o}", "drinks": "喝 {o}", "cooks": "做 {o}",
    "mentioned": "提到 {o}", "asked": "问过 {o}", "recommends": "推荐 {o}", "considering": "在考虑 {o}",
    "follows": "关注 {o}", "frequents": "常去 {o}", "frequently_visits": "常去 {o}", "visits": "去 {o}",
    "uses_video_platform": "用 {o}", "uses_platform": "用 {o}", "enrolled_in": "就读 {o}",
    "had": "做过 {o}", "won": "中了 {o}", "habits": "习惯 {o}", "habit": "习惯 {o}", "wants_to": "想 {o}",
    "going_to": "要去 {o}", "plans_to_travel": "计划去 {o}", "appointment": "预约 {o}",
}

# favorite_<x> -> "最喜欢的<x 的中文>是 {o}"
_FAV_SUFFIX: dict[str, str] = {
    "color": "颜色", "food": "食物", "music": "音乐", "movie": "电影", "film": "电影", "song": "歌曲",
    "drink": "饮品", "sport": "运动", "book": "书", "place": "地方", "animal": "动物", "season": "季节",
    "hobby": "爱好", "city": "城市", "artist": "歌手", "singer": "歌手", "team": "球队", "brand": "品牌",
    "programming_language": "编程语言", "cuisine": "菜系", "fruit": "水果",
}


def has_cjk(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in (s or ""))


_USER_SUBJECTS = {"user", "i", "me", "myself", "我", "你"}


def render_display(subject: str, predicate: str, obj: str, text: str = "", lang: str = "auto") -> str:
    """fact 的本地化展示字符串。`lang="auto"` 仅在 fact 内容含 CJK 时渲染中文；
    否则原样返回规范的英文 `text`。"""
    if lang == "auto":
        lang = "zh" if (has_cjk(obj) or has_cjk(subject) or has_cjk(text)) else "en"
    if lang != "zh":
        return text or f"{subject} {predicate.replace('_', ' ')} {obj}".strip()

    p = predicate.lower()
    if p in _ZH:
        body = _ZH[p].format(o=obj)
    elif p.startswith(("favorite_", "favourite_")):
        suffix = p.split("_", 1)[1]
        body = f"最喜欢的{_FAV_SUFFIX.get(suffix, suffix)}是 {obj}"
    elif p.startswith("likes_"):
        body = f"喜欢 {obj}"
    else:
        body = f"{predicate.replace('_', ' ')} {obj}"  # 长尾回退（仍可读）

    if subject.lower() in _USER_SUBJECTS:
        return body
    return f"{subject}{body}"  # 非用户主语：加上前缀（"Charles 是老板…"）


def display_of(fact, lang: str = "auto") -> str:
    # 优先使用抽取器产出的原语言表述（任何 predicate 都以源语言记录）；
    # 旧 fact / 离线抽取则回退到对规范 predicate 做本地化。
    native = (getattr(fact, "display", "") or "").strip()
    if native:
        return native
    return render_display(fact.subject, fact.predicate, fact.object, getattr(fact, "text", ""), lang)

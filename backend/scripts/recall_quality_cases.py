"""普通召回验收的固定中英文合成事实和查询。"""

from typing import NamedTuple


class FactCase(NamedTuple):
    """指定一条事实及其索引和有效期。"""

    id: str
    text: str
    indexed: bool = True
    valid_at: float | None = None
    invalid_at: float | None = None


class QueryCase(NamedTuple):
    """指定查询和唯一正确事实，缺省表示无答案。"""

    id: str
    kind: str
    text: str
    expected: str | None = None
    as_of: float | None = None


class Dataset(NamedTuple):
    """可独立写入一个空间的事实和查询。"""

    name: str
    facts: tuple[FactCase, ...]
    queries: tuple[QueryCase, ...]


_CALIBRATION = Dataset(
    "calibration",
    (
        FactCase("target-id", "MT-042 规定发布后由值班人员核对审计清单。", False),
        FactCase("similar-id", "MT-043 规定发布后值班人员记录指标快照。"),
        FactCase("other-id", "OTHER-042 是测试环境的发布记录。"),
        FactCase("en-code", "The RB-217 runbook covers switching the audit replica."),
        FactCase("en-code-near", "RB-218 is the runbook for clearing the audit cache."),
        FactCase("zh-name", "阿斯托拉负责整理跨区部署规则。", False),
        FactCase("en-name", "Vesper Quill maintains the asset ledger.", False),
        FactCase("zh-semantic", "服务器切换前必须检查变更单。"),
        FactCase("en-semantic", "Automated backups are retained for thirty days."),
        FactCase("zh-history", "鲸泊项目的部署窗口是周四上午。", True, 10.0, 30.0),
        FactCase("zh-current", "鲸泊项目的部署窗口是周二下午。", True, 30.0),
        FactCase(
            "en-history", "Pine Gate alerts went to the QA channel.", True, 1.0, 20.0
        ),
        FactCase(
            "en-current", "Pine Gate alerts go to the operations channel.", True, 20.0
        ),
        FactCase("zh-workflow", "报表导出默认采用 CSV 格式。"),
        FactCase("en-workflow", "Invoice attachments are generated as PDFs."),
        *(
            FactCase(
                f"noise-{number}",
                f"MT-{number} 发布工作之后记录审计轮次。"
                if number % 2
                else f"Release record MT-{number} captures an audit round after deployment.",
            )
            for number in range(100, 320)
        ),
    ),
    (
        QueryCase("zh-id", "identifier", "MT-042", "target-id"),
        QueryCase("en-id", "identifier", "RB-217 runbook", "en-code"),
        QueryCase("zh-name", "name", "阿斯托拉", "zh-name"),
        QueryCase("en-name", "name", "Vesper Quill", "en-name"),
        QueryCase(
            "zh-paraphrase", "paraphrase", "上线之前要核对哪些记录？", "zh-semantic"
        ),
        QueryCase(
            "en-paraphrase",
            "paraphrase",
            "How long do we keep snapshots?",
            "en-semantic",
        ),
        QueryCase("zh-history", "historical", "鲸泊项目的部署窗口", "zh-history", 29.0),
        QueryCase(
            "en-history", "historical", "Pine Gate alerts channel", "en-history", 19.0
        ),
        QueryCase("zh-current", "historical", "鲸泊项目部署窗口", "zh-current", 30.0),
        QueryCase(
            "en-current", "historical", "Pine Gate alerts channel", "en-current", 20.0
        ),
        QueryCase("zh-direct", "direct", "报表导出的格式是什么", "zh-workflow"),
        QueryCase(
            "en-direct", "direct", "Format of invoice attachments", "en-workflow"
        ),
        QueryCase("zh-id-missing", "no_answer", "MT-999"),
        QueryCase("en-id-missing", "no_answer", "RB-219 runbook"),
        QueryCase("zh-name-missing", "no_answer", "阿斯托拉的电话号码"),
        QueryCase("en-name-missing", "no_answer", "Vesper Quill office number"),
        QueryCase("zh-related-missing", "no_answer", "报表导出最多支持多少行"),
        QueryCase(
            "en-related-missing", "no_answer", "Are invoice attachments encrypted?"
        ),
        QueryCase("zh-unrelated", "no_answer", "海龟的寿命多长"),
        QueryCase("en-unrelated", "no_answer", "Height of Mount Kilimanjaro"),
    ),
)

_HOLDOUT = Dataset(
    "holdout",
    (
        FactCase("target-id", "QN-517 是备份恢复演练的手册。", False),
        FactCase("similar-id", "QN-518 是备份恢复归档时填写的表单。"),
        FactCase("other-id", "OTHER-517 借给演练组使用的文档。"),
        FactCase("en-code", "BR-726 names the archive verification procedure."),
        FactCase("en-code-near", "BR-727 names the archive rotation procedure."),
        FactCase("zh-name", "诺里维亚负责维护主分支。", False),
        FactCase("en-name", "Cedar Wren curates the on-call handbook.", False),
        FactCase("zh-semantic", "员工登录时使用单点认证。"),
        FactCase("en-semantic", "A new workspace starts with twenty seats."),
        FactCase("zh-history", "星澄服务通过邮件发送报警。", True, 10.0, 30.0),
        FactCase("zh-current", "星澄服务通过聊天群发送报警。", True, 30.0),
        FactCase(
            "en-history", "Harbor restores needed two approvals.", True, 1.0, 20.0
        ),
        FactCase("en-current", "Harbor restores need one approval.", True, 20.0),
        FactCase("zh-workflow", "会话缓存每隔十五分钟刷新。"),
        FactCase("en-workflow", "A nightly backup is kept offsite."),
        *(
            FactCase(
                f"noise-{number}",
                f"QN-{number} 归档备份演练之后记录校验轮次。"
                if number % 2
                else f"Archive record QN-{number} describes a backup verification round.",
            )
            for number in range(100, 320)
        ),
    ),
    (
        QueryCase("zh-id", "identifier", "QN-517", "target-id"),
        QueryCase("en-id", "identifier", "BR-726 verification", "en-code"),
        QueryCase("zh-name", "name", "诺里维亚", "zh-name"),
        QueryCase("en-name", "name", "Cedar Wren", "en-name"),
        QueryCase(
            "zh-paraphrase", "paraphrase", "职员如何验证自己的身份？", "zh-semantic"
        ),
        QueryCase(
            "en-paraphrase",
            "paraphrase",
            "How many people can join a newly created space?",
            "en-semantic",
        ),
        QueryCase(
            "zh-history", "historical", "星澄服务报警发送方式", "zh-history", 29.0
        ),
        QueryCase(
            "en-history", "historical", "Harbor restores approvals", "en-history", 19.0
        ),
        QueryCase(
            "zh-current", "historical", "星澄服务报警发送方式", "zh-current", 30.0
        ),
        QueryCase(
            "en-current", "historical", "Harbor restores approvals", "en-current", 20.0
        ),
        QueryCase("zh-direct", "direct", "会话缓存多久刷新", "zh-workflow"),
        QueryCase(
            "en-direct", "direct", "Where is the nightly backup kept?", "en-workflow"
        ),
        QueryCase("zh-id-missing", "no_answer", "QN-999"),
        QueryCase("en-id-missing", "no_answer", "BR-728 archive procedure"),
        QueryCase("zh-name-missing", "no_answer", "诺里维亚负责哪个时区"),
        QueryCase("en-name-missing", "no_answer", "Cedar Wren vacation schedule"),
        QueryCase("zh-related-missing", "no_answer", "会话缓存容量上限"),
        QueryCase(
            "en-related-missing",
            "no_answer",
            "What is the offsite backup recovery deadline?",
        ),
        QueryCase("zh-unrelated", "no_answer", "企鹅为什么会游泳"),
        QueryCase("en-unrelated", "no_answer", "How far apart are Earth and Mars?"),
    ),
)

DATASETS = (_CALIBRATION, _HOLDOUT)

QUALITY_NOW = 100.0
_OLD_VALID_AT = QUALITY_NOW - 90 * 86_400
SALIENCE_TARGET = "rollout-5"
SALIENCE_HISTORY_TARGET = "history-other"
SALIENCE_QUERY = QueryCase("used-rollout", "reinforced", "rollouttag", SALIENCE_TARGET)


def _ranking_facts(target_valid_at: float) -> tuple[FactCase, ...]:
    return (
        *(
            FactCase(
                f"rollout-{index}",
                f"rollouttag step {index}",
                valid_at=target_valid_at if index == 5 else _OLD_VALID_AT,
            )
            for index in range(6)
        ),
        FactCase("policy-old", "policytag enduring rule", valid_at=_OLD_VALID_AT),
        *(
            FactCase(
                f"policy-{index}",
                f"policytag recent change {index}",
                indexed=False,
                valid_at=QUALITY_NOW,
            )
            for index in range(6)
        ),
        FactCase("history-old", "historytag previous rule", valid_at=10, invalid_at=90),
        FactCase(SALIENCE_HISTORY_TARGET, "historytag secondary rule", valid_at=50),
        FactCase("history-current", "historytag replacement rule", valid_at=90),
    )


_RANKING_CONTROLS = (
    QueryCase("old-policy", "evergreen", "policytag", "policy-old"),
    QueryCase("past-rule", "historical", "historytag", "history-old", 50),
)
RECENCY_DATASET = Dataset(
    "recency",
    _ranking_facts(QUALITY_NOW),
    (
        QueryCase("recent-rollout", "recent", "rollouttag", SALIENCE_TARGET),
        *_RANKING_CONTROLS,
    ),
)
SALIENCE_DATASET = Dataset(
    "salience", _ranking_facts(_OLD_VALID_AT), (SALIENCE_QUERY, *_RANKING_CONTROLS)
)

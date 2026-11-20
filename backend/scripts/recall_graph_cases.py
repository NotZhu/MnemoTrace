"""图召回质量验收的固定合成关系和查询。"""

from typing import Literal, NamedTuple

type QueryKind = Literal["one_hop", "two_hop", "no_answer"]


class GraphFactCase(NamedTuple):
    id: str
    subject: str
    target: str
    predicate: str
    text: str
    indexed: bool = True
    valid_at: float = 10.0
    invalid_at: float | None = None


class GraphQueryCase(NamedTuple):
    id: str
    kind: QueryKind
    text: str
    expected: str | None = None
    as_of: float | None = None
    entity_names: tuple[str, ...] | None = None
    predicates: tuple[str, ...] = ()


class GraphDataset(NamedTuple):
    name: str
    facts: tuple[GraphFactCase, ...]
    queries: tuple[GraphQueryCase, ...]


class _Project(NamedTuple):
    code: str
    owner: str
    team: str
    reviewer: str
    chinese: bool
    index_team: bool


def _project_facts(project: _Project) -> tuple[GraphFactCase, ...]:
    code, owner, team, reviewer, chinese, index_team = project
    if chinese:
        ownership = f"{code} 的维护负责人是{owner}。"
        membership = f"{owner}隶属{team}。"
        review = f"{code} 的复核人员是{reviewer}。"
    else:
        ownership = f"{owner} maintains project {code}."
        membership = f"{owner} reports to {team}."
        review = f"{reviewer} reviews changes for {code}."
    return (
        GraphFactCase(f"{code}-owner", code, owner, "maintained_by", ownership),
        GraphFactCase(f"{code}-reviewer", code, reviewer, "reviewed_by", review),
        GraphFactCase(
            f"{code}-team", owner, team, "belongs_to", membership, index_team
        ),
    )


def _dataset(
    name: str, projects: tuple[_Project, ...], previous_team: str
) -> GraphDataset:
    first, *_, last = projects
    historical = GraphFactCase(
        f"{first.code}-old-team",
        first.owner,
        previous_team,
        "belongs_to",
        f"{first.owner}此前隶属{previous_team}。",
        False,
        10.0,
        50.0,
    )
    facts = tuple(fact for project in projects for fact in _project_facts(project))
    facts = tuple(
        fact._replace(valid_at=50.0) if fact.id == f"{first.code}-team" else fact
        for fact in facts
    )
    questions: list[GraphQueryCase] = []
    for code, _, _, _, chinese, _ in projects:
        owner_query = (
            f"{code} 的维护负责人是谁？" if chinese else f"Who maintains {code}?"
        )
        team_query = (
            f"{code} 的维护负责人归哪个小组？"
            if chinese
            else f"Which division handles {code}?"
        )
        questions.extend(
            (
                GraphQueryCase(
                    f"{code}-direct", "one_hop", owner_query, f"{code}-owner"
                ),
                GraphQueryCase(
                    f"{code}-division", "two_hop", team_query, f"{code}-team"
                ),
            )
        )
    questions.extend(
        (
            GraphQueryCase(
                "historical-division",
                "two_hop",
                f"{first.code} 负责人当时属于哪支队伍？",
                historical.id,
                40.0,
                predicates=("maintained_by", "belongs_to"),
            ),
            GraphQueryCase(
                "explicit-origins",
                "two_hop",
                f"Which group is responsible for {last.code}?",
                f"{last.code}-team",
                entity_names=(last.code, first.code),
                predicates=("maintained_by", "belongs_to"),
            ),
            GraphQueryCase("no-budget", "no_answer", f"{last.code} 的采购预算是多少？"),
            GraphQueryCase(
                "no-project", "no_answer", "Who manages archived workspace XZ000?"
            ),
        )
    )
    return GraphDataset(name, (*facts, historical), tuple(questions))


DATASETS = (
    _dataset(
        "calibration",
        (
            _Project("NQ410", "林梢", "交付编排组", "田岸", True, False),
            _Project("VB207", "Mira Tern", "Glacier Routing", "Quin Moss", False, True),
            _Project("LP532", "江甸", "灰度发布组", "艾兰", True, True),
            _Project(
                "RK604", "Ellis Crane", "Maple Reliability", "Cora Reed", False, False
            ),
        ),
        "终端集成组",
    ),
    _dataset(
        "holdout",
        (
            _Project("CT615", "苏汀", "票据联调组", "高宁", True, False),
            _Project(
                "MR309", "Ines Vale", "Harbor Compliance", "Nilo Park", False, True
            ),
            _Project("HD882", "魏池", "区域接入组", "石苓", True, True),
            _Project(
                "ZF742", "Ada North", "Copper Response", "Rene Frost", False, False
            ),
        ),
        "结算联络组",
    ),
)


def stress_dataset(fanout: int, other_origins: int) -> GraphDataset:
    """构造大出度起点、独占二跳目标及排在其后的普通起点。

    Args:
        fanout: 主起点无关出边的数量。
        other_origins: 参与自动扩展的其他起点数量。

    Returns:
        可独立运行的合成压力数据集。
    """
    return GraphDataset(
        name=f"fanout-{fanout}",
        facts=(
            GraphFactCase(
                "00-bridge", "hub", "lead", "maintained_by", "stresscode hub", False
            ),
            GraphFactCase(
                "target", "lead", "team", "belongs_to", "归属调度团队", False
            ),
            *(
                GraphFactCase(
                    f"other-{index:03}",
                    f"origin-{index}",
                    f"peer-{index}",
                    "maintained_by",
                    "stresscode peer",
                    False,
                )
                for index in range(other_origins)
            ),
            *(
                GraphFactCase(
                    f"noise-{index:05}",
                    "hub",
                    f"leaf-{index}",
                    "references",
                    "合成无关出边",
                    False,
                )
                for index in range(fanout)
            ),
        ),
        queries=(GraphQueryCase("second-hop", "two_hop", "stresscode", "target"),),
    )

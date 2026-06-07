from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PersonaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluatorPersonaInput(PersonaModel):
    """评测员描述的模拟用户画像。所有字段均可选，越详细场景越贴近真实测试目的。"""

    identity: str = ""
    """身份描述，如 '大学生兼职骑手' '中年男性装修工人'"""

    relationship_to_task: str = ""
    """与本次外呼任务的关系，如 '刚签约准备接单' '已合作3年老骑手'"""

    motivation: str = ""
    """参与通话的动机，如 '好奇奖励政策' '担心合同条款' '只是接了个陌生电话'"""

    attitude: str = ""
    """对话态度，如 '不耐烦、想快点挂电话' '配合但有疑问' '强势追问'"""

    communication_style: str = ""
    """沟通风格，如 '说话简短、爱用口语' '喜欢追问细节' '容易跑题'"""

    initial_focus: str = ""
    """第一句话最关注的点，如 '先问有没有奖励' '直接问是不是骗子'"""

    decision_rule: str = ""
    """做决定的依据，如 '钱给够就接单' '先看合同再决定' '不接陌生电话介绍的任务'"""

    inconvenience_context: str = ""
    """额外不方便上下文，如 '正在开车' '旁边有人不方便说话' '信号不好'"""

    extra_notes: str = ""
    """其他补充说明"""

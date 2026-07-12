"""VSM ロール向けプロンプトと出力契約。

CLI エージェントは、このモジュールで組み立てたプロンプトを
ヘッドレス実行する。各 System が個別に文面を持つと、ロール契約の
更新漏れや、次工程が扱えない確認質問への退行が起きるため、共通契約と
ロール固有契約をここへ集約する。
"""

from __future__ import annotations

from typing import Any

from vsm.roles import SystemRole

__all__ = [
    "COMMON_OUTPUT_CONTRACT",
    "ROLE_OUTPUT_CONTRACTS",
    "build_consortium_statement_prompt",
    "build_consortium_synthesis_prompt",
    "build_s1_worker_prompt",
    "build_s2_coordination_prompt",
    "build_s3star_audit_prompt",
    "build_s4_scanner_prompt",
    "build_s5_algedonic_prompt",
    "build_s5_policy_prompt",
    "render_role_prompt",
]


COMMON_OUTPUT_CONTRACT = (
    "【共通出力契約】\n"
    "日本語で応答してください。\n"
    "あなたはVSMの中の「{role}」としてヘッドレス実行されています。\n"
    "確認質問は禁止です。情報不足時は妥当な仮定を明示して前進してください。\n"
    "出力は次工程がそのまま使える形式にしてください。"
)


ROLE_OUTPUT_CONTRACTS: dict[str, str] = {
    SystemRole.S4_SCANNER.value: (
        "【S4固有の出力契約】\n"
        "環境評価を短く、次の3項目をこの順で出力してください。\n"
        "事実: 観測できた事実を1つ。\n"
        "含意: その事実が意味することを1つ。\n"
        "推奨: 次に取るべき行動を1つ。\n"
        "質問文、確認依頼、役割説明、前置きは出力しないでください。"
    ),
    SystemRole.S5_POLICY.value: (
        "【S5固有の出力契約】\n"
        "必ず実行可能な directive を1件出してください。directive は命令文とし、"
        "対象・成果物・完了条件を同じ命令に含めてください。\n"
        "directive の次に、判断根拠を1〜2行だけ出してください。\n"
        "directive 自体を質問文、確認依頼、保留表明にしてはいけません。"
    ),
    SystemRole.S1_WORKER.value: (
        "【S1固有の出力契約】\n"
        "成果物そのものを出力してください。次工程が提出物として扱える内容にします。\n"
        "メタ言及（分析した、確認した、実行を依頼する、役割を説明する等）は禁止です。\n"
        "情報が足りない場合は採用した仮定を成果物内に短く明示し、成果物を完成させてください。"
    ),
    SystemRole.S2_COORDINATOR.value: (
        "【S2固有の出力契約】\n"
        "JSON object のみを出力してください。必須キーは decision と reason です。\n"
        "decision は当事者が直ちに実行できる調停結果、reason は1〜2行の根拠にしてください。\n"
        "確認質問、選択肢だけの提示、JSON以外の説明は禁止です。"
    ),
    SystemRole.S3_ALLOCATOR.value: (
        "【S3固有の出力契約】\n"
        "資源配分の判断を、対象・割り当てる資源・実行開始条件が分かる短い命令として出力してください。\n"
        "確認質問や、判断を先送りする説明は禁止です。"
    ),
    SystemRole.S3STAR_AUDITOR.value: (
        "【S3★固有の出力契約】\n"
        "監査所見そのものを短く出力してください。観測事実・判定・必要な次のアクションを含めます。\n"
        "メタ言及や確認質問は禁止です。情報不足時は妥当な仮定を明示して監査所見を完成させてください。"
    ),
}


def render_role_prompt(
    role: SystemRole | str,
    instructions: str,
    input_text: str,
) -> str:
    """共通契約、ロール契約、入力の順で決定論的にプロンプトを組み立てる。"""

    role_name = role.value if isinstance(role, SystemRole) else role
    role_contract = ROLE_OUTPUT_CONTRACTS.get(role_name)
    if role_contract is None:
        raise ValueError(f"未定義のVSMロールです: {role_name}")
    return "\n\n".join(
        (
            COMMON_OUTPUT_CONTRACT.format(role=role_name),
            role_contract,
            instructions,
            f"【入力】\n{input_text}",
        )
    )


def build_s4_scanner_prompt(*, sub_agent_label: str, task_context: Any) -> str:
    """S4 の環境走査用プロンプトを返す。"""

    instructions = (
        f"あなたはS4の{sub_agent_label}担当です。入力から重要な観測を1件選び、"
        "指定された3項目の形式で簡潔に評価してください。"
    )
    return render_role_prompt(
        SystemRole.S4_SCANNER,
        instructions,
        f"走査対象: {task_context!r}",
    )


def build_s5_policy_prompt(*, assessment: Any) -> str:
    """S5 の assessment → directive 生成用プロンプトを返す。"""

    instructions = (
        "EnvironmentAssessment を読み、S3_Allocator が直ちに実行できる directive を"
        "1件作成してください。S4_Scanner への追加調査が必要なら、directive の対象または"
        "完了条件に含めてください。"
    )
    return render_role_prompt(
        SystemRole.S5_POLICY,
        instructions,
        f"EnvironmentAssessment: {assessment!r}",
    )


def build_s5_algedonic_prompt(
    *, severity: str, reason: str, source_node_id: str
) -> str:
    """S5 の algedonic signal 判断用プロンプトを返す。"""

    instructions = (
        "JSON object のみを返してください。必須キーは action と reason、action は"
        " suspend / consortium / escalate のいずれかです。reason は選んだ対応を"
        "直ちに実行できる根拠にしてください。"
    )
    input_text = (
        f"severity={severity}\nreason={reason}\nsource_node_id={source_node_id}"
    )
    return render_role_prompt(SystemRole.S5_POLICY, instructions, input_text)


def build_s1_worker_prompt(*, specialization: str, assignment: Any) -> str:
    """S1 の assignment 実行用プロンプトを返す。"""

    instructions = (
        f"専門領域「{specialization}」のS1_Workerとして、今回のassignmentを実行し、"
        "完了した成果物だけを返してください。"
    )
    return render_role_prompt(
        SystemRole.S1_WORKER,
        instructions,
        f"今回の指示: {assignment!r}",
    )


def build_s2_coordination_prompt(
    *,
    issue: str,
    participants: Any,
    claims: Any,
) -> str:
    """S2 の AI 調停用プロンプトを返す。"""

    instructions = (
        "係争内容と当事者の主張を比較し、実行可能な1つの決定へ調停してください。"
        "当事者の主張を無視せず、decision と reason をJSONで返してください。"
    )
    input_text = (
        f"係争: {issue}\n参加者: {participants!r}\n"
        f"当事者の主張: {claims!r}"
    )
    return render_role_prompt(SystemRole.S2_COORDINATOR, instructions, input_text)


def build_s3star_audit_prompt(*, s1_id: str, observed_state: Any) -> str:
    """S3★の S1 観測 → 監査所見生成用プロンプトを返す。"""

    instructions = (
        "S1の観測状態を監査し、問題の有無と次に取るべきアクションが分かる"
        "監査所見を1件作成してください。"
    )
    input_text = f"監査対象S1: {s1_id}\n観測状態: {observed_state!r}"
    return render_role_prompt(SystemRole.S3STAR_AUDITOR, instructions, input_text)


def build_consortium_statement_prompt(
    *,
    participant_role: SystemRole | str,
    subject: str,
    round_number: int,
    round_count: int,
) -> str:
    """Consortium の各参加者の statement 用プロンプトを返す。"""

    role_name = (
        participant_role.value
        if isinstance(participant_role, SystemRole)
        else participant_role
    )
    instructions = (
        "Consortium の参加者として、件名に対する判断案・根拠・懸念を短く述べてください。"
        "他の参加者への確認質問はせず、現時点の仮定で前進する statement を返してください。"
    )
    input_text = (
        f"件名: {subject}\nラウンド: {round_number}/{round_count}\n"
        f"参加ロール: {role_name}"
    )
    return render_role_prompt(participant_role, instructions, input_text)


def build_consortium_synthesis_prompt(*, subject: str, statements: str) -> str:
    """Consortium 招集者の synthesis 用プロンプトを返す。"""

    instructions = (
        "あなたは Consortium の招集者です。全発言を総合し、JSON object のみを"
        "返してください。コードフェンス、前置き、後置きは禁止です。必須キーは"
        "decision (string), reason (string), dissent_summary (string) です。"
        "decision は直ちに実行できる結論、reason は1〜2行、dissent_summary は"
        "反対意見の要約（なければ「なし」）にしてください。"
    )
    input_text = f"件名: {subject}\n発言:\n{statements}"
    return render_role_prompt("S5_POLICY", instructions, input_text)

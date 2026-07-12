"""VSM ロールプロンプトの出力契約を決定論的に検証する。"""

from __future__ import annotations

import pytest

from vsm.roles import SystemRole
from vsm.systems.prompts import (
    COMMON_OUTPUT_CONTRACT,
    build_consortium_statement_prompt,
    build_consortium_synthesis_prompt,
    build_s1_worker_prompt,
    build_s2_coordination_prompt,
    build_s3star_audit_prompt,
    build_s4_scanner_prompt,
    build_s5_algedonic_prompt,
    build_s5_policy_prompt,
    render_role_prompt,
)


@pytest.mark.parametrize(
    ("prompt_factory", "required_fragments"),
    [
        (
            lambda: build_s4_scanner_prompt(
                sub_agent_label="営業", task_context={"goal": "市場を調べる"}
            ),
            ("S4_SCANNER", "事実:", "含意:", "推奨:"),
        ),
        (
            lambda: build_s5_policy_prompt(
                assessment={"opportunities": ["機会"], "threats": ["脅威"]}
            ),
            ("S5_POLICY", "実行可能な directive", "対象", "成果物", "完了条件", "判断根拠"),
        ),
        (
            lambda: build_s5_algedonic_prompt(
                severity="pain", reason="停止", source_node_id="s1"
            ),
            ("S5_POLICY", "JSON object", "action", "reason"),
        ),
        (
            lambda: build_s1_worker_prompt(
                specialization="backend", assignment={"work": "実装"}
            ),
            ("S1_WORKER", "成果物そのもの", "メタ言及"),
        ),
        (
            lambda: build_s2_coordination_prompt(
                issue="競合", participants=["s1"], claims={"s1": "実行する"}
            ),
            ("S2_COORDINATOR", "JSON object", "decision", "reason", "当事者の主張"),
        ),
        (
            lambda: build_s3star_audit_prompt(
                s1_id="s1", observed_state={"completed_count": 1}
            ),
            ("S3STAR_AUDITOR", "監査所見", "観測事実", "判定"),
        ),
        (
            lambda: build_consortium_statement_prompt(
                participant_role=SystemRole.S4_SCANNER,
                subject="変更判断",
                round_number=1,
                round_count=2,
            ),
            ("S4_SCANNER", "判断案", "根拠", "懸念"),
        ),
        (
            lambda: build_consortium_synthesis_prompt(
                subject="変更判断", statements="- S4: 実施する"
            ),
            ("S5_POLICY", "招集者", "decision", "dissent_summary"),
        ),
    ],
)
def test_role_prompt_contains_common_and_role_contract(
    prompt_factory, required_fragments: tuple[str, ...]
) -> None:
    prompt = prompt_factory()

    assert "日本語で応答してください。" in prompt
    assert "確認質問は禁止です。" in prompt
    assert "情報不足時は妥当な仮定を明示して前進してください。" in prompt
    assert "出力は次工程がそのまま使える形式にしてください。" in prompt
    assert all(fragment in prompt for fragment in required_fragments)


def test_role_prompt_is_deterministic() -> None:
    args = {"assessment": {"threats": ["脅威"], "opportunities": ["機会"]}}

    assert build_s5_policy_prompt(**args) == build_s5_policy_prompt(**args)
    assert COMMON_OUTPUT_CONTRACT.format(role="S5_POLICY") in build_s5_policy_prompt(
        **args
    )


def test_unknown_role_fails_fast() -> None:
    with pytest.raises(ValueError, match="未定義のVSMロール"):
        render_role_prompt("UNKNOWN_ROLE", "指示", "入力")

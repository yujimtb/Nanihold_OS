# Role prompt 調整結果

## 実装結果

`vsm/systems/prompts.py` に、全ロール共通の出力契約とロール固有の契約を集約した。
各プロンプトは次の共通文言を必ず含む。

- 日本語で応答する。
- VSM 内の指定ロールとしてヘッドレス実行されている。
- 確認質問は禁止し、情報不足時は妥当な仮定を明示して前進する。
- 出力を次工程がそのまま使える形式にする。

既存の実行経路は維持したまま、S4 / S5 / S1 / S2 / S3★ / Consortium の
プロンプト生成を共通ビルダーへ置き換えた。S3★の監査所見も契約付きの
SubAgent 応答をそのまま finding として扱い、空の所見はエラーにする。

## ロール別契約

| ロール | 必須出力 |
| --- | --- |
| S4 Scanner | 短い `事実` → `含意` → `推奨`。質問や前置きは出さない。 |
| S5 Policy | 必ず実行可能な directive を1件。命令文に `対象`・`成果物`・`完了条件` を含め、続けて判断根拠を1〜2行出す。directive を確認質問にしない。 |
| S1 Worker | メタ説明ではなく、次工程へ渡す成果物そのものを出す。 |
| S2 Coordinator | `decision` と `reason` を持つ JSON object のみ。decision は直ちに実行できる調停結果。 |
| S3★ Auditor | 観測事実・判定・次のアクションを含む監査所見そのものを出す。 |
| Consortium statement | 判断案・根拠・懸念を短く述べ、確認質問をしない。 |
| Consortium synthesis | `decision`・`reason`・`dissent_summary` を持つ JSON object のみ。 |

## テスト結果

契約文言とロール固有の必須要素を `tests/unit/test_role_prompts.py` で決定論的に検証した。
指定された Docker Compose コマンドの結果は以下のとおり。

```text
377 passed, 1 skipped, 1 warning
```

## 実CLI再テストで確認すべき観点

Claude Code / Codex の実CLI煙テストでは、次を Event Log と各CLIの生応答で確認する。

1. S4 の応答が質問や確認依頼ではなく、`事実`・`含意`・`推奨`の3項目になっている。
2. S5 の `policy_decision.directive` が質問文ではなく命令文であり、対象・成果物・完了条件を含む。判断根拠が1〜2行で続く。
3. S5 の directive が S3 へ配送され、S1 が確認質問を指摘して停止せず、`s1_completion` と成果物を返す。
4. S1 の応答に「確認しました」「分析中です」などのメタ応答だけが残らず、成果物そのものが記録される。
5. S3★の `audit_finding.content` が観測事実・判定・次のアクションを含み、質問になっていない。
6. S2 の調停応答と Consortium 招集者の総合応答が JSON として parse でき、必要キーを満たす。
7. 1周のイベント順が、少なくとも assessment → policy decision → directive dispatch → S1 completion となり、確認質問で周回が止まらない。

この作業では、実CLIの認証情報・モデル・煙テスト対象Runが指定されていないため、Claude Code / Codex 自体の再起動は行っていない。

## 2026-07-18 追補: Consortium statement の日本語ファースト

Consortium statement を生成する実 backend prompt (`vsm/selfdev/consortium_adapter.py`) に、
statement 本文を日本語ファーストで記述し、固有名詞・コード識別子・JSON キー以外を英語だけに
しない契約を追加した。通常の runtime prompt (`vsm/systems/prompts.py`) にも同じ方針を反映した。

`tests/unit/test_selfdev_wave3.py` は selfdev backend prompt に日本語ファースト指定が含まれることと、
英語だけの statement を禁止する文言を確認する。`tests/unit/test_role_prompts.py` は通常 runtime の
statement prompt に同じ指定が含まれることを確認する。

### 検証結果

- `python -m pytest -q tests/unit/test_role_prompts.py tests/unit/test_selfdev_wave3.py`: **28 passed**。
- `python -m pytest -q`: **459 passed, 1 skipped, 3 failed**。失敗は既存のホスト環境依存で、
  Windows の CLI 実行ファイル解決期待の差異 2 件と、Windows の `os.getpgid` 非対応 1 件だった。
  日本語ファースト変更の対象テストは全て通過している。
- WSL の同じ対象テストは pytest 未導入 (`No module named pytest`) のため実行できなかった。
  Docker Compose app は今回のユーザー制約（Docker 非接触）により起動・変更していない。

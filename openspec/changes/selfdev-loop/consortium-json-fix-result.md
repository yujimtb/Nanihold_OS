# Consortium JSON 応答頑健化 修正結果

## 対応内容

- `vsm/agents/json_response.py` に、前置き・後置き・コードフェンスを含む応答から最初の完全な JSON object を抽出する共通ユーティリティを追加した。
- JSON 抽出または契約検証に失敗した場合だけ、同じ runtime／SubAgent へ新しいリクエストを1回送り、パースエラー内容を再質問へ含めるようにした。2回目の失敗は例外を伝播し、内容を補う fallback は追加していない。
- selfdev Consortium participant のプロンプトに、JSON object のみ・コードフェンス／前置き／後置き禁止・`{"statement":"string"}` スキーマを明記した。
- selfdev Consortium の participant／synthesis と S3★ audit の raw response を proposal の `artifacts` 配下へ保存するようにした。participant の基本名は `raw-statement-{participant}-{round}.txt`、衝突時は `-retry` を付ける。
- 通常 Consortium の synthesis、S2 coordination、S5 algedonic の厳格 JSON 応答にも同じ抽出＋1回再質問を適用した。

## テスト

追加・更新した決定論テスト:

- 前置き＋コードフェンス付き JSON の抽出
- selfdev participant の破損応答 → 再質問 → コードフェンス付き JSON 成功
- 再質問後も不正な場合は呼び出し回数が2回で終了
- S3★ audit のコードフェンス付き応答と raw artifact 保存

Docker Compose app は起動したが、Compose の `/workspace` が指定された Windows 作業ツリーではなく別の古い WSL checkout をマウントしているため、対象テスト指定は `file or directory not found` で実行できなかった。app 内での `compileall` もその別 checkout に対する実行結果であり、本修正の検証結果とはみなしていない。最終的な対象テスト＋`compileall`＋全テストは、人間側の正しい WSL checkout／Compose mount で実施する。

git commit は作成していない。

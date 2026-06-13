# runtime-resilience(N-4)

## ADDED Requirements

### Requirement: 常駐と自動再起動

システムは、systemd unit(`Restart=on-failure`)で常駐し、プロセス強制終了後に自動再起動しなければならない (SHALL)。

#### Scenario: kill -9 から復旧する
- **WHEN** プロセスを `kill -9` する
- **THEN** 30秒以内に自動再起動する

### Requirement: replay による projection 再構築

システムは、起動時に Event_Log を replay して projection を再構築しなければならない (SHALL)。

#### Scenario: 再起動で状態が戻る
- **WHEN** 再起動する
- **THEN** projection が Event_Log から再構築される

### Requirement: stale ロックの回収と二重実行防止

システムは、起動時に `RUNNING` のまま残った stale ロックを回収し、プロセス死亡を検出した場合は interrupted イベントを発行して二重実行を防がなければならない (SHALL)。

#### Scenario: 中断が正しく記録される
- **WHEN** 実行中の Run があるまま `kill -9` する
- **THEN** その Run が「中断」として記録され、二重実行が起きない(idempotency)

#### Scenario: 電源断でも破損しない
- **WHEN** コンテナを強制停止する(電源断シミュレーション)
- **THEN** Event_Log が破損しない

### Requirement: 外部死活監視

システムは、LETHE と Nanihold 双方の `/health` を外形監視し、ダウン時に Discord/メール通知しなければならない (SHALL)。

#### Scenario: ダウンが通知される
- **WHEN** `/health` が応答しなくなる
- **THEN** Discord/メールに通知が届く

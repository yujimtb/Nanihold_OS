import { expect, test, type Route } from "@playwright/test";

const proposalId = "proposal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const effectId = "run:recovery-0";

const detail = () => ({
  schema_version: 1,
  proposal_id: proposalId,
  title: "in-doubt の裁定を確認する",
  state: "WORKSPACE_READY",
  pause_causes: [
    { pause_id: "pause-recovery-1", kind: "SUSPEND", actor_type: "controller", actor_id: "controller", pool_id: null, reset_at: null, source_event_id: "event-1", reason: "外部事実の確認が必要" },
    { pause_id: "pause-recovery-2", kind: "SUSPEND", actor_type: "controller", actor_id: "controller", pool_id: null, reset_at: null, source_event_id: "event-2", reason: "外部事実の確認が必要" },
  ],
  state_version: 3,
  risk_class: "normal",
  active_run_id: null,
  pending_action: "resume",
  updated_at: "2026-07-13T10:00:00.000Z",
  proposal: {
    id: proposalId,
    title: "in-doubt の裁定を確認する",
    motivation: "外部事実を人間が裁定する",
    scope: [{ path: "docs/", kind: "tree" }],
    acceptance_criteria: [{ id: "AC-1", statement: "裁定できる", verifier: { kind: "path_exists", path: "docs" } }],
    risk_class: "normal",
    budget_estimate: { tokens: 100, active_wall_clock_seconds: 60, pool_quota: [] },
  },
  proposal_manifest_sha256: "a".repeat(64),
  protected_scope_sha256: "b".repeat(64),
  integrity_failed: false,
  integrity_resolved: false,
  integrity_reason: null,
  in_doubt_effects: [{ effect_id: effectId, effect_kind: "run", input_sha256: "1".repeat(64), invoked_at: "2026-07-13T09:59:00.000Z", invocation_event_id: "invoke-1" }],
  transitions: [],
  consortium_reviews: [],
  implementation_runs: [],
  gate_attempts: [],
  audit_report: null,
  budget_actual: {},
  artifacts: [],
  candidate: null,
  pr_description: null,
  last_error: null,
});

test("自己開発タブから in-doubt 効果を completed 裁定できる", async ({ page }) => {
  let currentDetail = detail();
  let decisionBody: Record<string, unknown> | undefined;

  await page.route("**/api/runs", async (route) => route.fulfill({ status: 200, json: [] }));
  await page.route("**/api/config", async (route) => route.fulfill({ status: 200, json: { demo_mode: true, runtimes: [] } }));
  const fulfillList = async (route: Route) => route.fulfill({
    status: 200,
    json: { items: [{ proposal_id: proposalId, title: currentDetail.title, state: currentDetail.state, pause_causes: currentDetail.pause_causes, state_version: currentDetail.state_version, risk_class: "normal", active_run_id: null, pending_action: "resume", updated_at: currentDetail.updated_at }] },
  });
  await page.route("**/api/selfdev/proposals", fulfillList);
  await page.route("**/api/selfdev/proposals?*", fulfillList);
  await page.route(`**/api/selfdev/proposals/${proposalId}`, async (route) => route.fulfill({ status: 200, json: currentDetail }));
  await page.route(`**/api/selfdev/proposals/${proposalId}/human-decision`, async (route) => {
    decisionBody = JSON.parse(route.request().postData() || "{}");
    currentDetail = { ...currentDetail, state_version: currentDetail.state_version + 1, in_doubt_effects: [], pause_causes: [] };
    await route.fulfill({ status: 202, json: { accepted: true, decision: "completed", effect_id: effectId, state: currentDetail.state, state_version: currentDetail.state_version, in_doubt_effects: [], pause_causes: [] } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "自己開発" }).click();
  await expect(page.getByText("in-doubt 効果の人間裁定")).toBeVisible();
  await expect(page.getByText(effectId)).toBeVisible();
  await page.getByPlaceholder("操作理由（裁定・操作に必須）").fill("外部事実を記録と照合した");
  await page.getByRole("button", { name: "completed" }).click();
  await expect.poll(() => decisionBody).toMatchObject({ decision: "completed", effect_id: effectId, reason: "外部事実を記録と照合した" });
  await expect(page.getByText("in-doubt 効果の人間裁定")).toHaveCount(0);
});

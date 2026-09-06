from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GROUP = ROOT / "docs" / "acceptance" / "2026-08-30-weekend-rc"
LEGACY_REDIRECT = ROOT / "docs" / "wip" / "weekend-release-readiness-and-zero-known-defects-2026-08-25.md"
ISSUE_TEMPLATE_GROUP = ROOT / ".github" / "ISSUE_TEMPLATE"
WEEKEND_WORK_PACKET = ISSUE_TEMPLATE_GROUP / "weekend_rc_work_packet.yml"
PRODUCTION_MANIFEST = ROOT / "acceptance" / "weekend_production_journeys.v1.json"
PRODUCTION_GATE = ROOT / "backend" / "scripts" / "weekend_rc_gate.py"

REQUIRED_FILES = (
    "README.md",
    "01-north-star-and-boundaries.md",
    "02-owner-decisions.md",
    "03-current-status.md",
    "04-journey-ledger.md",
    "05-findings.md",
    "06-runbook-and-release-gates.md",
    "domains/single-agent-and-session.md",
    "domains/memory-knowledge-and-growth.md",
    "domains/hr-identity-and-permissions.md",
    "domains/collaboration-workflow-and-a2a.md",
    "domains/automation-hooks-and-capabilities.md",
    "domains/frontend-and-product-consumption.md",
    "evidence/README.md",
    "archive/README.md",
    "archive/legacy-ledger-2026-08-25.md",
)

REQUIRED_METADATA = {
    "document_id",
    "owner",
    "status",
    "authority",
    "last_reviewed",
    "source_commit",
    "verification_status",
}


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{path.relative_to(ROOT)} has no frontmatter"
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError(f"{path.relative_to(ROOT)} has unterminated frontmatter") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        assert separator and key.strip() and value.strip(), (
            f"{path.relative_to(ROOT)} has invalid frontmatter line: {line!r}"
        )
        metadata[key.strip()] = value.strip()
    return metadata


def _active_documents() -> list[Path]:
    return sorted(path for path in GROUP.rglob("*.md") if "archive" not in path.relative_to(GROUP).parts)


def test_weekend_acceptance_document_group_is_complete_and_indexed() -> None:
    missing = [relative for relative in REQUIRED_FILES if not (GROUP / relative).is_file()]
    assert not missing, f"missing Weekend RC documents: {missing}"

    index = (GROUP / "README.md").read_text(encoding="utf-8")
    for relative in REQUIRED_FILES[1:15]:
        assert f"]({relative})" in index, f"README.md does not index {relative}"


def test_active_documents_have_unique_authority_metadata() -> None:
    documents = _active_documents() + [GROUP / "archive" / "README.md", LEGACY_REDIRECT]
    document_ids: list[str] = []
    for path in documents:
        metadata = _frontmatter(path)
        assert REQUIRED_METADATA <= metadata.keys(), (
            f"{path.relative_to(ROOT)} missing metadata: {sorted(REQUIRED_METADATA - metadata.keys())}"
        )
        document_ids.append(metadata["document_id"])

    assert len(document_ids) == len(set(document_ids)), "document_id values must be unique"


def test_active_documents_stay_bounded_and_legacy_history_stays_archived() -> None:
    line_budgets = {
        "README.md": 160,
        "03-current-status.md": 220,
    }
    for path in _active_documents():
        relative = path.relative_to(GROUP).as_posix()
        budget = line_budgets.get(relative, 500)
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= budget, f"{relative} has {line_count} lines; budget is {budget}"

    archive = GROUP / "archive" / "legacy-ledger-2026-08-25.md"
    archive_text = archive.read_text(encoding="utf-8")
    assert len(archive_text.splitlines()) >= 5_000
    assert "历史档案，不是当前恢复入口" in archive_text
    assert len(LEGACY_REDIRECT.read_text(encoding="utf-8").splitlines()) <= 40
    assert "../acceptance/2026-08-30-weekend-rc/README.md" in LEGACY_REDIRECT.read_text(encoding="utf-8")


def test_active_markdown_links_resolve() -> None:
    markdown_link = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    documents = _active_documents() + [GROUP / "archive" / "README.md", LEGACY_REDIRECT]
    failures: list[str] = []

    for path in documents:
        for target in markdown_link.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative_target = target.split("#", 1)[0]
            if not relative_target:
                continue
            resolved = (path.parent / relative_target).resolve()
            if not resolved.exists():
                failures.append(f"{path.relative_to(ROOT)} -> {target}")

    assert not failures, "broken Weekend RC Markdown links:\n" + "\n".join(failures)


def test_journey_ledger_preserves_ci_ids_and_has_one_frozen_denominator() -> None:
    ledger = (GROUP / "04-journey-ledger.md").read_text(encoding="utf-8")
    ci_ids = re.findall(r"^\| (J-\d{2}) \|", ledger, flags=re.MULTILINE)
    candidate_ids = re.findall(r"^\| (PJ-\d{2}) \|", ledger, flags=re.MULTILINE)

    assert ci_ids == [f"J-{index:02d}" for index in range(1, 16)]
    assert candidate_ids == [f"PJ-{index:02d}" for index in range(1, 36)]
    assert PRODUCTION_MANIFEST.is_file()
    assert "verification_status: frozen-96-p01-negative-workspace-path-governance-order-breakpoint" in ledger
    assert "共 **96** 条可独立计分的 production journeys" in ledger
    assert "weekend_production_journeys.v1.json" in ledger
    assert "0/96 Closed；NPTCR 0%" in ledger


def test_structural_checks_do_not_claim_semantic_acceptance() -> None:
    index = (GROUP / "README.md").read_text(encoding="utf-8")
    evidence_contract = (GROUP / "evidence" / "README.md").read_text(encoding="utf-8")
    production_gate = PRODUCTION_GATE.read_text(encoding="utf-8")
    assert "结构检查只验证文件、ID、链接、字段" in index
    assert "不判断语义质量" in index
    assert "frozen-machine-contract-production-evidence-active" in evidence_contract
    assert '"semantic_verdict": "not_computed_by_tool"' in production_gate


def test_execution_control_contract_is_explicit_and_non_semantic() -> None:
    decisions = (GROUP / "02-owner-decisions.md").read_text(encoding="utf-8")
    index = (GROUP / "README.md").read_text(encoding="utf-8")
    north_star = (GROUP / "01-north-star-and-boundaries.md").read_text(encoding="utf-8")
    ledger = (GROUP / "04-journey-ledger.md").read_text(encoding="utf-8")
    runbook = (GROUP / "06-runbook-and-release-gates.md").read_text(encoding="utf-8")
    automation = (GROUP / "domains" / "automation-hooks-and-capabilities.md").read_text(encoding="utf-8")
    evidence = (GROUP / "evidence" / "README.md").read_text(encoding="utf-8")
    findings = (GROUP / "05-findings.md").read_text(encoding="utf-8")

    assert "Kimi Code 负责前端，zCode 负责后端" in decisions
    assert "Codex 是唯一验收总控" in decisions
    assert "当前 `agent-delegation` Skill 是唯一派发协议" in decisions
    assert "保留为历史记录，已分别由 PDEC-005/PDEC-007 覆盖" in decisions
    assert "允许 Codex 原生 Multi-Agent 与 subagent" in decisions
    assert "缺少预存会话或 fixture 不是 owner gate" in decisions
    assert "执行分工与外部 Harness 禁令已由 2026-09-04 PDEC-012 替代，不再生效" in decisions
    assert "CC（Claude Code）先独立审查" in decisions
    assert "核对结论并补充遗漏" in decisions
    assert "只有重大节点额外进行 Codex 与 CC 双向对抗性审查" in decisions
    assert "挑战方案、对账证据，解决阻塞发现并收敛结论后推进" in decisions
    assert "不把对抗流程套到每次小改动，不增加第二 controller" in decisions
    assert "此决定明确替代 PDEC-007 与旧 Goal 中 single-Codex/禁止外部代理的分工条款" in decisions
    assert "worker 不 commit/push/deploy、不自验收" in decisions
    assert "GitHub Issue 只是" in index
    assert "都不是 Journey/Finding verdict" in index
    assert "PDEC-012 替代旧执行分工" in index
    assert "worker/reviewer 的独立判断是审查意见，不直接改写 Journey verdict" in index
    assert "本轮执行 PDEC-012/PDEC-014" in runbook
    assert "zCode 负责后端及功能实现，Kimi Code 负责前端 UI" in runbook
    assert "CC 不可用、限额或等待不再阻断进度" in runbook
    assert "作者自审不能占据交叉审查席位" in runbook
    assert "只有重大节点额外启动对抗性证据对账" in runbook
    assert "PDEC-014" in decisions
    assert "CC 不再是进度前置门" in decisions
    assert "实现者不能把自审算作独立 review" in decisions
    assert "旧 single-Codex 与禁用外部代理的分工已被替代" in runbook
    assert "zCode/Kimi/CC 不 commit/push/deploy、不持有生产凭据、不做生产 effects 或最终验收" in runbook
    assert "产品 turn 的 selected runtime LLM 负责任务语义" in runbook
    assert "主 Codex负责验收语义" in runbook
    assert "不得决定 semantic truth、quality、failure、`blocked`、priority" in runbook
    assert "只经受支持、经过认证的 product/control-plane path" in runbook
    assert "禁止 forged claim/JWT/token" in runbook
    assert "denial 只阻断该操作" in runbook
    assert "已登记 read-only deny/not-found/existence probe 必须继续" in runbook
    assert "若意外返回 protected bytes，立即停止该 lane" in runbook
    assert "owner 指令不能把未授权访问变成授权" in runbook
    assert "缺少预存身份、fixture、Session 或仓库内 runtime/build/adapter" in runbook
    assert "不属于停止条件" in runbook
    assert "不设人工 Goal-wide timeout、step cap 或 attempt cap" in runbook
    assert "expiry 只结束或恢复当前 attempt" in runbook
    assert "每个 worker 调用必须满足" not in runbook
    assert "不拥有 Journey、Finding、产品质量或最终语义 verdict" in runbook
    assert (
        "只有未解决的 Hive/product-controlled requirement 才能记录 blocking fact `BLOCKED_PRECONDITION`" in north_star
    )
    assert "Journey completion state 只使用" in north_star
    assert "`BLOCKED_PRECONDITION` 与 `EXTERNAL_UNAVAILABLE`" in north_star
    assert "Breakpoint / IMPLEMENTATION_QUEUED" in ledger
    assert "Breakpoint / RECOVERY_QUEUED" in ledger
    assert "P33-DEEPSEEK 保持未闭环" in ledger
    assert "本 Goal 创建并登记的合成资产 cleanup 已授权" in decisions
    assert "全部 in-scope 冻结旅程" in ledger
    assert "全部 in-scope 冻结旅程" in runbook
    assert "Agent 智能 → 全部前后端功能可用 → 权限/RLS/安全 → Release" in decisions
    assert "最小真实 Session 中的单 Agent 智能" in runbook
    assert "完整 Session streaming/20 commands" in runbook
    assert "功能未完成时先修功能，不以安全工作掩盖" in runbook
    assert "权限加固、RLS 扩张和安全评分不得提前阻断无关功能补全" in runbook
    assert "当前 manifest 的 `P01-MAIN` 已有两次clean signed-in pass" in index
    assert "1/96 条有 current-manifest pass 1" in ledger
    assert "1/96 条完成current-manifest signed-in双遍" in ledger
    assert "真实 external provider/bridge 不可用时单列 `EXTERNAL_UNAVAILABLE`" in automation
    assert "真实 external provider 或 bridge 不可用时诚实 `BLOCKED_PRECONDITION`" not in automation
    assert "PDEC-008 已授权 Example Owner 实验 tenant synthetic scope" in evidence
    assert "超出该 scope 的写入才需要 owner action-time authorization" in evidence
    assert "不是 Journey completion state" in evidence
    assert "UI-CMD-003 | Fix Candidate" in findings
    assert "production verification pending" in findings
    assert "fix commit `1b4be5d2`" in findings
    assert "余额/auth/rate-limit/offline 是 `BLOCKED_PRECONDITION`" not in decisions
    assert "不降低产品 NPTCR" not in decisions
    for decision_id in tuple(f"PDEC-{index:03d}" for index in range(1, 13)):
        assert decision_id in decisions
    assert "一个可独立回滚的共享根因对应一个 Codex integration commit" in runbook
    assert "新增纯 evidence/docs commit `E`" in runbook


def test_active_runbook_and_index_do_not_restore_external_agent_prohibition() -> None:
    # PDEC-012 replaced the external-agent prohibition with the two-level review
    # contract. These exact operative prohibition formulations must not return to
    # the active runbook or index; superseded-decision mentions elsewhere (for
    # example 02-owner-decisions.md and the runbook's "已被替代" sentence) stay
    # allowed, so the ban is scoped to these two active routing documents only.
    # Ceiling: this check catches only these two exact literals — reworded
    # prohibition language is not detected here, and no semantic wording scanner
    # is added; PDEC-012 wording review owns that judgment.
    prohibited = ("外部 Harness 仍禁用", "external agent harnesses are prohibited")
    for relative in ("06-runbook-and-release-gates.md", "README.md"):
        text = (GROUP / relative).read_text(encoding="utf-8")
        for phrase in prohibited:
            assert phrase not in text, f"{relative} restores the superseded external-agent prohibition: {phrase!r}"


def test_weekend_work_packet_template_matches_current_repository_and_boundaries() -> None:
    assert WEEKEND_WORK_PACKET.is_file()
    packet = WEEKEND_WORK_PACKET.read_text(encoding="utf-8")
    issue_templates = "\n".join(path.read_text(encoding="utf-8") for path in sorted(ISSUE_TEMPLATE_GROUP.glob("*.yml")))

    assert "https://github.com/SteamRocket-Labs/hiveclaw/issues" in issue_templates
    assert 'labels: ["rc:weekend"]' in packet
    for field_id in (
        "finding_id",
        "journey_ids",
        "execution_owner",
        "base_commit",
        "objective",
        "reproduction",
        "scope",
        "validation",
        "authority",
    ):
        assert re.search(rf"^    id: {field_id}$", packet, flags=re.MULTILINE)

    assert "zCode — backend and functional implementation" in packet
    assert "Kimi Code — frontend UI and interaction" in packet
    assert "Claude Code — first independent review" in packet
    assert "Primary Codex — subsequent independent review and integration" in packet
    assert (
        "Worker results remain candidates; Claude Code reviews first, then primary Codex "
        "independently checks code, evidence, conclusions and missed issues before integration."
    ) in packet
    assert "Use the PDEC-012 agent-delegation roles without a second semantic controller" in packet
    assert "grants no production, credential, billing, destructive" in packet


def test_active_markdown_fences_are_balanced() -> None:
    documents = _active_documents() + [GROUP / "archive" / "README.md", LEGACY_REDIRECT]
    failures = [
        str(path.relative_to(ROOT))
        for path in documents
        if sum(line.startswith("```") for line in path.read_text(encoding="utf-8").splitlines()) % 2
    ]
    assert not failures, f"unbalanced Markdown fences: {failures}"

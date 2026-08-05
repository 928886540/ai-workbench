from llm_core.structured import ProjectBrief


def test_project_brief_schema() -> None:
    brief = ProjectBrief(
        name="ai-workbench",
        purpose="Learn AI engineering by building real systems",
        stage="bootstrap",
        next_actions=["finish llm-core", "start code-agent"],
    )
    assert brief.stage == "bootstrap"
    assert len(brief.next_actions) == 2

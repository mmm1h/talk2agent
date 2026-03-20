import pytest

from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
)


@pytest.mark.asyncio
async def test_auto_approve_prefers_allow_once():
    from talk2agent.acp.permission import AutoApprovePermissionPolicy

    policy = AutoApprovePermissionPolicy()
    options = [
        PermissionOption(kind="allow_always", name="Always", optionId="always"),
        PermissionOption(kind="allow_once", name="Once", optionId="once"),
    ]

    response = await policy.decide(
        session_id="session-1",
        options=options,
        tool_call=object(),
    )

    assert response == RequestPermissionResponse(
        outcome=AllowedOutcome(optionId="once", outcome="selected")
    )


@pytest.mark.asyncio
async def test_auto_approve_denies_when_no_allow_option():
    from talk2agent.acp.permission import AutoApprovePermissionPolicy

    policy = AutoApprovePermissionPolicy()
    options = [
        PermissionOption(kind="reject_once", name="Reject once", optionId="reject-once"),
        PermissionOption(kind="reject_always", name="Reject always", optionId="reject-always"),
    ]

    response = await policy.decide(
        session_id="session-2",
        options=options,
        tool_call=object(),
    )

    assert response == RequestPermissionResponse(
        outcome=DeniedOutcome(outcome="cancelled")
    )


@pytest.mark.asyncio
async def test_auto_approve_selects_allow_always_when_allow_once_is_absent():
    from talk2agent.acp.permission import AutoApprovePermissionPolicy

    policy = AutoApprovePermissionPolicy()
    options = [
        PermissionOption(kind="reject_once", name="Reject once", optionId="reject-once"),
        PermissionOption(kind="allow_always", name="Always", optionId="always"),
    ]

    response = await policy.decide(
        session_id="session-3",
        options=options,
        tool_call=object(),
    )

    assert response == RequestPermissionResponse(
        outcome=AllowedOutcome(optionId="always", outcome="selected")
    )

from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse


class AutoApprovePermissionPolicy:
    async def decide(self, session_id, options, tool_call):
        selected = next((option for option in options if option.kind == "allow_once"), None)
        if selected is None:
            selected = next((option for option in options if option.kind == "allow_always"), None)
        if selected is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(optionId=selected.option_id, outcome="selected")
        )

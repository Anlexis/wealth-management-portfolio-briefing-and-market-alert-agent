"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, the gateway calls agent.invoke() directly.

import os
import secrets
from typing import Any, Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from shared.secrets import factory as secrets_factory
from src.graph.graph import Graph, load_runtime_config
from src.services.security import (
    ContextValidationError,
    mask_field_name,
    validate_caller_context,
)

app = FastAPI(title="Agent")

# Runtime parameters come from config/config.yaml. Constructing the graph bare
# would leave self.config empty, so every declared value (max_retry, timeout_s,
# the alert thresholds, the structural caps) would be inert on this entry point
# while still appearing in the shipped configuration.
agent = Graph(config=load_runtime_config())
agent.compile()
# namespace/agent_name match the manifest values in config/agent.yaml.
agent.provision_secrets(secrets_factory(namespace="fin", agent_name="WealthManagementPortfolioBriefingAgent"))

# Adapter-level caps: the request body is bounded before anything downstream
# sees it. PreProcessNode caps the payload again — the node, not the entry
# point, owns the contract, and this agent must hold its guarantees wherever it
# is mounted.
MAX_INPUT_CHARS = 200_000
MAX_CONTEXT_BYTES = 4_096


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    input_context: Dict[str, Any] = Field(default_factory=dict)


def _screen_context_credentials(context: Dict[str, Any]) -> None:
    """Refuse a credential-shaped value in input_context, naming the FIELD.

    The framework's InitializeNode returns input_context verbatim in its result,
    and the @final S-3 gate scans every value of every result — so a
    credential-shaped string anywhere on this channel makes the FIRST node
    return an error with a traceback, before any template code runs. The
    request cannot succeed either way; refusing here converts an opaque node-1
    failure into a 400 the caller can act on.

    detect_credentials_in_value over a dict is defined as the union over its
    values, so scanning field by field blocks exactly the same set the
    framework does — no local approximation that could drift wider or narrower.
    The detector scans values only, never keys; this screen matches that on
    purpose.
    """
    for index, (name, value) in enumerate(context.items()):
        if detect_credentials_in_value(value):
            safe = mask_field_name(name)
            where = f"input_context.{safe}" if safe != "<masked field name>" else f"input_context field #{index + 1}"
            # 400, not 422: pydantic owns 422 and answers with a list of error
            # objects there, so reusing it makes client handling ambiguous.
            raise HTTPException(
                status_code=400,
                detail=f"{where} contains a credential-shaped value.",
            )


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    if len(req.input) > MAX_INPUT_CHARS:
        raise HTTPException(status_code=400, detail=f"input exceeds {MAX_INPUT_CHARS} characters.")
    if len(str(req.input_context).encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise HTTPException(status_code=400, detail=f"input_context exceeds {MAX_CONTEXT_BYTES} bytes.")
    # Validate BEFORE the credential screen so unknown keys are refused rather
    # than merely ignored: an ignored key is not a stripped key — it survives
    # into state["input_context"] and detonates on the first node.
    try:
        input_context = validate_caller_context(req.input_context)
    except ContextValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    _screen_context_credentials(input_context)

    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted. This
    # adapter is the entry-point auth boundary — a deployment-level caller
    # credential, not an agent secret, so ctx.secrets does not apply: no
    # InvocationContext exists before auth runs.
    #
    # INVOKE_AUTH_TOKEN is REQUIRED for this agent, not optional. Every node
    # requires VERIFIED_EXTERNAL (the manifest's declared entry contract), so
    # with no token configured every caller stays ANONYMOUS and the trust gate
    # refuses the request before any briefing work happens.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of a clean 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was
            # absent, malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        envelope: Dict[str, Any] = agent.invoke(req.input, ctx=ctx, input_context=input_context)
        return envelope


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "WealthManagementPortfolioBriefingAgent"}

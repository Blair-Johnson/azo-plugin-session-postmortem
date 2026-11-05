"""Observe one synchronous native call without altering its real text response.

``require_body()`` returns a deep copy of the cache.dispatch envelope, never an
engine page or a parsed preview. Retrieval envelopes contain document/text/path/
preview_chars; edit envelopes contain document (operation/ack) and preview_chars.
``render_document`` and ``buffer_id`` are the actual render_preview inputs.
The probe is internal instrumentation, not a native structured-output mode.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
from unittest.mock import patch


@dataclass(frozen=True)
class NativeResponse:
    body: dict | None
    text: str
    error: bool
    render_document: dict | None = None
    buffer_id: str = ""

    def require_body(self):
        """Return the cache envelope; its document is under ``['document']``."""
        if self.error:
            raise RuntimeError(self.text)
        if self.body is None:
            raise AssertionError("Successful native call did not dispatch a cache envelope")
        return self.body


def invoke(state, name, arguments, *, call_id="native-probe"):
    """Observe the real cache envelope and preview inputs; preserve ToolCall.result."""
    from agent_utils import ToolCall
    from azo_session_postmortem import native

    call = ToolCall(id=call_id, name=name, raw_args=json.dumps(arguments),
                    parsed_args=arguments)
    state.pending_tool_calls = [call]
    envelopes = []
    dispatch = native.cache.dispatch

    def observe_dispatch(*args, **kwargs):
        envelope = dispatch(*args, **kwargs)
        envelopes.append(deepcopy(envelope))
        return envelope

    with patch.object(native.cache, "dispatch", side_effect=observe_dispatch), \
            patch.object(native.cache_render, "render_preview",
                         wraps=native.cache_render.render_preview) as observed:
        getattr(native, name)(state)
    assert isinstance(call.result, str), "Native responses must be text"
    body, document, buffer_id = None, None, ""
    if not call.error:
        assert len(envelopes) == 1, "Successful native call must dispatch once"
        assert observed.call_count == 1, "Successful native call must render once"
        body = envelopes[0]
        document, buffer_id, budget = observed.call_args.args
        assert document == body["document"] and budget == body["preview_chars"]
        assert document["operation"] == name
        document = deepcopy(document)
    return NativeResponse(body=body, text=call.result, error=bool(call.error),
                          render_document=document, buffer_id=buffer_id)

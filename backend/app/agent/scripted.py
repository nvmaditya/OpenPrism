"""Scripted chat model for tests (tool_calls without live API)."""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr


class ScriptedChatModel(BaseChatModel):
    """
    Returns a fixed sequence of AIMessages (including tool_calls).

    Used so agent/graph tests need no API key.
    """

    script: list[AIMessage] = Field(default_factory=list)
    _i: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "scripted-prism"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        # Tools are executed by the agent runtime; script already names them.
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self._i < len(self.script):
            msg = self.script[self._i]
            self._i += 1
        else:
            msg = AIMessage(content="(script exhausted)")
        return ChatResult(generations=[ChatGeneration(message=msg)])

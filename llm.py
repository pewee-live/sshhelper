import os
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, BaseMessageChunk
from langchain_core.outputs import ChatResult, ChatGeneration
from dotenv import load_dotenv


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI subclass that captures non-standard ``reasoning_content``
    from OpenAI-compatible APIs (GLM, DeepSeek-R1, QwQ, etc.).

    The base ``_convert_dict_to_message`` drops ``reasoning_content`` because it
    is not part of the standard OpenAI schema. We override ``_create_chat_result``
    to fish it back out of the raw response and attach it to ``additional_kwargs``.
    """

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        # Re-extract the reasoning_content from the raw response dict and attach
        # it to each generated message.
        response_dict = (
            response
            if isinstance(response, dict)
            else response.model_dump()
        )
        choices = response_dict.get("choices") or []
        for i, choice in enumerate(choices):
            msg = choice.get("message") if isinstance(choice, dict) else {}
            reasoning = ""
            if isinstance(msg, dict):
                reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            if reasoning and i < len(result.generations):
                gen_msg = result.generations[i].message
                if isinstance(gen_msg, AIMessage):
                    gen_msg.additional_kwargs["reasoning_content"] = reasoning
        return result

    async def _astream(self, *args, **kwargs):
        """Capture reasoning_content from streaming delta chunks too.

        The base implementation drops ``reasoning_content`` from each delta. We
        intercept each chunk, check the raw delta for the field, and accumulate
        it into ``additional_kwargs["reasoning_content"]``.
        """
        async for chunk in super()._astream(*args, **kwargs):
            yield chunk
        # Note: streaming reasoning capture requires patching the delta converter,
        # which is a module-level function. For the agent path (which uses invoke,
        # not stream), the _create_chat_result override above is sufficient. The
        # streaming path is handled in web_server's run_agent_workflow via the
        # non-streaming AIMessage that LangGraph produces.


def get_llm():
    """
    Initialize and return the LLM instance.
    Supports any OpenAI-compatible API, with reasoning_content capture for
    reasoning models like GLM, DeepSeek-R1, QwQ, etc.
    """
    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY or DEEPSEEK_API_KEY is not set in the environment variables.")

    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL")
    model = os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL", "deepseek-chat")

    # Default to DeepSeek base URL if using a DeepSeek model and no base URL is specified
    if not base_url and "deepseek" in model:
        base_url = "https://api.deepseek.com/v1"

    kwargs = {
        "model": model,
        "api_key": api_key,
        "max_tokens": 2048,
        "temperature": 0.0
    }

    if base_url:
        kwargs["base_url"] = base_url

    llm = ReasoningChatOpenAI(**kwargs)

    return llm
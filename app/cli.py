from __future__ import annotations

import asyncio

from app.agent.react_agent import ReactAgent


def _print_text_response(response) -> None:
    text = getattr(response, "text", None)
    if text:
        print(text)
        return
    print(str(response))


async def run_interactive() -> int:
    agent = ReactAgent()
    session_id = "default"

    if not agent._framework_ready():
        print("agent-framework 未就绪，请先配置 Azure OpenAI（ENDPOINT/API_KEY/DEPLOYMENT）。")
        return 2

    agent.get_session(session_id)
    print("Azure Support Agent（agent-framework）")
    print("- 输入 exit 退出")
    print("- 普通输入：非流式调用")
    print("- 输入 /stream 你的问题：流式调用")

    while True:
        message = input("\n> ").strip()
        if not message:
            continue
        if message.lower() in {"exit", "quit"}:
            return 0

        if message.startswith("/stream "):
            actual_message = message[len("/stream ") :].strip()
            if not actual_message:
                print("请在 /stream 后输入问题")
                continue
            try:
                async for chunk in agent.arun_stream(actual_message, session_id=session_id):
                    chunk_text = getattr(chunk, "text", None)
                    if chunk_text:
                        print(chunk_text, end="", flush=True)
                print()
            except Exception as exc:
                print(f"\n[错误] {exc}")
            continue

        try:
            response = await agent.arun(message, session_id=session_id)
            _print_text_response(response)
        except Exception as exc:
            print(f"[错误] {exc}")


def main() -> int:
    return asyncio.run(run_interactive())


if __name__ == "__main__":
    raise SystemExit(main())

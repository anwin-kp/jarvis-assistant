"""Local LLM brain using Ollama's tool calling."""

import ollama
from loguru import logger

from jarvis.tools.registry import ConfirmFn, ToolRegistry


class BrainUnavailable(RuntimeError):
    pass


class OllamaBrain:
    def __init__(self, registry: ToolRegistry, cfg: dict):
        self.reg = registry
        self.client = ollama.Client(host=cfg["ollama_host"])
        self.model = cfg["ollama_model"]
        self.think = cfg.get("think", False)
        self.keep_alive = cfg.get("keep_alive", "10m")
        self.options = {"num_ctx": cfg.get("num_ctx", 8192), "temperature": cfg.get("temperature", 0.3)}
        self.max_steps = cfg.get("max_steps", 6)

    def check(self) -> None:
        """Raise BrainUnavailable with a helpful message if Ollama or the model is missing."""
        try:
            models = [m.model for m in self.client.list().models]
        except Exception as e:
            raise BrainUnavailable(
                "Can't reach Ollama. Start it from the Start menu (or run `ollama serve`)."
            ) from e
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted not in models:
            raise BrainUnavailable(f"Model {self.model} isn't downloaded. Run: ollama pull {self.model}")

    def unload(self) -> None:
        """Free the GPU memory immediately (e.g. before gaming)."""
        self.client.generate(model=self.model, prompt="", keep_alive=0)

    def complete(self, messages: list[dict]) -> str:
        """Plain completion without tools (summaries etc.)."""
        resp = self.client.chat(model=self.model, messages=messages, think=False,
                                keep_alive=self.keep_alive, options=self.options)
        return (resp.message.content or "").strip()

    def respond(self, messages: list[dict], tools: list[dict], confirm: ConfirmFn) -> str:
        """messages: plain [{"role", "content"}] dicts. Tool calls live only inside this turn."""
        msgs = list(messages)
        self.last_tool_calls: list[str] = []
        for step in range(self.max_steps):
            resp = self.client.chat(
                model=self.model,
                messages=msgs,
                tools=tools or None,
                think=self.think,
                keep_alive=self.keep_alive,
                options=self.options,
            )
            msg = resp.message
            msgs.append(msg)
            if not msg.tool_calls:
                return (msg.content or "").strip()
            for call in msg.tool_calls:
                name, args = call.function.name, call.function.arguments
                logger.debug("step {} tool call {} {}", step, name, args)
                self.last_tool_calls.append(name)
                result = self.reg.execute(name, args, confirm)
                msgs.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": ("ERROR: " if result.error else "") + result.text,
                })
        return "That took more steps than I'm allowed, so I stopped. Want me to try a simpler approach?"

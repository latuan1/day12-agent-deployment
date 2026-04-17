from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# Ensure the deployable folder is importable. The workspace root is only
# useful when running the repo outside the Railway build context.
LAB_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = LAB_ROOT.parent
for path in (LAB_ROOT, WORKSPACE_ROOT):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

from src.telemetry.logger import logger


MOCK_RESPONSES = {
    "default": [
        "Đây là câu trả lời từ AI agent (mock). Trong production, đây sẽ là response từ OpenAI/Anthropic.",
        "Agent đang hoạt động tốt! (mock response) Hỏi thêm câu hỏi đi nhé.",
        "Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã được nhận.",
    ],
    "docker": ["Container là cách đóng gói app để chạy ở mọi nơi. Build once, run anywhere!"],
    "deploy": ["Deployment là quá trình đưa code từ máy bạn lên server để người khác dùng được."],
    "health": ["Agent đang hoạt động bình thường. All systems operational."],
}


def mock_ask(question: str, delay: float = 0.1) -> str:
    import random

    time.sleep(delay + random.uniform(0, 0.05))

    question_lower = question.lower()
    for keyword, responses in MOCK_RESPONSES.items():
        if keyword in question_lower:
            return random.choice(responses)

    return random.choice(MOCK_RESPONSES["default"])


def mock_ask_stream(question: str):
    response = mock_ask(question)
    for word in response.split():
        time.sleep(0.05)
        yield word + " "


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    port: int = 8000
    redis_url: str = "redis://localhost:6379/0"
    agent_api_key: str = "dev-secret-change-me"
    log_level: str = "INFO"
    rate_limit_per_minute: int = 10
    monthly_budget_usd: float = 10.0
    default_provider: str = "openai"
    default_model: str = "deepseek-ai/deepseek-v3.2"
    session_ttl_seconds: int = 86400
    max_history_messages: int = 20
    input_token_cost_usd_per_1k: float = 0.00015
    output_token_cost_usd_per_1k: float = 0.0006
    environment: str = "development"


settings = Settings()


def estimate_cost_usd(usage: Dict[str, int]) -> float:
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)
    return round(
        (input_tokens / 1000) * settings.input_token_cost_usd_per_1k
        + (output_tokens / 1000) * settings.output_token_cost_usd_per_1k,
        6,
    )


def normalize_user_id(api_key: str, explicit_user_id: Optional[str] = None) -> str:
    if explicit_user_id:
        cleaned_user_id = explicit_user_id.strip()
        if cleaned_user_id:
            return cleaned_user_id
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:10]
    return f"user-{digest}"


def format_history(messages: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for item in messages[-settings.max_history_messages :]:
        role = str(item.get("role", "user")).capitalize()
        content = str(item.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


class MockProvider:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.provider_name = "mock"

    def generate(self, prompt: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
        start_time = time.time()
        content = mock_ask(prompt)
        latency_ms = int((time.time() - start_time) * 1000)
        usage = {
            "prompt_tokens": max(1, len(prompt) // 4),
            "completion_tokens": max(1, len(content) // 4),
            "total_tokens": max(2, (len(prompt) + len(content)) // 4),
        }
        return {
            "content": content,
            "usage": usage,
            "latency_ms": latency_ms,
            "provider": self.provider_name,
        }

    def stream(self, prompt: str, system_prompt: Optional[str] = None):
        yield from mock_ask_stream(prompt)


class UsageTrackingProvider:
    def __init__(self, provider: Any):
        self.provider = provider
        self.model_name = getattr(provider, "model_name", settings.default_model)
        self.provider_name = getattr(provider, "provider_name", provider.__class__.__name__.lower())
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def reset(self) -> None:
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def generate(self, prompt: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
        result = self.provider.generate(prompt=prompt, system_prompt=system_prompt)
        usage = result.get("usage", {})
        for key in self.total_usage:
            self.total_usage[key] += int(usage.get(key, 0) or 0)
        return result

    def stream(self, prompt: str, system_prompt: Optional[str] = None):
        yield from self.provider.stream(prompt=prompt, system_prompt=system_prompt)


def build_provider() -> Any:
    preferred = (settings.default_provider or "mock").strip().lower()
    provider: Any

    try:
        if preferred == "openai":
            from src.core.openai_provider import OpenAIProvider

            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is not set")
            base_url = os.getenv("OPENAI_API_BASE_URL") or os.getenv("OPENAI_BASE_URL")
            provider = OpenAIProvider(model_name=settings.default_model, api_key=api_key, base_url=base_url)
            provider.provider_name = "openai"
        elif preferred in {"google", "gemini"}:
            from src.core.gemini_provider import GeminiProvider

            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY is not set")
            provider = GeminiProvider(model_name=settings.default_model, api_key=api_key)
            provider.provider_name = "google"
        elif preferred == "local":
            from src.core.local_provider import LocalProvider

            model_path = os.getenv("LOCAL_MODEL_PATH")
            if not model_path:
                raise RuntimeError("LOCAL_MODEL_PATH is not set")
            provider = LocalProvider(model_path=model_path)
            provider.provider_name = "local"
        else:
            provider = MockProvider(model_name=settings.default_model)
    except Exception as exc:
        logger.error(f"Falling back to mock provider: {exc}")
        provider = MockProvider(model_name=settings.default_model)

    return provider


class ProductionStateStore:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._redis = None
        self._use_redis = False
        self._redis_error = ""
        self._sessions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._rate_windows: Dict[str, Deque[float]] = defaultdict(deque)
        self._monthly_usage: Dict[str, float] = defaultdict(float)

        try:
            import redis

            self._redis = redis.from_url(redis_url, decode_responses=True)
            self._redis.ping()
            self._use_redis = True
        except Exception as exc:
            self._redis_error = str(exc)

    @property
    def backend(self) -> str:
        return "redis" if self._use_redis else "memory"

    @property
    def redis_connected(self) -> bool:
        if not self._use_redis or self._redis is None:
            return False
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    @property
    def redis_error(self) -> str:
        return self._redis_error

    def health_payload(self) -> Dict[str, Any]:
        return {
            "storage_backend": self.backend,
            "redis_connected": self.redis_connected,
            "redis_error": self._redis_error if not self._use_redis else "",
        }

    def _session_key(self, session_id: str) -> str:
        return f"session:{session_id}"

    def _rate_key(self, user_id: str) -> str:
        return f"rate:{user_id}"

    def _usage_key(self, user_id: str) -> str:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")
        return f"usage:{month_key}:{user_id}"

    def get_history(self, session_id: str) -> List[Dict[str, Any]]:
        if self._use_redis and self._redis is not None:
            raw = self._redis.get(self._session_key(session_id))
            return json.loads(raw) if raw else []
        return list(self._sessions.get(session_id, []))

    def append_history(self, session_id: str, role: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        history = self.get_history(session_id)
        entry = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            entry["metadata"] = metadata
        history.append(entry)
        history = history[-settings.max_history_messages :]

        if self._use_redis and self._redis is not None:
            self._redis.setex(self._session_key(session_id), settings.session_ttl_seconds, json.dumps(history, ensure_ascii=False))
        else:
            self._sessions[session_id] = history

        return history

    def clear_session(self, session_id: str) -> None:
        if self._use_redis and self._redis is not None:
            self._redis.delete(self._session_key(session_id))
        else:
            self._sessions.pop(session_id, None)

    def check_rate_limit(self, user_id: str, limit: Optional[int] = None, window_seconds: int = 60) -> Dict[str, Any]:
        limit = limit or settings.rate_limit_per_minute
        now = time.time()
        window_start = now - window_seconds

        if self._use_redis and self._redis is not None:
            key = self._rate_key(user_id)
            self._redis.zremrangebyscore(key, 0, window_start)
            current = int(self._redis.zcard(key))
            if current >= limit:
                retry_after = window_seconds
                oldest = self._redis.zrange(key, 0, 0, withscores=True)
                if oldest:
                    retry_after = max(1, int(oldest[0][1] + window_seconds - now) + 1)
                raise ValueError(json.dumps({"status": 429, "retry_after": retry_after}))

            member = f"{now}:{uuid.uuid4().hex}"
            self._redis.zadd(key, {member: now})
            self._redis.expire(key, window_seconds + 5)
            remaining = max(0, limit - current - 1)
            return {"limit": limit, "remaining": remaining, "window_seconds": window_seconds}

        window = self._rate_windows[user_id]
        while window and window[0] < window_start:
            window.popleft()
        if len(window) >= limit:
            retry_after = max(1, int(window[0] + window_seconds - now) + 1)
            raise ValueError(json.dumps({"status": 429, "retry_after": retry_after}))

        window.append(now)
        return {"limit": limit, "remaining": max(0, limit - len(window)), "window_seconds": window_seconds}

    def get_monthly_usage(self, user_id: str) -> Dict[str, Any]:
        budget = settings.monthly_budget_usd
        if self._use_redis and self._redis is not None:
            key = self._usage_key(user_id)
            spent = float(self._redis.get(key) or 0.0)
        else:
            spent = float(self._monthly_usage[user_id])

        return {
            "user_id": user_id,
            "month": datetime.now(timezone.utc).strftime("%Y-%m"),
            "spent_usd": round(spent, 6),
            "budget_usd": budget,
            "remaining_usd": max(0.0, round(budget - spent, 6)),
            "used_pct": 0.0 if budget <= 0 else round((spent / budget) * 100, 1),
        }

    def check_budget(self, user_id: str, budget_usd: Optional[float] = None) -> Dict[str, Any]:
        budget_usd = settings.monthly_budget_usd if budget_usd is None else budget_usd
        usage = self.get_monthly_usage(user_id)
        if usage["spent_usd"] >= budget_usd:
            raise ValueError(json.dumps({"status": 402, "usage": usage}))
        return usage

    def record_usage(self, user_id: str, usage: Dict[str, int]) -> Dict[str, Any]:
        cost = estimate_cost_usd(usage)
        if self._use_redis and self._redis is not None:
            key = self._usage_key(user_id)
            self._redis.incrbyfloat(key, cost)
            self._redis.expire(key, 35 * 24 * 60 * 60)
        else:
            self._monthly_usage[user_id] += cost
        return self.get_monthly_usage(user_id)


state_store = ProductionStateStore(settings.redis_url)

"""
Production-ready Flask web application for the travel planning agent.
Keeps the existing UI but adds REST endpoints, Redis-backed state, auth,
rate limiting, cost guard, and production deployment hooks.
"""

import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from production_support import (  # noqa: E402
    UsageTrackingProvider,
    build_provider,
    estimate_cost_usd,
    format_history,
    normalize_user_id,
    settings,
    state_store,
)
from src.agent.agent import ReActAgent
from src.telemetry.logger import logger
from src.tools.tool_registry import get_tools

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
)

# Initialize provider and tools
provider = None
tools = None
shutdown_event = threading.Event()
START_TIME = time.time()


def _handle_shutdown(signum, frame):
    if not shutdown_event.is_set():
        shutdown_event.set()
        logger.log_event("APP_SHUTDOWN", {"signal": signum})


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)


def init_systems():
    """Lazy initialization of the provider and tool registry."""
    global provider, tools
    if provider is None:
        provider = build_provider()
        tools = get_tools()
        logger.log_event(
            "SYSTEM_READY",
            {
                "provider": getattr(provider, "provider_name", provider.__class__.__name__),
                "model": getattr(provider, "model_name", settings.default_model),
                "tools": [tool["name"] for tool in tools],
                "storage_backend": state_store.backend,
            },
        )


def _build_prompt(user_message: str, session_id: str) -> str:
    history = state_store.get_history(session_id)
    history_text = format_history(history)
    if not history_text:
        return user_message
    return f"Conversation history:\n{history_text}\n\nCurrent user request: {user_message}"


def _error_response(message: str, status_code: int, extra: dict | None = None):
    payload = {"error": message}
    if extra:
        payload.update(extra)
    return jsonify(payload), status_code


def _parse_guard_error(exc: ValueError):
    try:
        data = json.loads(str(exc))
    except json.JSONDecodeError:
        return None

    status_code = int(data.get("status", 500))
    if status_code == 429:
        retry_after = int(data.get("retry_after", 60))
        return _error_response(
            "Rate limit exceeded",
            429,
            {"retry_after_seconds": retry_after, "limit": settings.rate_limit_per_minute},
        )
    if status_code == 402:
        usage = data.get("usage", {})
        return _error_response(
            "Monthly budget exceeded",
            402,
            {"usage": usage, "budget_usd": settings.monthly_budget_usd},
        )
    return _error_response("Request rejected", status_code, data)


def _resolve_identity(payload: dict, require_auth: bool = False):
    api_key = request.headers.get("X-API-Key", "").strip()
    explicit_user_id = request.headers.get("X-User-Id") or payload.get("user_id")

    if require_auth:
        if not api_key or api_key != settings.agent_api_key:
            return None, _error_response("Invalid or missing API key", 401)
        user_id = normalize_user_id(api_key, explicit_user_id)
        return user_id, None

    if api_key and api_key == settings.agent_api_key:
        return normalize_user_id(api_key, explicit_user_id), None

    return (explicit_user_id or "demo-user").strip() or "demo-user", None


def _enforce_limits(user_id: str):
    try:
        rate_info = state_store.check_rate_limit(user_id)
        budget_info = state_store.check_budget(user_id)
        return rate_info, budget_info, None
    except ValueError as exc:
        return None, None, _parse_guard_error(exc)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health')
def health():
    """Health check for load balancers and deployment platforms."""
    init_systems()
    return jsonify({
        "status": "ok",
        "service": "travel-planning-agent",
        "provider": getattr(provider, "provider_name", provider.__class__.__name__ if provider else "unknown"),
        "model": getattr(provider, "model_name", settings.default_model),
        "uptime_seconds": int(time.time() - START_TIME),
        "storage": state_store.health_payload(),
    })


@app.route('/ready')
def ready():
    """Readiness check that validates state backend connectivity."""
    init_systems()
    storage = state_store.health_payload()
    if storage["storage_backend"] == "redis" and not storage["redis_connected"]:
        return jsonify({"status": "not_ready", "storage": storage}), 503
    return jsonify({"status": "ready", "storage": storage})


@app.route('/api/chat', methods=['POST'])
def chat():
    payload = request.get_json(silent=True) or {}
    return _handle_request(payload, require_auth=False)


@app.route('/ask', methods=['POST'])
def ask():
    payload = request.get_json(silent=True) or {}
    return _handle_request(payload, require_auth=True)


@app.route('/api/sessions/<session_id>', methods=['GET'])
def get_session(session_id: str):
    init_systems()
    history = state_store.get_history(session_id)
    return jsonify({
        "session_id": session_id,
        "count": len(history),
        "messages": history,
    })


@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session(session_id: str):
    init_systems()
    state_store.clear_session(session_id)
    return jsonify({"deleted": session_id})


def _handle_request(payload: dict, require_auth: bool):
    init_systems()

    user_message = payload.get('message') or payload.get('question') or ''
    mode = payload.get('mode', 'agent_v2')
    session_id = (payload.get('session_id') or request.headers.get('X-Session-Id') or uuid.uuid4().hex).strip() or uuid.uuid4().hex

    if not user_message:
        return _error_response('No message provided', 400)

    user_id, identity_error = _resolve_identity(payload, require_auth=require_auth)
    if identity_error:
        return identity_error

    rate_info, budget_info, guard_error = _enforce_limits(user_id)
    if guard_error:
        return guard_error

    logger.log_event(
        'REQUEST_START',
        {
            'user_id': user_id,
            'session_id': session_id,
            'mode': mode,
            'require_auth': require_auth,
            'storage_backend': state_store.backend,
        },
    )

    try:
        if mode == 'chatbot':
            response = handle_chatbot(user_message, session_id, user_id)
        elif mode == 'agent_v1':
            response = handle_agent(user_message, session_id, user_id, 'v1')
        else:
            response = handle_agent(user_message, session_id, user_id, 'v2')
    except Exception as exc:
        logger.error(f'Error in request handler: {exc}')
        return _error_response(str(exc), 500)

    response['session_id'] = session_id
    response['user_id'] = user_id
    response['rate_limit'] = rate_info or {'limit': settings.rate_limit_per_minute}
    response['budget'] = budget_info or state_store.get_monthly_usage(user_id)

    logger.log_event(
        'REQUEST_END',
        {
            'user_id': user_id,
            'session_id': session_id,
            'mode': response.get('mode'),
            'steps_count': response.get('metrics', {}).get('steps_count', 0),
            'storage_backend': state_store.backend,
        },
    )

    return jsonify(response)


def handle_chatbot(user_message: str, session_id: str, user_id: str):
    """Handle chatbot baseline request."""
    start_time = time.time()
    history_prompt = _build_prompt(user_message, session_id)
    state_store.append_history(session_id, 'user', user_message, {'mode': 'chatbot'})

    system_prompt = """You are a travel planning assistant (Trợ lý Du lịch).
You help users plan trips including weather, hotels, and activities.
IMPORTANT: You do NOT have access to any tools, APIs, or real-time data.
You can only answer based on your general knowledge.
If asked about specific real-time information (current weather, hotel prices, availability),
you must clearly state that you don't have access to real-time data.
Answer in the same language as the user's query."""

    request_provider = UsageTrackingProvider(provider)
    request_provider.reset()
    result = request_provider.generate(prompt=history_prompt, system_prompt=system_prompt)
    total_time = int((time.time() - start_time) * 1000)
    usage = result.get('usage', {})
    usage_record = state_store.record_usage(user_id, request_provider.total_usage)
    state_store.append_history(session_id, 'assistant', result['content'], {'mode': 'chatbot'})

    return {
        "mode": "chatbot",
        "answer": result['content'],
        "steps": [],
        "history": state_store.get_history(session_id),
        "storage_backend": state_store.backend,
        "metrics": {
            "latency_ms": total_time,
            "total_tokens": usage.get('total_tokens', 0),
            "prompt_tokens": usage.get('prompt_tokens', 0),
            "completion_tokens": usage.get('completion_tokens', 0),
            "steps_count": 0,
            "cost_usd": estimate_cost_usd(request_provider.total_usage),
            "monthly_spend_usd": usage_record['spent_usd'],
        },
    }


def handle_agent(user_message: str, session_id: str, user_id: str, version: str):
    """Handle agent request with step-by-step tracking."""
    start_time = time.time()
    steps_log = []
    history_prompt = _build_prompt(user_message, session_id)
    state_store.append_history(session_id, 'user', user_message, {'mode': f'agent_{version}'})

    request_provider = UsageTrackingProvider(provider)
    request_provider.reset()
    agent = ReActAgent(llm=request_provider, tools=tools, max_steps=10, version=version)

    current_prompt = history_prompt
    conversation = ""
    steps = 0
    total_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    final_answer = None

    while steps < agent.max_steps:
        steps += 1

        full_prompt = current_prompt
        if conversation:
            full_prompt = f"{current_prompt}\n\n{conversation}"

        try:
            result = agent.llm.generate(
                prompt=full_prompt,
                system_prompt=agent.get_system_prompt()
            )
        except Exception as e:
            steps_log.append({
                "step": steps,
                "type": "error",
                "content": f"LLM call failed: {str(e)}"
            })
            break

        llm_output = result["content"]
        usage = result.get("usage", {})
        latency = result.get("latency_ms", 0)

        total_tokens["prompt_tokens"] += usage.get("prompt_tokens", 0)
        total_tokens["completion_tokens"] += usage.get("completion_tokens", 0)
        total_tokens["total_tokens"] += usage.get("total_tokens", 0)

        thought_match = re.search(r'Thought:\s*(.+?)(?:\n|Action:|Final Answer:)', llm_output, re.DOTALL)
        thought = thought_match.group(1).strip() if thought_match else ""

        fa = agent._extract_final_answer(llm_output)
        if fa:
            steps_log.append({
                "step": steps,
                "type": "thought",
                "content": thought,
                "latency_ms": latency
            })
            steps_log.append({
                "step": steps,
                "type": "final_answer",
                "content": fa
            })
            final_answer = fa
            break

        action = agent._parse_action(llm_output)

        if action is None:
            steps_log.append({
                "step": steps,
                "type": "thought",
                "content": thought or llm_output[:200],
                "latency_ms": latency
            })
            if version == "v2" and steps < agent.max_steps:
                conversation += f"\n{llm_output}\n\nSystem: You must provide an Action in JSON format or a Final Answer."
                steps_log.append({
                    "step": steps,
                    "type": "retry",
                    "content": "No valid Action found, retrying..."
                })
                continue
            else:
                final_answer = llm_output
                break

        tool_name = action.get("tool", "")
        tool_args = action.get("args", {})
        observation = agent._execute_tool(tool_name, tool_args)

        steps_log.append({
            "step": steps,
            "type": "thought",
            "content": thought,
            "latency_ms": latency
        })
        steps_log.append({
            "step": steps,
            "type": "action",
            "tool": tool_name,
            "args": tool_args,
            "content": f"{tool_name}({json.dumps(tool_args, ensure_ascii=False)})"
        })
        steps_log.append({
            "step": steps,
            "type": "observation",
            "content": observation
        })

        conversation += f"\n{llm_output}\nObservation: {observation}\n"

    total_time = int((time.time() - start_time) * 1000)

    if final_answer is None:
        try:
            final_prompt = f"{current_prompt}\n\n{conversation}\n\nProvide your Final Answer based on the information gathered."
            result = agent.llm.generate(prompt=final_prompt, system_prompt=agent.get_system_prompt())
            final_answer = agent._extract_final_answer(result["content"]) or result["content"]
        except Exception:
            final_answer = "Agent could not complete the request."

    usage_record = state_store.record_usage(user_id, request_provider.total_usage)
    state_store.append_history(session_id, 'assistant', final_answer, {'mode': f'agent_{version}'})

    return {
        "mode": f"agent_{version}",
        "answer": final_answer,
        "steps": steps_log,
        "history": state_store.get_history(session_id),
        "storage_backend": state_store.backend,
        "metrics": {
            "latency_ms": total_time,
            "total_tokens": total_tokens.get("total_tokens", 0),
            "prompt_tokens": total_tokens.get("prompt_tokens", 0),
            "completion_tokens": total_tokens.get("completion_tokens", 0),
            "steps_count": steps,
            "cost_usd": estimate_cost_usd(request_provider.total_usage),
            "monthly_spend_usd": usage_record['spent_usd'],
        }
    }


@app.route('/api/test-cases', methods=['GET'])
def get_test_cases():
    """Return predefined test cases for quick testing."""
    today = datetime.now()
    next_sat = (today + timedelta(days=(5 - today.weekday()) % 7 or 7)).strftime("%Y-%m-%d")
    
    test_cases = [
        {
            "name": "🌤️ Simple Weather Check",
            "query": f"Thời tiết ở Đà Lạt ngày {next_sat} thế nào?",
            "type": "single_tool"
        },
        {
            "name": "🏨 Hotel Search",
            "query": "Tìm khách sạn ở Đà Lạt dưới 500k/đêm",
            "type": "single_tool"
        },
        {
            "name": "🌿 Multi-step Branching (KEY TEST)",
            "query": (
                "Tôi định đi Đà Lạt vào cuối tuần này. Kiểm tra xem thời tiết thế nào nhé. "
                "Nếu trời không mưa, hãy tìm cho tôi một khách sạn dưới 500k và 2 địa điểm đi dạo ngoài trời. "
                "Nếu trời mưa, hãy gợi ý quán cafe đẹp."
            ),
            "type": "multi_step"
        },
        {
            "name": "☕ Rainy Day Activities",
            "query": "Trời đang mưa ở Hà Nội, gợi ý cho tôi vài quán cafe đẹp",
            "type": "single_tool"
        },
        {
            "name": "🏖️ Nha Trang Trip",
            "query": f"Tôi muốn đi Nha Trang ngày {next_sat}. Kiểm tra thời tiết và tìm khách sạn dưới 1 triệu.",
            "type": "multi_step"
        }
    ]
    
    return jsonify(test_cases)


if __name__ == '__main__':
    debug_mode = os.getenv("DEBUG", "false").lower() == "true"
    runtime_port = int(os.getenv("PORT", settings.port))
    print("\n" + "=" * 60)
    print("🌍 Travel Planning Agent — Web UI")
    print("=" * 60)
    print(f"Open in browser: http://localhost:{runtime_port}")
    print("=" * 60 + "\n")
    app.run(debug=debug_mode, host='0.0.0.0', port=runtime_port)

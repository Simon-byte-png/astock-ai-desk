# -*- coding: utf-8 -*-
"""OpenAI 兼容 LLM 客户端：DeepSeek 主通道，阶跃星辰备用通道。"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_CTX = ssl.create_default_context()

DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
STEP_BASE = os.environ.get("STEP_BASE_URL", "https://api.stepfun.com/v1").rstrip("/")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
STEP_KEY = os.environ.get("STEP_API_KEY", "").strip()

# 模型名都可由 Zeabur Variables 覆盖，避免服务商升级时改代码。
MODEL_FAST = os.environ.get("DEEPSEEK_MODEL_FAST", "deepseek-v4-flash")
MODEL_STRONG = os.environ.get("DEEPSEEK_MODEL_STRONG", "deepseek-v4-pro")
MODEL_BACKUP = os.environ.get("STEP_MODEL_BACKUP", "step-3.5-flash")
MODEL_TTS = os.environ.get("STEP_MODEL_TTS", "step-tts-mini")

PROVIDERS = {
    "deepseek": {"base": DEEPSEEK_BASE, "key": DEEPSEEK_KEY},
    "step": {"base": STEP_BASE, "key": STEP_KEY},
}

# 兼容旧代码读取 BASE；不再暴露 TOKEN。
BASE = DEEPSEEK_BASE


def provider_status():
    return {
        "primary": {
            "name": "deepseek",
            "host": DEEPSEEK_BASE.split("//")[-1].split("/")[0],
            "configured": bool(DEEPSEEK_KEY),
            "fast_model": MODEL_FAST,
            "strong_model": MODEL_STRONG,
        },
        "backup": {
            "name": "step",
            "host": STEP_BASE.split("//")[-1].split("/")[0],
            "configured": bool(STEP_KEY),
            "model": MODEL_BACKUP,
            "tts_model": MODEL_TTS,
        },
    }


def _error_text(exc):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read(800).decode("utf-8", "ignore")
        except Exception:
            detail = ""
        return f"HTTP {exc.code}: {detail or exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def _stream_chat(provider_name, system, user, model, max_tokens, temperature,
                 json_mode=False):
    cfg = PROVIDERS[provider_name]
    if not cfg["key"]:
        raise RuntimeError(f"未配置 {provider_name.upper()} API Key")

    payload = {
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if provider_name == "deepseek" and model == MODEL_STRONG:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = os.environ.get("DEEPSEEK_REASONING_EFFORT", "high")

    req = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + cfg["key"],
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    chunks = []
    with urllib.request.urlopen(req, timeout=150, context=_CTX) as response:
        for raw in response:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
                delta = event.get("choices", [{}])[0].get("delta", {})
                # reasoning_content 是模型思考过程，不展示、不进入业务 JSON。
                content = delta.get("content")
                if content:
                    chunks.append(content)
            except (ValueError, IndexError, TypeError):
                continue
    text = "".join(chunks).strip()
    if not text:
        raise RuntimeError("模型返回了空响应")
    return text


def _chat(system, user, model=None, max_tokens=1500, temperature=None,
          retries=2, json_mode=False):
    primary_model = model or MODEL_FAST
    attempts = []
    if DEEPSEEK_KEY:
        attempts.append(("deepseek", primary_model))
    if STEP_KEY:
        attempts.append(("step", MODEL_BACKUP))
    if not attempts:
        raise RuntimeError(
            "未配置 DEEPSEEK_API_KEY 或 STEP_API_KEY；行情、模拟盘和回测仍可使用，AI 功能暂不可用"
        )

    errors = []
    for provider_name, selected_model in attempts:
        for attempt in range(max(1, retries)):
            try:
                return _stream_chat(
                    provider_name, system, user, selected_model, max_tokens,
                    temperature, json_mode=json_mode,
                )
            except Exception as exc:
                errors.append(f"{provider_name}/{selected_model}: {_error_text(exc)}")
                if attempt + 1 < retries:
                    time.sleep(0.8 * (attempt + 1))
    raise RuntimeError("LLM 双通道均调用失败：" + " | ".join(errors))


def chat(system, user, model=None, max_tokens=1500, temperature=None, retries=2):
    """调用主模型；主通道失败后自动切到阶跃备用通道。"""
    return _chat(system, user, model=model, max_tokens=max_tokens,
                 temperature=temperature, retries=retries)


def _extract_json(text):
    """从模型回复中提取 JSON 对象。"""
    text = (text or "").strip()
    if text.startswith("```"):
        parts = text.split("```", 2)
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    return {"_raw": text}


def chat_json(system, user, model=None, max_tokens=1500, json_retries=3):
    """要求模型只返回 JSON；格式错误时自动重试。"""
    system_json = (
        system
        + "\n\n严格要求：只输出一个合法 JSON 对象，不要解释文字，不要 Markdown 代码块。"
    )
    last = {"_raw": ""}
    for _ in range(max(1, json_retries)):
        text = _chat(system_json, user, model=model, max_tokens=max_tokens,
                     retries=2, json_mode=True)
        last = _extract_json(text)
        if "_raw" not in last:
            return last
    return last


def parallel(tasks):
    """并发执行 ``[(key, fn)]``，单个失败不会拖垮整组任务。"""
    out = {}
    with ThreadPoolExecutor(max_workers=min(6, len(tasks) or 1)) as executor:
        futures = {executor.submit(fn): key for key, fn in tasks}
        for future, key in futures.items():
            try:
                out[key] = future.result()
            except Exception as exc:
                out[key] = {"error": str(exc)}
    return out


def tts(text, voice=None):
    """调用阶跃 TTS，返回 MP3 字节。"""
    if not STEP_KEY:
        raise RuntimeError("未配置 STEP_API_KEY，语音复盘暂不可用")
    clean = (text or "").strip()
    if not clean:
        raise ValueError("缺少要朗读的文字")
    payload = {
        "model": MODEL_TTS,
        "input": clean[:1000],
        "voice": voice or os.environ.get("STEP_TTS_VOICE", "cixingnansheng"),
        "response_format": "mp3",
        "text_normalization": "enhanced",
    }
    req = urllib.request.Request(
        STEP_BASE + "/audio/speech",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + STEP_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120, context=_CTX) as response:
        return response.read()


if __name__ == "__main__":
    print(chat_json("你是测试器", '返回 {"ok": true, "msg": "你好"}'))

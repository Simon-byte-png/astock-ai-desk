# -*- coding: utf-8 -*-
"""
LLM 客户端：Anthropic 兼容 messages API（默认走小米 MiMo v2.5 端点）。
支持：system prompt、强制 JSON 输出、流式(SSE)、失败重试、并发调用。
可用环境变量覆盖端点/密钥/模型：DESK_LLM_BASE / DESK_LLM_TOKEN / DESK_MODEL_FAST / DESK_MODEL_STRONG。
"""
import os, json, urllib.request, ssl, time
from concurrent.futures import ThreadPoolExecutor

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# 默认使用用户提供的 MiMo v2.5（Anthropic 兼容）
BASE = (os.environ.get("DESK_LLM_BASE")
        or "https://token-plan-cn.xiaomimimo.com/anthropic").rstrip("/")
TOKEN = (os.environ.get("DESK_LLM_TOKEN")
         or "tp-cwxhnnn1oh0fzpm0h8uk8mey7342gfalfj3qev0p5nmpnbhv")

MODEL_FAST = os.environ.get("DESK_MODEL_FAST", "mimo-v2.5")
MODEL_STRONG = os.environ.get("DESK_MODEL_STRONG", "mimo-v2.5")


def chat(system, user, model=None, max_tokens=1500, temperature=None, retries=3):
    """流式(SSE)调用：边生成边收，避免代理对长响应体的截断。"""
    payload = {
        "model": model or MODEL_FAST,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "stream": True,
    }
    if temperature is not None:
        payload["temperature"] = temperature  # 新模型已弃用，默认不传
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-api-key": TOKEN,
        "authorization": "Bearer " + TOKEN,
        "anthropic-version": "2023-06-01",
        "accept": "text/event-stream",
    }
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(BASE + "/v1/messages", data=body, headers=headers)
            r = urllib.request.urlopen(req, timeout=120, context=_CTX)
            chunks = []
            stopped = False
            for raw in r:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    stopped = True
                    break
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                t = ev.get("type")
                if t == "content_block_delta":
                    d = ev.get("delta", {})
                    if d.get("type") == "text_delta":
                        chunks.append(d.get("text", ""))
                elif t == "message_stop":
                    stopped = True
                elif t == "error":
                    raise RuntimeError(ev.get("error", {}).get("message", "stream error"))
            text = "".join(chunks).strip()
            if not text:
                raise RuntimeError("empty stream")
            return text
        except Exception as e:
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"LLM 调用失败: {last}")


def _extract_json(text):
    """从模型回复里抠出 JSON 对象。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            pass
    return {"_raw": text}


def chat_json(system, user, model=None, max_tokens=1500, json_retries=3):
    """要求模型输出 JSON。若响应被截断/无法解析，自动重试（代理在长响应下偶发截断）。"""
    sys_j = system + "\n\n严格要求：只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块围栏。"
    last = None
    for _ in range(json_retries):
        txt = chat(sys_j, user, model=model, max_tokens=max_tokens)
        obj = _extract_json(txt)
        if "_raw" not in obj:   # 成功解析
            return obj
        last = obj
    return last if last is not None else {"_raw": ""}


def parallel(tasks):
    """tasks: list of (key, fn)；并发执行，返回 {key: result}。fn 内部自行 try。"""
    out = {}
    with ThreadPoolExecutor(max_workers=min(6, len(tasks) or 1)) as ex:
        futs = {ex.submit(fn): key for key, fn in tasks}
        for fut in futs:
            key = futs[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                out[key] = {"error": str(e)}
    return out


if __name__ == "__main__":
    print(chat_json("你是测试器", "返回 {\"ok\": true, \"msg\": \"你好\"}"))

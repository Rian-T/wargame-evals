"""Model backends: local CLI tools and OpenAI-compatible HTTP APIs.

Every backend returns a parsed game response:

    {"status": "ok" | "parse_failure" | "timeout" | "error",
     "message": str, "action_level": int | None, "target": str | None,
     "reasoning": str, "raw": str}

`action_level` is None unless status is "ok". Analysis code must filter on
`status` so that failed runs are never counted as decisions.
"""

import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.request

TEMPERATURE = 0.7
MAX_BACKOFF_S = 90
RETRY_INSTRUCTION = ("\n\nIMPORTANT: Respond with ONLY a raw JSON object. "
                     "No markdown, no explanation.")

# Models called through a local command-line tool.
CLI_MODELS = {
    "sonnet": ["claude", "-p", "--model", "claude-sonnet-4-6", "--output-format", "text"],
    "opus": ["claude", "-p", "--model", "claude-opus-4-6", "--output-format", "text"],
    "haiku": ["claude", "-p", "--model", "claude-haiku-4-5-20251001", "--output-format", "text"],
    "sonnet-5": ["claude", "-p", "--model", "claude-sonnet-5", "--output-format", "text"],
    "opus-4.8": ["claude", "-p", "--model", "claude-opus-4-8", "--output-format", "text"],
    "fable-5": ["claude", "-p", "--model", "claude-fable-5", "--output-format", "text"],
    "opus-5": ["claude", "-p", "--model", "claude-opus-5", "--effort", "low",
               "--output-format", "text"],
    "gemini-flash": ["gemini", "-m", "gemini-3-flash-preview"],
    "gemini-pro": ["gemini", "-m", "gemini-3.1-pro-preview"],
    "codex": ["codex", "exec", "--skip-git-repo-check", "-c", 'model_reasoning_effort="low"', "-"],
    "gpt-5.5-codex-cli": ["codex", "exec", "--skip-git-repo-check", "-m", "gpt-5.5",
                          "-c", 'model_reasoning_effort="medium"', "-"],
    "gpt-5.6-sol": ["codex", "exec", "--skip-git-repo-check", "-m", "gpt-5.6-sol",
                    "-c", 'model_reasoning_effort="medium"', "-"],
    "gpt-5.6-terra": ["codex", "exec", "--skip-git-repo-check", "-m", "gpt-5.6-terra",
                    "-c", 'model_reasoning_effort="medium"', "-"],
    "gpt-5.6-luna": ["codex", "exec", "--skip-git-repo-check", "-m", "gpt-5.6-luna",
                    "-c", 'model_reasoning_effort="medium"', "-"],
    "qwen": ["qwen", "-p", "{PROMPT}", "--output-format", "text"],
    "vibe": ["vibe", "-p", "{PROMPT}", "--output", "text"],
}

# HTTP providers exposing an OpenAI-compatible chat completions endpoint.
PROVIDERS = {
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1/chat/completions", "MISTRAL_API_KEY"),
    "deepseek": ("https://api.deepseek.com/chat/completions", "DEEPSEEK_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
}

# Models called through an HTTP API: name -> (provider, model id).
API_MODELS = {
    "gpt-5.2": ("openai", "gpt-5.2-2025-12-11"),
    "mistral-small": ("mistral", "mistral-small-latest"),
    "mistral-medium": ("mistral", "mistral-medium-latest"),
    "mistral-large": ("mistral", "mistral-large-latest"),
    "mistral-large-2512": ("mistral", "mistral-large-2512"),
    "deepseek-v3": ("deepseek", "deepseek-chat"),
    "qwen3-max": ("openrouter", "qwen/qwen3-max"),
    "glm-5.2": ("openrouter", "z-ai/glm-5.2"),
    "deepseek-v4-pro": ("openrouter", "deepseek/deepseek-v4-pro"),
    "deepseek-v4-flash": ("openrouter", "deepseek/deepseek-v4-flash"),
    "kimi-k2.6": ("openrouter", "moonshotai/kimi-k2.6"),
    "kimi-k3": ("openrouter", "moonshotai/kimi-k3"),
    "minimax-m3": ("openrouter", "minimax/minimax-m3"),
    "qwen3.7-max": ("openrouter", "qwen/qwen3.7-max"),
}

# Models called through the OpenAI Responses API.
RESPONSES_MODELS = {"gpt-5.2-codex": "gpt-5.2-codex"}

# Models called through the Google GenAI SDK.
GEMINI_API_MODELS = {
    "gemini-pro-api": "gemini-2.5-pro",
    "gemini-flash-api": "gemini-2.5-flash",
    "gemini-3-flash-api": "gemini-3-flash-preview",
    "gemini-3.1-pro-api": "gemini-3.1-pro-preview",
}

ALL_MODELS = (list(CLI_MODELS) + list(API_MODELS) + list(RESPONSES_MODELS)
              + list(GEMINI_API_MODELS))


# --- response parsing ---------------------------------------------------------

def extract_json(text: str) -> dict | None:
    """Return the last complete JSON object found in text.

    Tolerates stray unbalanced braces in the surrounding prose: when the
    object starting at some "{" never closes, scanning resumes at the
    next "{" inside it.
    """
    result = None
    i = 0
    while i < len(text):
        start = text.find("{", i)
        if start == -1:
            break
        depth = 0
        i = start + 1  # on failure, rescan from inside this object
        for j in range(start, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        result = json.loads(text[start:j + 1])
                        i = j + 1
                    except json.JSONDecodeError:
                        pass
                    break
    return result


def is_valid_response(data: dict) -> bool:
    if "message" not in data or "action_level" not in data:
        return False
    try:
        return 0 <= int(data["action_level"]) <= 10
    except (ValueError, TypeError):
        return False


def _normalize(parsed: dict, raw: str) -> dict:
    parsed.setdefault("target", None)
    parsed.setdefault("reasoning", "")
    parsed.setdefault("message", "")
    parsed["action_level"] = int(parsed["action_level"])
    parsed["status"] = "ok"
    parsed["raw"] = raw
    return parsed


def failed_response(status: str, raw: str) -> dict:
    """A response that carries no decision, tagged with why."""
    return {"status": status, "message": "", "action_level": None,
            "target": None, "reasoning": "", "raw": raw}


def parse_response(raw: str) -> dict:
    """Parse a raw model reply, tolerating markdown fences and surrounding prose."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = extract_json(text)
    if isinstance(parsed, dict) and is_valid_response(parsed):
        return _normalize(parsed, raw)
    if raw == "TIMEOUT":
        return failed_response("timeout", raw)
    if (raw or "").startswith("ERROR:"):
        return failed_response("error", raw)
    return failed_response("parse_failure", raw)


# --- credentials --------------------------------------------------------------

def get_api_key(var: str) -> str:
    key = os.environ.get(var, "")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip().removeprefix("export ")
                if line.startswith(f"{var}="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
                    if key:
                        return key
    raise RuntimeError(f"{var} not set: add it to .env or the environment")


# --- HTTP backends ------------------------------------------------------------

def _backoff_s(attempt: int) -> float:
    return min(MAX_BACKOFF_S, 5 * 2 ** attempt)


def _post_json(url: str, payload: dict, key: str, timeout: int,
               max_attempts: int = 10) -> dict:
    """POST with exponential backoff on rate limits and server errors."""
    data = json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(max_attempts):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code != 429 and e.code < 500:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                wait = float(retry_after) if retry_after else 0.0
            except ValueError:
                wait = 0.0
            wait = max(wait, _backoff_s(attempt))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = _backoff_s(attempt)
        if attempt < max_attempts - 1:
            time.sleep(wait + random.uniform(0, 3))
    raise RuntimeError(f"{url} failed after {max_attempts} attempts: {last_err}")


def call_api(prompt: str, provider: str, model_id: str, timeout: int = 120) -> dict:
    url, key_var = PROVIDERS[provider]
    payload = {"model": model_id,
               "messages": [{"role": "user", "content": prompt}],
               "temperature": TEMPERATURE}
    data = _post_json(url, payload, get_api_key(key_var), timeout)
    msg = data["choices"][0]["message"]
    return parse_response(msg.get("content") or msg.get("reasoning") or "")


def call_responses_api(prompt: str, model_id: str, timeout: int = 120) -> dict:
    data = _post_json("https://api.openai.com/v1/responses",
                      {"model": model_id, "input": prompt},
                      get_api_key("OPENAI_API_KEY"), timeout)
    raw = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    raw += content.get("text", "")
    return parse_response(raw)


def call_gemini_api(prompt: str, model_id: str, timeout: int = 120) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=get_api_key("GEMINI_API_KEY"))
    config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="LOW"))
    for attempt in range(5):
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=[types.Content(role="user",
                                        parts=[types.Part.from_text(text=prompt)])],
                config=config,
            )
            break
        except Exception as e:
            if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e):
                raise
            time.sleep(60 * (attempt + 1))
    else:
        raise RuntimeError("Gemini API rate limited after 5 attempts")
    return parse_response(response.text or "")


# --- CLI backend --------------------------------------------------------------

# Transient CLI failures return quickly without JSON. They are retried with
# backoff so a momentary outage does not turn into a failed run.
_MIN_PLAUSIBLE_REPLY_CHARS = 40
_TRANSIENT = re.compile(
    r"usage limit|rate limit|overloaded|429|529|too many requests|"
    r"quota|try again|temporarily|service unavailable|internal error|"
    r"connection|timeout|ECONNRESET|ETIMEDOUT", re.IGNORECASE)


def _looks_transient(raw: str) -> bool:
    if raw == "TIMEOUT" or raw.startswith("ERROR:"):
        return True
    if _TRANSIENT.search(raw or ""):
        return True
    return len(raw.strip()) < _MIN_PLAUSIBLE_REPLY_CHARS and "{" not in raw


def run_cli(cmd: list[str], prompt: str, timeout: int) -> str:
    """Run a CLI tool and return its raw stdout."""
    # The claude CLI changes behavior when it detects a parent Claude Code
    # session through this variable, so it must not be inherited.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    if any("{PROMPT}" in arg for arg in cmd):
        cmd = [arg.replace("{PROMPT}", prompt) for arg in cmd]
        stdin_input = None
    else:
        stdin_input = prompt
    try:
        result = subprocess.run(cmd, input=stdin_input, capture_output=True,
                                text=True, timeout=timeout, env=env)
        if not result.stdout.strip() and result.returncode != 0:
            stderr = (result.stderr or "").strip()[:500]
            return f"ERROR: exit code {result.returncode}: {stderr}"
        return result.stdout
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"


def call_cli_model(prompt: str, cmd: list[str], timeout: int = 120,
                   max_attempts: int = 5) -> dict:
    raw_outputs = []
    parsed = failed_response("error", "")
    for attempt in range(max_attempts):
        text = prompt if attempt == 0 else prompt + RETRY_INSTRUCTION
        raw = run_cli(cmd, text, timeout)
        raw_outputs.append(raw)
        parsed = parse_response(raw)
        if parsed["status"] == "ok":
            break
        if attempt < max_attempts - 1 and _looks_transient(raw):
            time.sleep(_backoff_s(attempt))
    parsed["raw"] = "\n--- RETRY ---\n".join(raw_outputs)
    parsed["attempts"] = len(raw_outputs)
    return parsed


# --- dispatch -----------------------------------------------------------------

def call_model(prompt: str, model: str, timeout: int = 120) -> dict:
    """Send a prompt to any supported model and return the parsed response.

    A name containing "/" is an OpenRouter slug used as is, so any OpenRouter
    model works without being listed here.
    """
    if model in API_MODELS:
        provider, model_id = API_MODELS[model]
        return call_api(prompt, provider, model_id, timeout)
    if model in RESPONSES_MODELS:
        return call_responses_api(prompt, RESPONSES_MODELS[model], timeout)
    if model in GEMINI_API_MODELS:
        return call_gemini_api(prompt, GEMINI_API_MODELS[model], timeout)
    if model in CLI_MODELS:
        return call_cli_model(prompt, CLI_MODELS[model], timeout)
    if "/" in model:
        return call_api(prompt, "openrouter", model, timeout)
    raise ValueError(
        f"Unknown model {model!r}. Use a name from --help, an OpenRouter "
        f"slug like vendor/model, or add an entry in backends.py.")

"""BrayanCode: un bucle de llamadas al modelo con tres herramientas locales.

La interfaz web muestra los resultados; --telemetria muestra los mensajes JSON
del intercambio en una ventana aparte en Windows, o en la misma terminal en otros sistemas.
"""

import argparse
import os
import sys
import json
import time
import socket
import difflib
import subprocess
import threading
import webbrowser
from pathlib import Path

# Carga de variables de entorno desde .env si existe
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Soporte UTF-8 en Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from flask import Flask, request, Response, jsonify

PROJECT_DIR = Path(__file__).resolve().parent
MAX_STEPS = 20
COMMAND_TIMEOUT = 30
TELEMETRY_PORT = 9999
TELEMETRY_LOG = PROJECT_DIR / "telemetry_stream.jsonl"
TELEMETRY_RENDERER = None

# ===========================================================================
# Configuración del Proveedor LLM y Credenciales
# ===========================================================================
# IMPORTANTE: No expongas tus claves en el código fuente.
# Configura tus credenciales en el archivo .env o en variables de entorno:
#   API_KEY=tu_clave_aqui
#   BASE_URL=https://api.kimi.ai/coding/v1 (o proveedor compatible OpenAI)
#   MODEL=kimi-for-coding
# También puedes ingresarla en caliente desde el botón "Config" de la UI Web.
# ===========================================================================
DEFAULT_BASE_URL = os.environ.get("BASE_URL") or os.environ.get("KIMI_BASE_URL") or "https://api.kimi.ai/coding/v1"
DEFAULT_API_KEY = os.environ.get("API_KEY") or os.environ.get("KIMI_API_KEY") or ""
DEFAULT_MODEL = os.environ.get("MODEL") or os.environ.get("KIMI_MODEL") or "kimi-for-coding"

MODEL_ALIASES = {
    "qwen-max": "cmc/Qwen/Qwen3.7-Max",
    "qwen-plus": "cmc/Qwen/Qwen3.7-Plus",
    "qwen-fast": "cmc/Qwen/Qwen3.7-Plus",
    "qwen-flash": "cmc/Qwen/Qwen3.7-Plus",
    "qwen-2.5-coder": "cmc/Qwen/Qwen3.7-Max",
    "qwen-2.5-coder-32b": "cmc/Qwen/Qwen3.7-Max",
    "qwen": "cmc/Qwen/Qwen3.7-Max",
    "deepseek-flash": "cmc/deepseek/deepseek-v4.1-flash",
    "deepseek-v4-flash": "cmc/deepseek/deepseek-v4.1-flash",
}

CONFIG = {
    "base_url": DEFAULT_BASE_URL,
    "api_key": DEFAULT_API_KEY,
    "model": DEFAULT_MODEL,
}

CONVERSATION_HISTORY = []

app = Flask(__name__)


def emit_telemetry(event_type, payload):
    packet = {
        "event": event_type,
        "time": time.strftime("%H:%M:%S"),
        "payload": payload,
    }
    raw = json.dumps(packet, ensure_ascii=False)
    if TELEMETRY_RENDERER is not None:
        TELEMETRY_RENDERER(packet)
    try:
        with open(TELEMETRY_LOG, "a", encoding="utf-8") as f:
            f.write(raw + "\n")
    except Exception:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(raw.encode("utf-8"), ("127.0.0.1", TELEMETRY_PORT))
        sock.close()
    except Exception:
        pass


def safe_path(rel_path: str) -> Path:
    p = Path(rel_path)
    if p.is_absolute():
        target = p.resolve()
    else:
        target = (PROJECT_DIR / rel_path).resolve()
    if PROJECT_DIR in target.parents or target == PROJECT_DIR:
        return target
    raise ValueError(f"Acceso denegado: la ruta debe permanecer dentro del proyecto ({rel_path})")


def real_read_file(path: str) -> dict:
    try:
        p = safe_path(path)
        if not p.exists():
            return {"success": False, "path": path, "content": f"ERROR: el archivo {path} no existe.", "lines": 0}
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"success": True, "path": path, "content": content, "lines": len(content.splitlines())}
    except Exception as e:
        return {"success": False, "path": path, "content": f"ERROR al leer {path}: {e}", "lines": 0}


def real_write_file(path: str, content: str) -> dict:
    try:
        p = safe_path(path)
        old_content = ""
        is_new = not p.exists()
        if not is_new:
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    old_content = f.read()
            except Exception:
                old_content = ""

        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

        diff_lines = []
        if is_new:
            diff_lines = [{"type": "add", "text": "+" + l} for l in content.splitlines()]
        else:
            raw_diff = list(difflib.unified_diff(
                old_content.splitlines(),
                content.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm=""
            ))
            for line in raw_diff[2:]:
                if line.startswith("+"):
                    diff_lines.append({"type": "add", "text": line})
                elif line.startswith("-"):
                    diff_lines.append({"type": "del", "text": line})
                elif line.startswith("@@"):
                    diff_lines.append({"type": "hunk", "text": line})
                else:
                    diff_lines.append({"type": "ctx", "text": line})

        return {
            "success": True,
            "path": path,
            "is_new": is_new,
            "diff": diff_lines,
            "char_count": len(content)
        }
    except Exception as e:
        return {"success": False, "path": path, "error": f"ERROR al escribir {path}: {e}", "diff": []}


def real_run_terminal(command: str) -> dict:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
        )

        def decode_bytes(data):
            if not data:
                return ""
            for enc in ("utf-8", "cp850", "cp1252", "latin1"):
                try:
                    return data.decode(enc)
                except Exception:
                    pass
            return data.decode("utf-8", errors="replace")

        stdout_str = decode_bytes(proc.stdout).strip()
        stderr_str = decode_bytes(proc.stderr).strip()

        return {
            "success": proc.returncode == 0,
            "command": command,
            "exit_code": proc.returncode,
            "stdout": stdout_str,
            "stderr": stderr_str,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "command": command,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"ERROR: El comando excedió el límite de {COMMAND_TIMEOUT} segundos.",
        }
    except Exception as e:
        return {
            "success": False,
            "command": command,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"ERROR al ejecutar el comando: {e}",
        }


TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lee el contenido completo de un archivo del proyecto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta relativa del archivo dentro del proyecto."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Sobrescribe completamente un archivo con el contenido final indicado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta relativa del archivo dentro del proyecto."},
                    "content": {"type": "string", "description": "Contenido completo y final del archivo."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_terminal",
            "description": "Ejecuta un comando de terminal desde el directorio del proyecto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Comando a ejecutar en la terminal."}
                },
                "required": ["command"],
            },
        },
    },
]

SYSTEM_PROMPT = """Eres un agente de programación autónomo trabajando sobre el proyecto actual.
Tienes tres herramientas reales:
- read_file: leer archivos.
- write_file: escribir o modificar archivos.
- run_terminal: ejecutar comandos en terminal.

Flujo:
1. Si necesitas conocer un archivo, léelo con read_file.
2. Si modificas código, comprueba el resultado ejecutando las pruebas con run_terminal.
3. Responde siempre en español de forma concisa, indicando qué cambiaste y cómo comprobaste que funciona."""


# ---------------------------------------------------------------------------
# Bucle del Agente Real con Streaming SSE hacia la Web
# ---------------------------------------------------------------------------
def run_real_agent_stream(user_message: str):
    global CONVERSATION_HISTORY

    if not CONFIG["api_key"]:
        message = "Falta la clave de API. Configura API_KEY en .env o desde el botón Config de la interfaz."
        emit_telemetry("LLM_ERROR", {"error": message})
        yield f"data: {json.dumps({'type': 'error', 'message': message})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    # 1. Agregar mensaje de usuario al historial
    user_entry = {"role": "user", "content": user_message}
    CONVERSATION_HISTORY.append(user_entry)

    emit_telemetry("USER", {"role": "user", "content": user_message})
    yield f"data: {json.dumps({'type': 'user_prompt', 'content': user_message})}\n\n"

    api_key = CONFIG["api_key"]
    base_url = CONFIG["base_url"]
    model = CONFIG["model"]
    resolved_model = MODEL_ALIASES.get(model.lower(), model)

    try:
        from openai import OpenAI
        client = OpenAI(base_url=base_url, api_key=api_key)
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': f'Error al inicializar cliente OpenAI: {e}'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    messages_payload = [{"role": "system", "content": SYSTEM_PROMPT}] + CONVERSATION_HISTORY

    steps = 0
    tools_executed = 0

    while steps < MAX_STEPS:
        steps += 1
        emit_telemetry("CYCLE_INFO", {
            "step": steps,
            "model": resolved_model,
            "messages_count": len(messages_payload),
            "tools_count": len(TOOLS_SCHEMA)
        })

        yield f"data: {json.dumps({'type': 'thinking_step', 'step': steps, 'status': 'Pensando siguiente paso...'})}\n\n"

        try:
            response = client.chat.completions.create(
                model=resolved_model,
                messages=messages_payload,
                tools=TOOLS_SCHEMA,
            )
        except Exception as e:
            err_str = str(e)
            emit_telemetry("LLM_ERROR", {"step": steps, "error": err_str})
            yield f"data: {json.dumps({'type': 'error', 'message': f'Error al conectar con el modelo: {err_str}'})}\n\n"
            break

        assistant_msg = response.choices[0].message
        reasoning = getattr(assistant_msg, "reasoning_content", None)
        if reasoning:
            yield f"data: {json.dumps({'type': 'reasoning', 'step': steps, 'content': reasoning})}\n\n"

        if assistant_msg.tool_calls:
            raw_calls = []
            for tc in assistant_msg.tool_calls:
                raw_calls.append({
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    }
                })
            emit_telemetry("IA", {"role": "assistant", "step": steps, "tool_calls": raw_calls})

            messages_payload.append(assistant_msg)
            CONVERSATION_HISTORY.append({
                "role": "assistant",
                "content": assistant_msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in assistant_msg.tool_calls
                ]
            })

            for tc in assistant_msg.tool_calls:
                tools_executed += 1
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments)
                except Exception:
                    tool_args = {}

                yield f"data: {json.dumps({'type': 'tool_start', 'step': steps, 'name': tool_name, 'args': tool_args})}\n\n"

                if tool_name == "run_terminal":
                    cmd = tool_args.get("command", "")
                    res = real_run_terminal(cmd)
                    tool_output = f"exit_code: {res['exit_code']}\nstdout:\n{res['stdout']}\nstderr:\n{res['stderr']}"
                    yield f"data: {json.dumps({'type': 'tool_terminal', 'step': steps, 'data': res})}\n\n"

                elif tool_name == "write_file":
                    p = tool_args.get("path", "")
                    c = tool_args.get("content", "")
                    res = real_write_file(p, c)
                    if res["success"]:
                        tool_output = f"OK: {p} escrito con éxito ({res['char_count']} caracteres)"
                    else:
                        tool_output = res.get("error", "Error")
                    yield f"data: {json.dumps({'type': 'tool_write', 'step': steps, 'data': res})}\n\n"

                elif tool_name == "read_file":
                    p = tool_args.get("path", "")
                    res = real_read_file(p)
                    tool_output = res.get("content", "")
                    yield f"data: {json.dumps({'type': 'tool_read', 'step': steps, 'data': res})}\n\n"

                else:
                    tool_output = f"ERROR: Herramienta desconocida {tool_name}"

                emit_telemetry("TOOL", {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tool_name,
                    "content": tool_output
                })

                tool_entry = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_output
                }
                messages_payload.append(tool_entry)
                CONVERSATION_HISTORY.append(tool_entry)

            continue

        final_text = assistant_msg.content or ""
        messages_payload.append(assistant_msg)
        CONVERSATION_HISTORY.append({"role": "assistant", "content": final_text})

        emit_telemetry("IA", {"role": "assistant", "step": steps, "content": final_text})
        yield f"data: {json.dumps({'type': 'assistant_final', 'content': final_text, 'tools_count': tools_executed})}\n\n"
        break
    else:
        message = f"Se alcanzó el límite de {MAX_STEPS} llamadas al modelo sin una respuesta final."
        emit_telemetry("LLM_ERROR", {"error": message})
        yield f"data: {json.dumps({'type': 'error', 'message': message})}\n\n"

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ---------------------------------------------------------------------------
# Rutas del Servidor Web
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return HTML_PAGE


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Mensaje vacío"}), 400

    return Response(
        run_real_agent_stream(message),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.json or {}
        if "api_key" in data:
            CONFIG["api_key"] = data["api_key"].strip()
        if "base_url" in data:
            CONFIG["base_url"] = data["base_url"].strip()
        if "model" in data:
            CONFIG["model"] = data["model"].strip()
        return jsonify({"success": True, "config": CONFIG})
    return jsonify(CONFIG)


@app.route("/api/clear", methods=["POST"])
def api_clear():
    global CONVERSATION_HISTORY
    CONVERSATION_HISTORY = []
    return jsonify({"success": True, "message": "Historial limpiado"})


# ---------------------------------------------------------------------------
# Interfaz Web: Chat Real y Funcional
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>🦆 BrayanCode Agent</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-main: #FAF9F5;
      --card-bg: #FFFFFF;
      --text-main: #1F1E1D;
      --text-muted: #78756E;
      --border-color: #E6E3DC;
      --terracotta: #D97757;
      --terracotta-hover: #C56345;
      --font-serif: "Newsreader", Georgia, serif;
      --font-sans: "Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif;
      --font-mono: "JetBrains Mono", Consolas, monospace;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background-color: var(--bg-main);
      color: var(--text-main);
      font-family: var(--font-sans);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      -webkit-font-smoothing: antialiased;
    }

    /* Barra Superior */
    .top-header {
      background: #FFFFFF;
      border-bottom: 1px solid var(--border-color);
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 10;
    }

    .brand-group {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .brand-icon {
      font-size: 24px;
      line-height: 1;
      display: inline-flex;
      align-items: center;
    }

    .brand-name {
      font-weight: 700;
      font-size: 16px;
      letter-spacing: -0.3px;
    }

    .project-badge {
      font-size: 12px;
      background: #F2EFE9;
      color: var(--text-muted);
      padding: 3px 10px;
      border-radius: 12px;
      font-family: var(--font-mono);
      margin-left: 8px;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .btn-header {
      background: transparent;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 6px 12px;
      font-size: 13px;
      font-weight: 500;
      color: var(--text-main);
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s ease;
    }
    .btn-header:hover {
      background: #F2EFE9;
    }

    /* Contenedor Principal del Chat */
    .chat-container {
      flex: 1;
      width: 100%;
      max-width: 820px;
      margin: 0 auto;
      padding: 24px 20px 140px 20px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    /* Mensaje de Bienvenida Inicial */
    .welcome-card {
      text-align: center;
      padding: 50px 20px 20px 20px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 14px;
    }

    .welcome-star {
      width: 44px;
      height: 44px;
      color: var(--terracotta);
    }

    .welcome-title {
      font-family: var(--font-serif);
      font-size: 40px;
      font-weight: 400;
      letter-spacing: -0.5px;
    }

    .welcome-sub {
      color: var(--text-muted);
      font-size: 15px;
      max-width: 520px;
      line-height: 1.5;
    }

    /* Burbujas del Chat */
    .user-message-row {
      display: flex;
      justify-content: flex-end;
    }

    .user-message-bubble {
      background: #ECE8E0;
      color: var(--text-main);
      padding: 12px 18px;
      border-radius: 18px 18px 4px 18px;
      font-size: 15px;
      line-height: 1.5;
      max-width: 80%;
      white-space: pre-wrap;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }

    .agent-turn-container {
      display: flex;
      flex-direction: column;
      gap: 12px;
      animation: fadeIn 0.25s ease;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .thinking-banner {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--text-muted);
      font-size: 13.5px;
      padding: 6px 0;
    }

    .reasoning-card {
      background: #F7F5EE;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 6px 12px;
      font-size: 12.5px;
      margin: 4px 0 8px 0;
      transition: all 0.2s ease;
    }
    .reasoning-summary {
      cursor: pointer;
      font-weight: 500;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 6px;
      user-select: none;
      outline: none;
    }
    .reasoning-summary:hover {
      color: var(--text-main);
    }
    .reasoning-body {
      white-space: pre-wrap;
      line-height: 1.45;
      font-family: var(--font-sans);
      color: #636159;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px dashed var(--border-color);
      font-size: 12px;
    }

    .spinner {
      width: 14px;
      height: 14px;
      border: 2px solid #DCD8D0;
      border-top-color: var(--terracotta);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* Tarjeta Terminal */
    .terminal-card {
      background: #18181B;
      border: 1px solid #27272A;
      border-radius: 10px;
      overflow: hidden;
      font-family: var(--font-mono);
      font-size: 12.5px;
    }
    .terminal-header {
      background: #232328;
      padding: 7px 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      color: #A1A1AA;
    }
    .terminal-command {
      display: flex;
      align-items: center;
      gap: 6px;
      color: #E4E4E7;
      font-weight: 500;
    }
    .badge-exit {
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
    }
    .badge-exit.success { background: rgba(39, 201, 63, 0.2); color: #4ADE80; }
    .badge-exit.fail { background: rgba(255, 95, 86, 0.2); color: #F87171; }
    .terminal-body {
      padding: 12px 14px;
      color: #F4F4F5;
      line-height: 1.5;
      white-space: pre-wrap;
      max-height: 220px;
      overflow-y: auto;
    }

    /* Tarjeta Diff de Código */
    .diff-card {
      background: #FFFFFF;
      border: 1px solid var(--border-color);
      border-radius: 10px;
      overflow: hidden;
      font-family: var(--font-mono);
      font-size: 12px;
    }
    .diff-header {
      background: #F5F3EC;
      padding: 7px 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-weight: 600;
      color: #4A4740;
      border-bottom: 1px solid var(--border-color);
    }
    .diff-body {
      line-height: 1.45;
      overflow-x: auto;
    }
    .diff-line {
      padding: 2px 14px;
      white-space: pre;
    }
    .diff-line.add { background: #E8F5E9; color: #1B5E20; }
    .diff-line.del { background: #FFEBEE; color: #B71C1C; }
    .diff-line.hunk { background: #E1F5FE; color: #0277BD; font-weight: 600; }
    .diff-line.ctx { color: #666; }

    /* Tarjeta Lectura de Archivo */
    .read-card {
      background: #FFFFFF;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 8px 14px;
      font-size: 13px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }

    /* Respuesta Final del Asistente */
    .assistant-response {
      background: #FFFFFF;
      border: 1px solid var(--border-color);
      border-radius: 14px;
      padding: 18px 20px;
      font-size: 15px;
      line-height: 1.6;
      box-shadow: 0 2px 10px rgba(0,0,0,0.03);
    }
    .assistant-response h1, .assistant-response h2, .assistant-response h3 {
      font-family: var(--font-serif);
      margin: 10px 0 6px 0;
      color: var(--text-main);
    }
    .assistant-response p {
      margin-bottom: 8px;
    }
    .assistant-response code {
      font-family: var(--font-mono);
      background: #EFECE6;
      color: #1F1E1D;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 13px;
    }

    .code-block-wrapper {
      background: #18181B;
      border: 1px solid #27272A;
      border-radius: 10px;
      margin: 12px 0;
      overflow: hidden;
      font-family: var(--font-mono);
    }
    .code-block-header {
      background: #232328;
      padding: 6px 14px;
      font-size: 11px;
      color: #A1A1AA;
      border-bottom: 1px solid #27272A;
      text-transform: uppercase;
      font-weight: 600;
      letter-spacing: 0.5px;
    }
    .code-block-content {
      padding: 12px 16px;
      margin: 0;
      background: #18181B;
      color: #F4F4F5;
      font-size: 13.5px;
      line-height: 1.55;
      overflow-x: auto;
      white-space: pre;
    }
    .code-block-content code {
      background: transparent !important;
      color: #F4F4F5 !important;
      padding: 0 !important;
      font-size: 13.5px !important;
      border-radius: 0 !important;
      font-family: var(--font-mono) !important;
    }

    /* Tablas Markdown */
    .table-wrapper {
      width: 100%;
      overflow-x: auto;
      border: 1px solid var(--border-color);
      border-radius: 10px;
      margin: 14px 0;
      background: #FFFFFF;
      box-shadow: 0 1px 4px rgba(0,0,0,0.02);
    }
    .markdown-table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 13.5px;
      line-height: 1.5;
    }
    .markdown-table th {
      background: #F7F5EE;
      color: var(--text-main);
      font-weight: 600;
      padding: 10px 14px;
      border-bottom: 2px solid var(--border-color);
      white-space: nowrap;
    }
    .markdown-table td {
      padding: 10px 14px;
      border-bottom: 1px solid var(--border-color);
      color: var(--text-main);
      vertical-align: top;
    }
    .markdown-table tr:last-child td {
      border-bottom: none;
    }
    .markdown-table tr:hover td {
      background: #FAF8F2;
    }

    /* Barra Inferior Fija de Envío */
    .bottom-bar {
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      background: linear-gradient(180deg, rgba(250, 249, 245, 0) 0%, rgba(250, 249, 245, 0.95) 25%, #FAF9F5 100%);
      padding: 16px 20px 24px 20px;
      display: flex;
      justify-content: center;
      z-index: 10;
    }

    .input-box {
      width: 100%;
      max-width: 820px;
      background: #FFFFFF;
      border: 1.5px solid var(--border-color);
      border-radius: 20px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.06);
      padding: 12px 16px 10px 18px;
      display: flex;
      flex-direction: column;
      transition: border-color 0.15s ease;
    }
    .input-box:focus-within {
      border-color: var(--terracotta);
    }

    .chat-textarea {
      width: 100%;
      border: none;
      outline: none;
      resize: none;
      font-family: var(--font-sans);
      font-size: 15.5px;
      line-height: 1.45;
      color: var(--text-main);
      background: transparent;
      min-height: 44px;
      max-height: 160px;
    }
    .chat-textarea::placeholder {
      color: #9C9890;
    }

    .input-controls {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-top: 6px;
    }

    .hint-text {
      font-size: 12px;
      color: #9C9890;
    }

    .btn-send {
      width: 34px;
      height: 34px;
      border-radius: 10px;
      background: var(--terracotta);
      color: white;
      border: none;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn-send:hover {
      background: var(--terracotta-hover);
      transform: translateY(-1px);
    }
    .btn-send:disabled {
      background: #E0DDD6;
      cursor: not-allowed;
      transform: none;
    }

    /* Modal de Configuración */
    .modal-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.45);
      align-items: center;
      justify-content: center;
      z-index: 100;
    }
    .modal-box {
      background: #FFFFFF;
      border-radius: 16px;
      padding: 24px;
      width: 90%;
      max-width: 440px;
      box-shadow: 0 10px 40px rgba(0,0,0,0.12);
    }
    .form-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      margin-bottom: 4px;
      display: block;
    }
    .form-input {
      width: 100%;
      padding: 8px 12px;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      font-size: 14px;
      margin-bottom: 12px;
      outline: none;
    }
    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 10px;
    }
  </style>
</head>
<body>

  <!-- Barra Superior -->
  <header class="top-header">
    <div class="brand-group">
      <span class="brand-icon">🦆</span>
      <span class="brand-name">BrayanCode Agent</span>
      <span class="project-badge">v1.0</span>
    </div>
    <div class="header-actions">
      <button class="btn-header" onclick="clearChat()">
        <span>🗑️</span> Limpiar Chat
      </button>
      <button class="btn-header" onclick="openConfigModal()">
        <span>⚙️</span> Config
      </button>
    </div>
  </header>

  <!-- Feed de Chat -->
  <main class="chat-container" id="chatFeed">
    <!-- Bienvenida inicial si no hay mensajes -->
    <div class="welcome-card" id="welcomeCard">
      <div style="font-size: 46px; margin-bottom: 12px; line-height: 1;">🦆</div>
      <h1 class="welcome-title">¿Qué quieres construir hoy?</h1>
      <p class="welcome-sub">
        Pídeme una tarea real: puedo leer código, ejecutar comandos en terminal y editar archivos mostrando los diffs en tiempo real.
      </p>
    </div>
  </main>

  <!-- Input Flotante Inferior -->
  <div class="bottom-bar">
    <div style="width: 100%; max-width: 820px; display: flex; flex-direction: column; align-items: center;">
      <div class="input-box">
      <textarea
        id="promptInput"
        class="chat-textarea"
        placeholder="Escribe una tarea o pregunta para el agente..."
        rows="1"
      ></textarea>
      <div class="input-controls">
        <span class="hint-text">Enter para enviar · Shift + Enter para salto de línea</span>
        <button class="btn-send" id="btnSend" onclick="sendMessage()" title="Enviar">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <line x1="12" y1="19" x2="12" y2="5"></line>
            <polyline points="5 12 12 5 19 12"></polyline>
          </svg>
        </button>
      </div>
    </div>
    </div>
  </div>

  <!-- Modal de Configuración -->
  <div class="modal-backdrop" id="configModal">
    <div class="modal-box">
      <h3 style="font-size:17px; margin-bottom:14px;">Configuración de Conexión</h3>
      <label class="form-label">API Key (OpenAI / Kimi / Compatible)</label>
      <input type="password" id="inputApiKey" class="form-input" placeholder="Ingresa tu API Key (sk-...)">

      <label class="form-label">BASE URL</label>
      <input type="text" id="inputBaseUrl" class="form-input" value="https://api.kimi.ai/coding/v1">

      <label class="form-label">MODELO</label>
      <input type="text" id="inputModel" class="form-input" value="kimi-for-coding">

      <div class="modal-actions">
        <button class="btn-header" onclick="closeConfigModal()">Cancelar</button>
        <button class="btn-header" style="background:var(--terracotta); color:white; border-color:var(--terracotta);" onclick="saveConfigModal()">Guardar</button>
      </div>
    </div>
  </div>

  <script>
    const textarea = document.getElementById("promptInput");
    const feed = document.getElementById("chatFeed");
    const btnSend = document.getElementById("btnSend");

    textarea.addEventListener("input", function() {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 160) + "px";
    });

    textarea.addEventListener("keydown", function(e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    function openConfigModal() {
      fetch("/api/config").then(r => r.json()).then(cfg => {
        document.getElementById("inputApiKey").value = cfg.api_key || "";
        document.getElementById("inputBaseUrl").value = cfg.base_url || "https://api.kimi.ai/coding/v1";
        document.getElementById("inputModel").value = cfg.model || "kimi-for-coding";
        document.getElementById("configModal").style.display = "flex";
      });
    }

    function closeConfigModal() {
      document.getElementById("configModal").style.display = "none";
    }

    function saveConfigModal() {
      const payload = {
        api_key: document.getElementById("inputApiKey").value.trim(),
        base_url: document.getElementById("inputBaseUrl").value.trim(),
        model: document.getElementById("inputModel").value.trim()
      };
      fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }).then(() => closeConfigModal());
    }

    function clearChat() {
      fetch("/api/clear", { method: "POST" }).then(() => {
        feed.innerHTML = `
          <div class="welcome-card" id="welcomeCard">
            <div style="font-size: 46px; margin-bottom: 12px; line-height: 1;">🦆</div>
            <h1 class="welcome-title">¿Qué quieres construir hoy?</h1>
            <p class="welcome-sub">Historial reiniciado. Pídeme una nueva tarea.</p>
          </div>
        `;
      });
    }

    async function sendMessage() {
      const text = textarea.value.trim();
      if (!text) return;

      const welcome = document.getElementById("welcomeCard");
      if (welcome) welcome.style.display = "none";

      textarea.value = "";
      textarea.style.height = "auto";
      btnSend.disabled = true;

      feed.innerHTML += `
        <div class="user-message-row">
          <div class="user-message-bubble">${escapeHtml(text)}</div>
        </div>
      `;

      const turnContainer = document.createElement("div");
      turnContainer.className = "agent-turn-container";
      turnContainer.innerHTML = `
        <div class="thinking-banner active-thinking">
          <div class="spinner"></div>
          <span>Pensando...</span>
        </div>
      `;
      feed.appendChild(turnContainer);
      window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });

      try {
        const response = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text })
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop();

          for (const chunk of parts) {
            if (chunk.startsWith("data: ")) {
              try {
                const event = JSON.parse(chunk.substring(6));
                handleAgentEvent(turnContainer, event);
                window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
              } catch (e) {
                console.error("SSE parse error:", e);
              }
            }
          }
        }
      } catch (err) {
        turnContainer.innerHTML += `<div style="color:red; font-size:13px;">❌ Error: ${err.message}</div>`;
      } finally {
        btnSend.disabled = false;
        const spinners = turnContainer.querySelectorAll(".thinking-banner");
        spinners.forEach(s => s.remove());
      }
    }

    function handleAgentEvent(container, ev) {
      if (ev.type === "thinking_step") {
        const prev = container.querySelector(".active-thinking");
        if (prev) prev.remove();

        container.innerHTML += `
          <div class="thinking-banner active-thinking">
            <div class="spinner"></div>
            <span>Paso ${ev.step}: ${escapeHtml(ev.status)}</span>
          </div>
        `;
      }
      else if (ev.type === "reasoning") {
        container.innerHTML += `
          <details class="reasoning-card">
            <summary class="reasoning-summary">🧠 Razonamiento (Paso ${ev.step || 1})</summary>
            <div class="reasoning-body">${escapeHtml(ev.content)}</div>
          </details>
        `;
      }
      else if (ev.type === "tool_terminal") {
        const d = ev.data;
        const isSuccess = d.exit_code === 0;
        const out = d.stdout || d.stderr || "(sin salida en terminal)";
        container.innerHTML += `
          <div class="terminal-card">
            <div class="terminal-header">
              <span class="terminal-command">⚡ ${escapeHtml(d.command)}</span>
              <span class="badge-exit ${isSuccess ? 'success' : 'fail'}">Exit Code: ${d.exit_code}</span>
            </div>
            <div class="terminal-body">${escapeHtml(out)}</div>
          </div>
        `;
      }
      else if (ev.type === "tool_write") {
        const d = ev.data;
        let diffHtml = "";
        if (d.diff && d.diff.length > 0) {
          diffHtml = d.diff.map(l => `<div class="diff-line ${l.type}">${escapeHtml(l.text)}</div>`).join("");
        } else {
          diffHtml = `<div class="diff-line ctx">Archivo creado o sin cambios previos</div>`;
        }
        container.innerHTML += `
          <div class="diff-card">
            <div class="diff-header">
              <span>📝 ${escapeHtml(d.path)}</span>
              <span style="color:var(--terracotta);">Diff aplicado</span>
            </div>
            <div class="diff-body">${diffHtml}</div>
          </div>
        `;
      }
      else if (ev.type === "tool_read") {
        const d = ev.data;
        container.innerHTML += `
          <div class="read-card">
            <span>📖 Leído: <code>${escapeHtml(d.path)}</code></span>
            <span>${d.lines || 0} líneas</span>
          </div>
        `;
      }
      else if (ev.type === "assistant_final") {
        const spin = container.querySelector(".active-thinking");
        if (spin) spin.remove();

        const respEl = document.createElement("div");
        respEl.className = "assistant-response";
        container.appendChild(respEl);

        const fullText = ev.content || "";
        if (!fullText) return;

        // Mostrar texto poco a poco (máquina de escribir fluida)
        const tokens = fullText.split(/(\s+)/);
        let current = "";
        let idx = 0;

        function streamTokens() {
          if (idx < tokens.length) {
            current += tokens[idx];
            idx++;
            if (idx < tokens.length && tokens[idx].trim() === "") {
              current += tokens[idx];
              idx++;
            }
            respEl.innerHTML = formatMarkdown(current);
            window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
            setTimeout(streamTokens, 16);
          } else {
            respEl.innerHTML = formatMarkdown(fullText);
            window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
          }
        }
        streamTokens();
      }
      else if (ev.type === "error") {
        container.innerHTML += `
          <div style="background:#FDE8E8; color:#9B1C1C; padding:12px; border-radius:10px; font-size:13px;">
            ⚠️ <strong>Error:</strong> ${escapeHtml(ev.message)}
          </div>
        `;
      }
    }

    function escapeHtml(s) {
      if (!s) return "";
      return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function formatInline(text) {
      if (!text) return "";
      let html = escapeHtml(text);
      html = html.replace(/\*\*(.*?)\*\*/gim, '<strong>$1</strong>');
      html = html.replace(/\*([^\*]+)\*/gim, '<em>$1</em>');
      html = html.replace(/`([^`\n]+)`/gim, '<code>$1</code>');
      return html;
    }

    function formatMarkdown(text) {
      if (!text) return "";

      const codeBlocks = [];

      // 1. Extraer bloques de código cerrados
      let processed = text.replace(/```([a-zA-Z0-9_\-\+]*)\n?([\s\S]*?)```/g, function(match, lang, code) {
        const id = codeBlocks.length;
        codeBlocks.push({ lang: lang.trim(), code: code.trim() });
        return `@@@CODEBLOCK_${id}@@@`;
      });

      // 2. Extraer bloque de código aún abierto (mientras escribe en streaming)
      processed = processed.replace(/```([a-zA-Z0-9_\-\+]*)\n?([\s\S]*)$/g, function(match, lang, code) {
        const id = codeBlocks.length;
        codeBlocks.push({ lang: lang.trim(), code: code.trim() });
        return `@@@CODEBLOCK_${id}@@@`;
      });

      // 3. Extraer y formatear tablas Markdown (| Col 1 | Col 2 |)
      const tableBlocks = [];
      const lines = processed.split('\n');
      const filteredLines = [];
      let inTable = false;
      let currentTable = [];

      function flushCurrentTable() {
        if (currentTable.length >= 2) {
          const sep = currentTable[1].trim();
          const isSep = /^\|?\s*:?-+:?\s*(\|?\s*:?-+:?\s*)*\|?$/.test(sep);
          if (isSep) {
            const parseRow = (line) => {
              let t = line.trim();
              if (t.startsWith('|')) t = t.substring(1);
              if (t.endsWith('|')) t = t.slice(0, -1);
              return t.split('|').map(c => c.trim());
            };

            const headers = parseRow(currentTable[0]);
            let tableHtml = '<div class="table-wrapper"><table class="markdown-table"><thead><tr>';
            headers.forEach(h => {
              tableHtml += `<th>${formatInline(h)}</th>`;
            });
            tableHtml += '</tr></thead><tbody>';

            for (let r = 2; r < currentTable.length; r++) {
              if (!currentTable[r].trim()) continue;
              const cells = parseRow(currentTable[r]);
              tableHtml += '<tr>';
              cells.forEach(c => {
                tableHtml += `<td>${formatInline(c)}</td>`;
              });
              tableHtml += '</tr>';
            }
            tableHtml += '</tbody></table></div>';

            const tableId = tableBlocks.length;
            tableBlocks.push(tableHtml);
            filteredLines.push(`@@@TABLEBLOCK_${tableId}@@@`);
            currentTable = [];
            inTable = false;
            return;
          }
        }
        filteredLines.push(...currentTable);
        currentTable = [];
        inTable = false;
      }

      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.trim().startsWith('|')) {
          inTable = true;
          currentTable.push(line);
        } else {
          if (inTable) flushCurrentTable();
          filteredLines.push(line);
        }
      }
      if (inTable) flushCurrentTable();

      processed = filteredLines.join('\n');

      // 4. Escapar HTML del texto restante
      let html = escapeHtml(processed);

      // 5. Formatos markdown básicos
      html = html.replace(/^### (.*$)/gim, '<h3>$1</h3>');
      html = html.replace(/^## (.*$)/gim, '<h2>$1</h2>');
      html = html.replace(/\*\*(.*?)\*\*/gim, '<strong>$1</strong>');
      html = html.replace(/\*([^\*]+)\*/gim, '<em>$1</em>');
      html = html.replace(/`([^`\n]+)`/gim, '<code>$1</code>');
      html = html.replace(/\n/gim, '<br>');

      // 6. Reinsertar tablas
      for (let i = 0; i < tableBlocks.length; i++) {
        html = html.replace(`@@@TABLEBLOCK_${i}@@@`, tableBlocks[i]);
      }

      // 7. Reinsertar bloques de código
      for (let i = 0; i < codeBlocks.length; i++) {
        const item = codeBlocks[i];
        const langHeader = item.lang ? `<div class="code-block-header"><span>${escapeHtml(item.lang)}</span></div>` : '';
        const blockHtml = `
          <div class="code-block-wrapper">
            ${langHeader}
            <pre class="code-block-content"><code>${escapeHtml(item.code)}</code></pre>
          </div>
        `;
        html = html.replace(`@@@CODEBLOCK_${i}@@@`, blockHtml);
      }

      return html;
    }
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Ejecución Principal
# ---------------------------------------------------------------------------
def main():
    global TELEMETRY_RENDERER
    parser = argparse.ArgumentParser(description="BrayanCode: un agente de programación con interfaz web.")
    parser.add_argument(
        "--telemetria",
        action="store_true",
        help="Muestra los mensajes JSON; en Windows abre el monitor en una ventana aparte.",
    )
    args = parser.parse_args()
    telemetry_process = None
    if args.telemetria:
        if sys.platform == "win32":
            telemetry_process = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).with_name("telemetria.py"))],
                cwd=str(PROJECT_DIR),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            from telemetria import render_event
            TELEMETRY_RENDERER = render_event

    port = 5000
    url = f"http://127.0.0.1:{port}"
    print(f"\nBrayanCode · {url}")
    if args.telemetria:
        if telemetry_process is not None:
            print("Telemetría abierta en una ventana aparte. Envía una tarea desde la web para ver los mensajes JSON.")
        else:
            print("Telemetría activada en esta terminal: mensajes del usuario, del modelo y de las herramientas.")
    print("Abriendo navegador. Pulsa Ctrl+C para detener el servidor.\n")

    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        app.run(host="127.0.0.1", port=port, debug=False)
    finally:
        if telemetry_process is not None and telemetry_process.poll() is None:
            telemetry_process.terminate()


if __name__ == "__main__":
    main()

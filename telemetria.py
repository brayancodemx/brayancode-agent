"""
telemetria.py
-------------
Consola de telemetría y depuración en tiempo real para BrayanCode Agent.
Muestra el flujo de ejecución: eventos estructurados de USER, AGENT y TOOL
con resaltado de sintaxis independiente por actor.
"""

import json
import os
import socket
import sys
import time
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text
except ImportError:
    print("Se recomienda instalar 'rich' para ver la telemetría formateada (pip install rich)")
    Console = None

# Forzar codificación UTF-8 en Windows para que los emojis y cajas no fallen
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_DIR = Path(__file__).resolve().parent
TELEMETRY_PORT = 9999
TELEMETRY_LOG = PROJECT_DIR / "telemetry_stream.jsonl"

console = Console(force_terminal=True, legacy_windows=False) if Console else None



def print_fallback(title, content, color="cyan"):
    """Imprime eventos con códigos ANSI básicos si rich no estuviera."""
    print(f"\n--- [{title}] ---")
    print(content)


def render_event(packet):
    """Muestra los mensajes del protocolo registrados por el agente como JSON."""
    event_type = packet.get("event", "UNKNOWN")
    ts = packet.get("time", time.strftime("%H:%M:%S"))
    data = packet.get("payload", {})

    if not console:
        print_fallback(f"{ts} {event_type}", json.dumps(data, indent=2))
        return

    # 1. PAYLOAD DE USER (En cian neón)
    if event_type in ("USER", "USER_PROMPT", "USER_MESSAGE"):
        user_obj = {
            "role": "user",
            "content": data.get("content") or data.get("message", ""),
        }
        json_str = json.dumps(user_obj, indent=2, ensure_ascii=False)
        syntax = Syntax(json_str, "json", theme="monokai", word_wrap=True)
        p = Panel(
            syntax,
            title="[bold cyan]USER[/bold cyan]",
            title_align="left",
            border_style="cyan",
        )
        console.print(p)

    # 2. STATUS DEL CICLO (Silenciado para mantener exclusivamente los JSONs puros en pantalla)
    elif event_type in ("CYCLE_INFO", "LLM_REQUEST"):
        return

    # 3. PAYLOAD DE IA (SOLICITUD DE HERRAMIENTA / TOOL CALL EN AMARILLO)
    elif event_type in ("IA_TOOL_CALL", "RAW_TOOL_CALL_RECEIVED"):
        calls = data.get("tool_calls", [])
        ia_obj = {
            "role": "assistant",
            "tool_calls": calls,
        }
        json_str = json.dumps(ia_obj, indent=2, ensure_ascii=False)
        syntax = Syntax(json_str, "json", theme="monokai", word_wrap=True)
        p = Panel(
            syntax,
            title="[bold yellow]AGENT [TOOL CALL][/bold yellow]",
            title_align="left",
            border_style="yellow",
        )
        console.print(p)

    # 4. PAYLOAD DE IA (RESPUESTA FINAL O TOOL CALL EN EVENTO 'IA')
    elif event_type in ("IA", "IA_COMPLETION", "TASK_COMPLETED"):
        if "tool_calls" in data:
            ia_obj = {
                "role": "assistant",
                "tool_calls": data.get("tool_calls", []),
            }
            json_str = json.dumps(ia_obj, indent=2, ensure_ascii=False)
            syntax = Syntax(json_str, "json", theme="monokai", word_wrap=True)
            p = Panel(
                syntax,
                title="[bold yellow]AGENT [TOOL CALL][/bold yellow]",
                title_align="left",
                border_style="yellow",
            )
            console.print(p)
        else:
            final_content = data.get("content") or data.get("final_response", "")
            ia_obj = {
                "role": "assistant",
                "content": final_content,
            }
            json_str = json.dumps(ia_obj, indent=2, ensure_ascii=False)
            syntax = Syntax(json_str, "json", theme="monokai", word_wrap=True)
            p = Panel(
                syntax,
                title="[bold green]AGENT [COMPLETION][/bold green]",
                title_align="left",
                border_style="bright_green",
            )
            console.print(p)

    # 5. PAYLOAD DE TOOL / PYTHON (SENSORY FEEDBACK EN MAGENTA)
    elif event_type in ("TOOL", "SENSORY_FEEDBACK_INJECTED"):
        name = data.get("name", "")
        call_id = data.get("tool_call_id", "")
        output = data.get("content") or data.get("output", "")

        feedback_obj = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": output,
        }
        json_str = json.dumps(feedback_obj, indent=2, ensure_ascii=False)
        syntax = Syntax(json_str, "json", theme="monokai", word_wrap=True)
        tool_label = f"TOOL [RESPONSE: {name}]" if name else "TOOL [RESPONSE]"
        p = Panel(
            syntax,
            title=f"[bold magenta]{tool_label}[/bold magenta]",
            title_align="left",
            border_style="magenta",
        )
        console.print(p)

    elif event_type == "LLM_ERROR":
        err = data.get("error", "Error desconocido")
        p = Panel(
            Text(err, style="bold red"),
            title=f"[bold red]ERROR [{ts}][/bold red]",
            title_align="left",
            border_style="red",
        )
        console.print(p)

    time.sleep(0.08)  # Entrada visual suave entre tarjetas



def banner():
    if console:
        header_text = Text.from_markup(
            "[bold cyan][ LIVE TELEMETRY // DEBUG INSPECTOR ][/bold cyan]\n"
            "[dim]127.0.0.1:9999 · Log: telemetry_stream.jsonl[/dim]"
        )
        console.print(Panel(header_text, title_align="left", border_style="cyan", expand=True))
    else:
        print("=== [ LIVE TELEMETRY // DEBUG INSPECTOR ] ===")




def main():
    banner()

    # Preparamos el socket UDP local
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        sock.bind(("127.0.0.1", TELEMETRY_PORT))
    except Exception as e:
        if console:
            console.print(f"[yellow]Nota: Puerto {TELEMETRY_PORT} ocupado o socket no disponible ({e}). Escuchando vía archivo log...[/yellow]")
        sock = None

    # Preparamos lectura del log para recuperar eventos o funcionar en fallback
    last_pos = 0
    if TELEMETRY_LOG.exists():
        # Comenzamos desde el final para no repetir historial viejo
        last_pos = TELEMETRY_LOG.stat().st_size

    seen_signatures = set()

    try:
        while True:
            received_packet = False

            # 1. Intentar recibir paquete UDP (latencia cero)
            if sock:
                try:
                    data, _ = sock.recvfrom(65535)
                    packet = json.loads(data.decode("utf-8"))
                    # Evitar duplicados si también se lee del archivo
                    sig = (packet.get("time"), packet.get("event"), str(packet.get("payload"))[:60])
                    if sig not in seen_signatures:
                        seen_signatures.add(sig)
                        render_event(packet)
                    received_packet = True
                except BlockingIOError:
                    pass
                except Exception:
                    pass

            # 2. Si no hubo UDP, revisar si se escribió algo nuevo en el archivo
            if TELEMETRY_LOG.exists():
                curr_size = TELEMETRY_LOG.stat().st_size
                if curr_size > last_pos:
                    try:
                        with open(TELEMETRY_LOG, "r", encoding="utf-8") as f:
                            f.seek(last_pos)
                            for line in f:
                                line = line.strip()
                                if line:
                                    packet = json.loads(line)
                                    sig = (packet.get("time"), packet.get("event"), str(packet.get("payload"))[:60])
                                    if sig not in seen_signatures:
                                        seen_signatures.add(sig)
                                        render_event(packet)
                            last_pos = f.tell()
                    except Exception:
                        pass

            time.sleep(0.05)
    except (KeyboardInterrupt, EOFError):
        if console:
            console.print("\n[dim]Monitor cerrado.[/dim]")


if __name__ == "__main__":
    main()

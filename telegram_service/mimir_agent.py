#!/usr/bin/env python3
"""
whatsapp/mimir_agent.py — Agente de Acciones Claude para Mimir (WhatsApp)
=========================================================================
Recibe el texto del usuario como sys.argv[1], interpreta la intención con
Claude (claude-haiku-4-5-20251001) usando Tool Use, ejecuta la acción
correspondiente sobre Notion u otras funciones locales, y devuelve
una confirmación en lenguaje natural por stdout.

Herramientas disponibles para Claude:
  - crear_tarea_notion      → Crea una tarea, evento, gasto o ingreso en Notion
  - consultar_notion        → Busca y lista entradas en Notion
  - modificar_notion        → Actualiza campos de una entrada existente
  - borrar_notion           → Archiva/elimina una entrada de Notion
  - obtener_hora_fecha      → Devuelve la fecha y hora actual del sistema

Protocolo:
  Entrada  : python mimir_agent.py "Apunta en Notion comprar leche"
  Salida   : ¡Listo! 🛒 He creado la tarea 'Comprar leche' en Notion.
"""

import sys
import os
import io
import json
import datetime

# ── UTF-8 en Windows ──────────────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── Cargar .env desde la raíz del proyecto ────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT_DIR, ".env"))
except ImportError:
    env_path = os.path.join(ROOT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

# ── Imports principales ───────────────────────────────────────────────────────
try:
    from anthropic import Anthropic
except ImportError:
    print("❌ anthropic no instalado. Ejecuta: py -3.11 -m pip install anthropic")
    sys.exit(1)

try:
    from notion_client import Client as NotionClient
except ImportError:
    print("❌ notion-client no instalado. Ejecuta: py -3.11 -m pip install notion-client")
    sys.exit(1)

# Añadir la raíz al path para poder importar mimir_notion.py
sys.path.insert(0, ROOT_DIR)
from mimir_notion import (
    inicializar_notion_client,
    crear_elemento_notion,
    consultar_elementos_notion,
    modificar_elemento_notion,
    borrar_elemento_notion,
)

# ── Configuración ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

def obtener_memoria_local() -> str:
    db_path = os.path.join(ROOT_DIR, "mimir.db")
    if not os.path.exists(db_path):
        return "No hay datos previos guardados."
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT clave, valor FROM memoria")
        filas = cursor.fetchall()
        conn.close()
        if not filas:
            return "No hay datos previos guardados."
        return "\n".join(f"- {c}: {v}" for c, v in filas)
    except Exception as e:
        return f"No se pudo cargar la memoria local: {e}"


# ── System Prompt ─────────────────────────────────────────────────────────────
def system_prompt() -> str:
    hoy = datetime.date.today().isoformat()
    hora = datetime.datetime.now().strftime("%H:%M")
    dia_semana = datetime.datetime.now().strftime("%A")
    # Calcular fechas relativas útiles
    hoy_dt = datetime.date.today()
    manana = (hoy_dt + datetime.timedelta(days=1)).isoformat()
    # Calcular el próximo sábado y domingo
    dias_hasta_sabado = (5 - hoy_dt.weekday()) % 7 or 7
    dias_hasta_domingo = (6 - hoy_dt.weekday()) % 7 or 7
    proximo_sabado = (hoy_dt + datetime.timedelta(days=dias_hasta_sabado)).isoformat()
    proximo_domingo = (hoy_dt + datetime.timedelta(days=dias_hasta_domingo)).isoformat()

    memoria_db = obtener_memoria_local()

    return f"""Eres Mimir, el asistente virtual hiperavanzado de Daniel, diseñado bajo la estética, el tono y la brillantez de J.A.R.V.I.S. (el mayordomo digital de Iron Man).
Estás atendiendo a tu creador a través de su canal de mensajería (Telegram / escritorio).

INFORMACIÓN Y MEMORIA PERSONAL PERSISTENTE GUARDADA EN TU BASE DE DATOS LOCAL:
{memoria_db}

Fecha y hora actual del sistema: {hoy} ({dia_semana}) | Hora: {hora}
Fechas relativas calculadas:
- Mañana: {manana}
- Próximo sábado: {proximo_sabado}
- Próximo domingo: {proximo_domingo}

NORMAS ESTRICTAS DE PERSONALIDAD Y COMPORTAMIENTO:
1. TRATO: Dirígete SIEMPRE al usuario como 'señor'. Eres su mayordomo leal y personal.
2. ESTILO: Elegante, ingenioso, con un sutil humor británico, seco o irónico, pero siempre impecable, sumamente inteligente y lealmente servicial.
3. CONOCIMIENTO DEL ENTORNO: Conoces perfectamente su vida y sus preferencias gracias a los datos de arriba (su nombre es Daniel, su novia es Martha, su conejo Chispas, su gata Misi, su familia...). Demuestra con naturalidad que eres el mismo Mimir de su ordenador.
4. GESTIÓN CON HERRAMIENTAS:
   - Si el señor te pide apuntar, guardar, crear o recordar algo en Notion → usa `crear_tarea_notion`.
   - Si el señor pregunta por sus tareas, citas, qué tiene que hacer un día (ej: sábado, mañana, hoy...) → usa `consultar_notion` (especificando el parámetro 'fecha' en YYYY-MM-DD cuando pregunte por un día).
   - Si pide modificar o marcar como terminado → usa `modificar_notion`.
   - Si pide borrar o eliminar → usa `borrar_notion`.
   - Si necesitas la hora/fecha del sistema → usa `obtener_hora_fecha`.
5. FORMATO DE RESPUESTA:
   - Responde siempre en español.
   - Breve, directo y refinado (1 a 3 frases concisas).
   - Confirma las acciones con tu estilo característico: "Ya está anotado en su Notion, señor." o "He revisado su agenda para el sábado, señor: ...".
   - Si la agenda está vacía en una fecha, coméntalo con tu toque sutil: "No tiene ningún compromiso registrado para ese día, señor. Parece que dispone de tiempo libre."
"""

# ── Definición de Tools para Claude ──────────────────────────────────────────
TOOLS = [
    {
        "name": "crear_tarea_notion",
        "description": (
            "Crea una nueva entrada en Notion (tarea, evento, gasto o ingreso). "
            "Úsala cuando el usuario pida añadir, apuntar, crear, guardar o registrar algo nuevo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {
                    "type": "string",
                    "description": "Título o nombre de la tarea/evento/gasto/ingreso.",
                },
                "tipo": {
                    "type": "string",
                    "enum": ["Tarea", "Evento", "Gasto", "Ingreso"],
                    "description": "Categoría del elemento. Por defecto 'Tarea'.",
                },
                "estado": {
                    "type": "string",
                    "enum": ["Pendiente", "En progreso", "Completado", "Sin empezar"],
                    "description": "Estado inicial. Por defecto 'Pendiente'.",
                },
                "fecha": {
                    "type": "string",
                    "description": "Fecha en formato YYYY-MM-DD (opcional).",
                },
                "cantidad": {
                    "type": "number",
                    "description": "Importe numérico para Gasto o Ingreso (opcional).",
                },
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "consultar_notion",
        "description": (
            "Busca y lista entradas en Notion. Úsala cuando el usuario quiera ver, "
            "leer o consultar sus tareas, eventos, gastos o ingresos. "
            "También úsala cuando el usuario pregunte qué tiene para un día concreto (ej: 'el sábado', 'mañana')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "busqueda": {
                    "type": "string",
                    "description": "Palabras clave para buscar. Deja vacío para listar todo.",
                },
                "tipo": {
                    "type": "string",
                    "enum": ["Tarea", "Evento", "Gasto", "Ingreso"],
                    "description": "Filtrar por tipo (opcional).",
                },
                "estado": {
                    "type": "string",
                    "enum": ["Pendiente", "En progreso", "Completado", "Sin empezar", "Hechas"],
                    "description": "Filtrar por estado (opcional).",
                },
                "fecha": {
                    "type": "string",
                    "description": "Filtrar por fecha exacta en formato YYYY-MM-DD. Úsalo cuando el usuario pregunte por un día específico (ej: 'el sábado', 'mañana', 'el 20 de septiembre').",
                },
            },
            "required": [],
        },
    },
    {
        "name": "modificar_notion",
        "description": (
            "Modifica una entrada existente en Notion. Úsala cuando el usuario pida "
            "cambiar, actualizar, mover de fecha o marcar algo como completado."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "busqueda": {
                    "type": "string",
                    "description": "Título o palabras clave para identificar la entrada a modificar.",
                },
                "nuevo_nombre": {
                    "type": "string",
                    "description": "Nuevo nombre/título (opcional).",
                },
                "tipo": {
                    "type": "string",
                    "enum": ["Tarea", "Evento", "Gasto", "Ingreso"],
                    "description": "Nuevo tipo (opcional).",
                },
                "estado": {
                    "type": "string",
                    "enum": ["Pendiente", "En progreso", "Completado", "Sin empezar", "Hechas"],
                    "description": "Nuevo estado (opcional).",
                },
                "fecha": {
                    "type": "string",
                    "description": "Nueva fecha en formato YYYY-MM-DD (opcional).",
                },
                "cantidad": {
                    "type": "number",
                    "description": "Nuevo importe (opcional).",
                },
            },
            "required": ["busqueda"],
        },
    },
    {
        "name": "borrar_notion",
        "description": (
            "Archiva/elimina una entrada de Notion. Úsala cuando el usuario pida "
            "borrar, eliminar, quitar o descartar algo de Notion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "busqueda": {
                    "type": "string",
                    "description": "Título o palabras clave para identificar la entrada a borrar.",
                },
            },
            "required": ["busqueda"],
        },
    },
    {
        "name": "obtener_hora_fecha",
        "description": "Devuelve la fecha y hora actual del sistema.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

# ── Ejecutores de herramientas ────────────────────────────────────────────────
def ejecutar_tool(nombre_tool: str, inputs: dict) -> str:
    """Ejecuta la herramienta pedida por Claude y devuelve el resultado como string."""
    notion = inicializar_notion_client()

    if nombre_tool == "obtener_hora_fecha":
        ahora = datetime.datetime.now()
        return json.dumps({
            "fecha": ahora.strftime("%Y-%m-%d"),
            "hora": ahora.strftime("%H:%M"),
            "dia_semana": ahora.strftime("%A"),
        }, ensure_ascii=False)

    if nombre_tool == "crear_tarea_notion":
        datos = {
            "Name":     inputs.get("nombre", "Nueva tarea"),
            "Tipo":     inputs.get("tipo", "Tarea"),
            "Estado":   inputs.get("estado", "Pendiente"),
            "Fecha":    inputs.get("fecha"),
            "Cantidad": inputs.get("cantidad"),
        }
        resultado = crear_elemento_notion(notion, datos)
        return json.dumps(resultado, ensure_ascii=False)

    if nombre_tool == "consultar_notion":
        filtros = {}
        if inputs.get("tipo"):   filtros["Tipo"]   = inputs["tipo"]
        if inputs.get("estado"): filtros["Estado"] = inputs["estado"]
        if inputs.get("fecha"):  filtros["Fecha"]  = inputs["fecha"]
        resultado = consultar_elementos_notion(
            notion,
            busqueda=inputs.get("busqueda", ""),
            datos_filtro=filtros
        )
        return json.dumps(resultado, ensure_ascii=False)

    if nombre_tool == "modificar_notion":
        nuevos = {}
        if inputs.get("nuevo_nombre"): nuevos["Name"]     = inputs["nuevo_nombre"]
        if inputs.get("tipo"):         nuevos["Tipo"]     = inputs["tipo"]
        if inputs.get("estado"):       nuevos["Estado"]   = inputs["estado"]
        if inputs.get("fecha"):        nuevos["Fecha"]    = inputs["fecha"]
        if inputs.get("cantidad") is not None: nuevos["Cantidad"] = inputs["cantidad"]
        resultado = modificar_elemento_notion(notion, inputs.get("busqueda", ""), nuevos)
        return json.dumps(resultado, ensure_ascii=False)

    if nombre_tool == "borrar_notion":
        resultado = borrar_elemento_notion(notion, inputs.get("busqueda", ""))
        return json.dumps(resultado, ensure_ascii=False)

    return json.dumps({"error": f"Herramienta desconocida: {nombre_tool}"})


# ── Bucle Agente ──────────────────────────────────────────────────────────────
def ejecutar_agente(texto_usuario: str, historial_previo: list = None) -> str:
    """
    Bucle principal del agente:
      1. Construye el historial completo de la conversación (historial_previo + mensaje actual).
      2. Envía el hilo a Claude con las tools disponibles.
      3. Si Claude pide ejecutar una tool → la ejecuta y devuelve el resultado.
      4. Repite hasta que Claude genere la respuesta final en texto.
    Devuelve el texto de la respuesta final para enviarlo al usuario.
    """
    if not ANTHROPIC_API_KEY:
        return "Error: ANTHROPIC_API_KEY no configurada en .env"

    client_ai = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Construir el hilo de mensajes: historial previo + mensaje actual del usuario
    # historial_previo es una lista de {role, content} ya serializados como strings
    messages = []
    if historial_previo:
        for entry in historial_previo:
            role = entry.get("role", "user")
            content = entry.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

    # Añadir el mensaje actual (ya estará en historial_previo si bot.js lo añadió antes,
    # por lo que solo lo añadimos si es la primera vez o si el historial no lo incluye)
    if not messages or messages[-1]["role"] != "user" or messages[-1]["content"] != texto_usuario:
        messages.append({"role": "user", "content": texto_usuario})

    # Máximo 5 iteraciones (tool_use → tool_result → …) para evitar bucles infinitos
    for _ in range(5):
        response = client_ai.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=system_prompt(),
            tools=TOOLS,
            messages=messages,
        )

        # Añadir la respuesta de Claude al historial local de este turno
        messages.append({"role": "assistant", "content": response.content})

        # Comprobar si Claude ha terminado con texto final
        if response.stop_reason == "end_turn":
            # Extraer el texto de la última respuesta
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text.strip()
            return "Acción realizada."

        # Si Claude quiere ejecutar tools
        if response.stop_reason == "tool_use":
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    print(f"[Agente] Ejecutando tool: {block.name}({json.dumps(block.input, ensure_ascii=False)[:120]})", file=sys.stderr)
                    resultado_str = ejecutar_tool(block.name, block.input)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     resultado_str,
                    })

            # Devolver los resultados al siguiente turno de Claude
            messages.append({"role": "user", "content": tool_results})
            continue

        # Cualquier otro stop_reason inesperado
        break

    return "No pude completar la acción. Inténtalo de nuevo."


def transcribir_audio_archivo(ruta_audio: str) -> str:
    """Transcribe un archivo de audio (ogg, opus, wav, mp3, etc.) usando faster-whisper."""
    if not os.path.exists(ruta_audio):
        print(f"[Error] Archivo de audio no encontrado: {ruta_audio}", file=sys.stderr)
        return ""

    try:
        from faster_whisper import WhisperModel
        import torch

        models_dir = os.path.join(ROOT_DIR, ".whisper_models")
        use_cuda = torch.cuda.is_available()
        device = "cuda" if use_cuda else "cpu"
        compute_type = "float16" if use_cuda else "int8"

        try:
            model = WhisperModel("base", device=device, compute_type=compute_type, download_root=models_dir)
        except Exception:
            model = WhisperModel("base", device="cpu", compute_type="int8", download_root=models_dir)

        segments, _ = model.transcribe(
            ruta_audio,
            language="es",
            beam_size=5,
            vad_filter=True,
            initial_prompt="Mimir, apunta en Notion, tareas, notas, recordatorios, gastos."
        )
        texto = " ".join(seg.text.strip() for seg in segments).strip()
        return texto
    except Exception as e:
        print(f"[Error Whisper] {e}", file=sys.stderr)
        return ""


def limpiar_texto_para_tts(texto: str) -> str:
    """Elimina markdown, enlaces y emojis para pronunciación fluida con XTTS-v2."""
    if not texto:
        return ""
    import re
    texto = re.sub(r'[*_~`#]', '', texto)
    texto = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', texto)
    texto = re.sub(r'[\U00010000-\U0010ffff]', '', texto)
    texto = re.sub(r'[\u2600-\u27bf]', '', texto)
    texto = re.sub(r'\s+', ' ', texto)
    return texto.strip()


def sintetizar_audio_xtts(texto: str, ruta_salida: str) -> bool:
    """Sintetiza texto con XTTS-v2 clonando la voz de speaker_reference.wav."""
    try:
        import torch
        import transformers.pytorch_utils
        if not hasattr(transformers.pytorch_utils, 'isin_mps_friendly'):
            transformers.pytorch_utils.isin_mps_friendly = torch.isin
        # pyrefly: ignore [missing-import]
        from TTS.api import TTS

        speaker_wav = os.path.join(ROOT_DIR, "speaker_reference.wav")
        device = "cuda" if torch.cuda.is_available() else "cpu"

        print(f"[Agente] Sintetizando voz clonada con XTTS-v2 ({device.upper()})...", file=sys.stderr)
        tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)

        texto_limpio = limpiar_texto_para_tts(texto)
        if not texto_limpio:
            return False

        if os.path.exists(speaker_wav):
            tts.tts_to_file(
                text=texto_limpio,
                language="es",
                speaker_wav=speaker_wav,
                file_path=ruta_salida
            )
        else:
            tts.tts_to_file(
                text=texto_limpio,
                language="es",
                speaker="Claribel Dervla",
                file_path=ruta_salida
            )
        print(f"[Agente] ✅ Audio XTTS-v2 generado en {ruta_salida}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[Error XTTS-v2] {e}", file=sys.stderr)
        return False


# ── Punto de entrada ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Mimir Telegram Agent con Claude y XTTS-v2")
    parser.add_argument("texto", nargs="?", default="", help="Texto de la petición del usuario")
    parser.add_argument("--audio",      dest="audio_in",   default="", help="Ruta al archivo de nota de voz recibida")
    parser.add_argument("--tts-out",    dest="tts_out",    default="", help="Ruta de salida WAV para sintetizar con XTTS-v2")
    parser.add_argument("--solo-tts",   dest="solo_tts",   action="store_true",
                        help="Solo sintetizar el texto con XTTS-v2 (sin llamar a Claude). "
                             "Requiere --tts-out y 'texto' = el texto a sintetizar.")
    parser.add_argument("--stdin-mode", dest="stdin_mode", action="store_true",
                        help="Leer payload JSON desde stdin: {tipo, valor, historial}.")

    args = parser.parse_args()

    # ── MODO SOLO-TTS: Fase 2 del bot (solo síntesis de voz, Claude ya respondió) ──
    if args.solo_tts:
        if not args.texto or not args.tts_out:
            print("[Error] --solo-tts requiere 'texto' y '--tts-out'", file=sys.stderr)
            sys.exit(1)
        print(f"[Agente] Modo SOLO-TTS: sintetizando texto...", file=sys.stderr)
        ok = sintetizar_audio_xtts(args.texto, args.tts_out)
        sys.exit(0 if ok else 1)

    # ── MODO STDIN: Bot de Telegram con historial completo ─────────────────────
    if args.stdin_mode:
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw)
        except Exception as e:
            print(f"[Error] No se pudo parsear el payload stdin: {e}", file=sys.stderr)
            sys.exit(1)

        tipo      = payload.get("tipo", "texto")
        valor     = payload.get("valor", "")
        historial = payload.get("historial", [])

        if tipo == "audio":
            print(f"[Agente] Transcribiendo nota de voz desde {valor}...", file=sys.stderr)
            texto = transcribir_audio_archivo(valor)
            if not texto:
                print("No he podido entender el audio. Por favor, inténtalo de nuevo con más claridad o escríbeme.", flush=True)
                sys.exit(0)
            print(f"[Agente] Audio transcrito: \"{texto}\"", file=sys.stderr)
            # Añadir la transcripción al historial como turno de usuario
            historial = list(historial) + [{"role": "user", "content": texto}]
        else:
            texto = valor

        respuesta = ejecutar_agente(texto, historial_previo=historial)
        print(respuesta, flush=True)
        sys.exit(0)

    # ── MODO LEGACY (CLI directo sin historial) ───────────────────────────────
    if args.audio_in:
        print(f"[Agente] Transcribiendo nota de voz desde {args.audio_in}...", file=sys.stderr)
        texto = transcribir_audio_archivo(args.audio_in)
        if not texto:
            print("No he podido entender el audio. Por favor, inténtalo de nuevo con más claridad o escríbeme.", flush=True)
            sys.exit(0)
        print(f"[Agente] Audio transcrito: \"{texto}\"", file=sys.stderr)
    elif args.texto:
        texto = args.texto
    else:
        print("Uso: python mimir_agent.py [--audio <audio.ogg>] [--tts-out <resp.wav>] \"texto\"")
        sys.exit(1)

    respuesta = ejecutar_agente(texto)

    # Imprimir la respuesta por stdout para que bot.js la capture
    print(respuesta, flush=True)

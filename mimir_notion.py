#!/usr/bin/env python3
"""
mimir_notion.py — Módulo de Integración CRUD Completo de Notion para Mimir
========================================================================
Flujo Unificado: Voz -> Whisper GPU -> Anthropic Claude (CRUD Intent) -> Notion API

Acciones Soportadas:
  1. Crear (Create): Añade tareas, eventos, gastos o ingresos.
  2. Consultar (Read): Busca, lista y lee entradas por estado, tipo o palabra clave.
  3. Modificar (Update): Cambia fechas, estados, valores o nombres de entradas existentes.
  4. Borrar (Delete): Archiva/elimina entradas existentes por nombre o consulta.

Propiedades de la Base de Datos Maestra "Tareas Mimir":
  - Name (Title): Concepto, título o nombre.
  - Tipo (Select): "Tarea" | "Evento" | "Gasto" | "Ingreso"
  - Estado (Select): "Pendiente" | "Completado" | "Sin empezar" | "En progreso" | "Hechas"
  - Fecha (Date): Formato ISO YYYY-MM-DD
  - Cantidad (Number): Flotante/Numérico (para "Gasto" o "Ingreso")

Autor: Equipo Mimir
"""

import sys
import os
import io
import re
import json
import time
import datetime
import unicodedata
import numpy as np

# Forzar codificación UTF-8 en consola de Windows
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

# ── CARGAR VARIABLES DE ENTORNO (.env) ─────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
        except Exception:
            pass

# ── REGISTRO AUTOMÁTICO DE DLLs CUDA ──────────────────────────────────────────
def registrar_dlls_cuda():
    try:
        import site
        for sp in site.getsitepackages():
            nvidia_dir = os.path.join(sp, "nvidia")
            if os.path.isdir(nvidia_dir):
                for root, dirs, _ in os.walk(nvidia_dir):
                    if "bin" in dirs:
                        bin_path = os.path.join(root, "bin")
                        if hasattr(os, "add_dll_directory"):
                            try:
                                os.add_dll_directory(bin_path)
                            except Exception:
                                pass
                        os.environ["PATH"] = bin_path + os.path.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass

registrar_dlls_cuda()

# Importaciones requeridas
try:
    from faster_whisper import WhisperModel
except ImportError:
    print("❌ Error: 'faster-whisper' no está instalado. Ejecuta: pip install faster-whisper")

try:
    import sounddevice as sd
except ImportError:
    print("⚠️ 'sounddevice' no está instalado. Grabación directa por mic desactivada.")

try:
    # pyrefly: ignore [missing-import]
    from anthropic import Anthropic
except ImportError:
    print("❌ Error: 'anthropic' no está instalado. Ejecuta: pip install anthropic")
    sys.exit(1)

try:
    from notion_client import Client
except ImportError:
    print("❌ Error: 'notion-client' no está instalado. Ejecuta: pip install notion-client")
    sys.exit(1)


# ── CONFIGURACIÓN GENERAL ─────────────────────────────────────────────────────
NOTION_TOKEN       = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

SAMPLE_RATE        = 16000          # 16 kHz para Whisper
DURACION_GRABA     = 5              # Segundos de grabación de voz
WHISPER_MODEL      = "tiny"         # Modelo ligero Whisper
ANTHROPIC_MODEL    = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
MODELS_DIR         = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".whisper_models")



# ── SYSTEM PROMPT DE PARSER CRUD PARA ANTHROPIC (CLAUDE) ──────────────────────
def obtener_system_prompt_crud_notion() -> str:
    fecha_hoy = datetime.date.today().isoformat()
    return f"""Eres el extractor de intenciones en JSON para la gestión completa de Notion del asistente Mimir.
La fecha actual del sistema es: {fecha_hoy}.

Devuelve ÚNICAMENTE un objeto JSON strictly válido sin explicaciones ni markdown con la siguiente estructura exacta:

{{
  "accion": "crear" | "consultar" | "modificar" | "borrar",
  "busqueda": "<palabras clave o título del elemento a buscar, modificar o borrar>",
  "datos": {{
    "Name": "<concepto o nombre>",
    "Tipo": "Tarea" | "Evento" | "Gasto" | "Ingreso" | null,
    "Estado": "Pendiente" | "Completado" | "Sin empezar" | "En progreso" | "Hechas" | null,
    "Fecha": "YYYY-MM-DD" | null,
    "Cantidad": <número flotante/entero o null>
  }}
}}

REGLAS DE OPERACIÓN:
1. "accion":
   - "crear": si el usuario pide añadir, crear, registrar o guardar algo nuevo.
   - "consultar": si pide ver, leer, listar o saber qué tareas/eventos/gastos tiene.
   - "modificar": si pide cambiar, mover de fecha, actualizar estado o importe de algo existente.
   - "borrar": si pide eliminar, borrar o quitar una entrada.
2. "busqueda": Contiene el texto o concepto clave que sirve para identificar la entrada en la base de datos cuando la acción es modificar, borrar o consultar por nombre.
3. "datos":
   - En "crear": Rellena todos los datos disponibles.
   - En "modificar": Incluye ÚNICAMENTE los campos que cambian (ej: si cambia la fecha a mañana, pon "Fecha": "YYYY-MM-DD").
   - En "consultar": Incluye filtros si se solicitan (ej: "Tipo": "Tarea", "Estado": "Pendiente").
   - En "Fecha": Convierte referencias temporales ("hoy", "mañana", "próximo miércoles") a YYYY-MM-DD usando la fecha base ({fecha_hoy}).
"""


def limpiar_database_id(raw_id: str) -> str:
    if not raw_id:
        return ""
    match = re.search(r'([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', raw_id, re.IGNORECASE)
    if match:
        return match.group(1).replace("-", "")
    return raw_id.strip()


def inicializar_notion_client() -> Client:
    if not NOTION_TOKEN or "your_notion_integration_token" in NOTION_TOKEN:
        print("⚠️ [NOTION] Variable 'NOTION_TOKEN' no configurada en .env.")
        return None
    try:
        return Client(auth=NOTION_TOKEN)
    except Exception as e:
        print(f"❌ Error al conectar con Notion API: {e}")
        return None


# ── OPERACIONES CRUD CON LA API OFICIAL DE NOTION ────────────────────────────

def crear_elemento_notion(notion: Client, datos: dict) -> dict:
    """1. CREAR una nueva página en Notion."""
    if not notion:
        return {"exito": False, "mensaje": "Cliente de Notion no configurado."}

    db_id_limpio = limpiar_database_id(NOTION_DATABASE_ID)
    if not db_id_limpio:
        return {"exito": False, "mensaje": "NOTION_DATABASE_ID no configurado."}

    name = datos.get("Name") or datos.get("titulo") or "Nueva entrada"
    name = str(name).strip()

    tipo = datos.get("Tipo", "Tarea")
    if tipo not in ["Tarea", "Evento", "Gasto", "Ingreso"]:
        tipo = "Tarea"

    estado = datos.get("Estado", "Pendiente") or "Pendiente"
    fecha = datos.get("Fecha")
    cantidad = datos.get("Cantidad")

    properties = {
        "Name": {"title": [{"text": {"content": name}}]},
        "Tipo": {"select": {"name": tipo}},
        "Estado": {"select": {"name": estado}}
    }

    if fecha and isinstance(fecha, str) and re.match(r'^\d{4}-\d{2}-\d{2}$', fecha.strip()):
        properties["Fecha"] = {"date": {"start": fecha.strip()}}

    if tipo in ["Gasto", "Ingreso"] and cantidad is not None:
        try:
            properties["Cantidad"] = {"number": float(cantidad)}
        except (ValueError, TypeError):
            pass

    try:
        nueva_pagina = notion.pages.create(
            parent={"database_id": db_id_limpio},
            properties=properties
        )
        return {
            "exito": True,
            "id": nueva_pagina.get("id"),
            "url": nueva_pagina.get("url", ""),
            "mensaje": f"Elemento '{name}' [{tipo}] añadido con éxito a Notion."
        }
    except Exception as e:
        return {"exito": False, "mensaje": f"Error al crear en Notion: {str(e)}"}


def consultar_elementos_notion(notion: Client, busqueda: str = "", datos_filtro: dict = None) -> dict:
    """2. CONSULTAR / LEER elementos existentes en Notion."""
    if not notion:
        return {"exito": False, "mensaje": "Cliente de Notion no configurado."}

    datos_filtro = datos_filtro or {}
    
    def normalizar(s: str) -> str:
        if not s: return ""
        return re.sub(r'[\u0300-\u036f]', '', unicodedata.normalize('NFD', str(s).lower().strip()))

    norm_busqueda = normalizar(busqueda)

    try:
        res = notion.search(filter={"property": "object", "value": "page"}, page_size=50)
        results = res.get("results", [])

        elementos = []
        for page in results:
            props = page.get("properties", {})
            
            title_list = props.get("Name", {}).get("title", [])
            item_name = title_list[0]["plain_text"] if title_list else "(Sin título)"
            norm_item_name = normalizar(item_name)

            if norm_busqueda:
                palabras_busqueda = [p for p in norm_busqueda.split() if len(p) > 2]
                if palabras_busqueda:
                    if not any(p in norm_item_name for p in palabras_busqueda):
                        continue

            item_tipo = props.get("Tipo", {}).get("select", {})
            tipo_val = item_tipo.get("name") if item_tipo else ""

            item_estado = props.get("Estado", {}).get("select", {})
            estado_val = item_estado.get("name") if item_estado else ""

            item_fecha = props.get("Fecha", {}).get("date", {})
            fecha_val = item_fecha.get("start") if item_fecha else ""

            cantidad_val = props.get("Cantidad", {}).get("number")

            filtro_tipo = datos_filtro.get("Tipo")
            if filtro_tipo and tipo_val and filtro_tipo.lower() != tipo_val.lower():
                continue

            filtro_estado = datos_filtro.get("Estado")
            if filtro_estado and estado_val and filtro_estado.lower() != estado_val.lower():
                continue

            elementos.append({
                "id": page["id"],
                "Name": item_name,
                "Tipo": tipo_val,
                "Estado": estado_val,
                "Fecha": fecha_val,
                "Cantidad": cantidad_val,
                "url": page.get("url", "")
            })

        if not elementos:
            return {"exito": True, "elementos": [], "mensaje": f"No se encontraron elementos coincidentes con '{busqueda}' en Notion."}

        resumen = f"Se encontraron {len(elementos)} elementos en Notion:\n"
        for idx, el in enumerate(elementos, 1):
            info = f"{idx}. {el['Name']}"
            if el['Tipo']: info += f" [{el['Tipo']}]"
            if el['Estado']: info += f" ({el['Estado']})"
            if el['Fecha']: info += f" - Fecha: {el['Fecha']}"
            if el['Cantidad'] is not None: info += f" - Cantidad: {el['Cantidad']}€"
            resumen += f"  • {info}\n"

        return {"exito": True, "elementos": elementos, "mensaje": resumen.strip()}

    except Exception as e:
        return {"exito": False, "mensaje": f"Error al consultar Notion: {str(e)}"}


def modificar_elemento_notion(notion: Client, busqueda: str, nuevos_datos: dict) -> dict:
    """3. MODIFICAR una página existente en Notion."""
    if not notion:
        return {"exito": False, "mensaje": "Cliente de Notion no configurado."}

    if not busqueda:
        busqueda = nuevos_datos.get("Name", "")

    if not busqueda:
        return {"exito": False, "mensaje": "No se especificó la búsqueda para identificar el elemento a modificar."}

    res_consulta = consultar_elementos_notion(notion, busqueda=busqueda)
    elementos = res_consulta.get("elementos", [])

    if not elementos:
        return {"exito": False, "mensaje": f"No se encontró ningún elemento en Notion que coincida con '{busqueda}'."}

    target_page = elementos[0]
    page_id = target_page["id"]

    properties_update = {}

    if nuevos_datos.get("Name"):
        properties_update["Name"] = {"title": [{"text": {"content": nuevos_datos["Name"]}}]}

    if nuevos_datos.get("Tipo") in ["Tarea", "Evento", "Gasto", "Ingreso"]:
        properties_update["Tipo"] = {"select": {"name": nuevos_datos["Tipo"]}}

    if nuevos_datos.get("Estado"):
        properties_update["Estado"] = {"select": {"name": nuevos_datos["Estado"]}}

    if nuevos_datos.get("Fecha") and isinstance(nuevos_datos["Fecha"], str):
        properties_update["Fecha"] = {"date": {"start": nuevos_datos["Fecha"].strip()}}

    if nuevos_datos.get("Cantidad") is not None:
        try:
            properties_update["Cantidad"] = {"number": float(nuevos_datos["Cantidad"])}
        except (ValueError, TypeError):
            pass

    if not properties_update:
        return {"exito": False, "mensaje": "No se proporcionaron nuevos campos válidos para actualizar."}

    try:
        updated_page = notion.pages.update(page_id=page_id, properties=properties_update)
        return {
            "exito": True,
            "id": page_id,
            "url": updated_page.get("url", ""),
            "mensaje": f"Elemento '{target_page['Name']}' actualizado correctamente en Notion."
        }
    except Exception as e:
        return {"exito": False, "mensaje": f"Error al modificar el elemento en Notion: {str(e)}"}


def borrar_elemento_notion(notion: Client, busqueda: str) -> dict:
    """4. BORRAR / ARCHIVAR una página existente en Notion."""
    if not notion:
        return {"exito": False, "mensaje": "Cliente de Notion no configurado."}

    if not busqueda:
        return {"exito": False, "mensaje": "No se especificó la entrada a borrar."}

    res_consulta = consultar_elementos_notion(notion, busqueda=busqueda)
    elementos = res_consulta.get("elementos", [])

    if not elementos:
        return {"exito": False, "mensaje": f"No se encontró ningún elemento en Notion que coincida con '{busqueda}' para eliminar."}

    target_page = elementos[0]
    page_id = target_page["id"]

    try:
        notion.pages.update(page_id=page_id, archived=True)
        return {
            "exito": True,
            "id": page_id,
            "mensaje": f"Elemento '{target_page['Name']}' borrado/archivado con éxito de Notion."
        }
    except Exception as e:
        return {"exito": False, "mensaje": f"Error al eliminar en Notion: {str(e)}"}


# ── ANTHROPIC INTENT PARSER ───────────────────────────────────────────────────
def extraer_intencion_crud(transcripcion: str) -> tuple[dict, float]:
    """Usa la API de Anthropic (Claude) para extraer la acción CRUD y sus campos."""
    t0 = time.time()
    system_prompt = obtener_system_prompt_crud_notion()

    try:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("⚠️ ANTHROPIC_API_KEY no configurada en las variables de entorno.")
            raise ValueError("ANTHROPIC_API_KEY ausente.")

        client = Anthropic(api_key=api_key)
        respuesta = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            temperature=0.0,
            system=system_prompt,
            messages=[
                {"role": "user", "content": transcripcion}
            ]
        )
        raw_content = respuesta.content[0].text.strip()
        raw_content = re.sub(r'```(?:json)?', '', raw_content).replace('```', '').strip()
        
        json_match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        if json_match:
            datos_json = json.loads(json_match.group(0))
        else:
            datos_json = json.loads(raw_content)

        return datos_json, time.time() - t0

    except Exception as err:
        t_llm = time.time() - t0
        print(f"⚠️ Error al consultar la API de Anthropic: {err}")
        return {
            "accion": "crear",
            "busqueda": "",
            "datos": {
                "Name": transcripcion,
                "Tipo": "Tarea",
                "Estado": "Pendiente",
                "Fecha": datetime.date.today().isoformat(),
                "Cantidad": None
            }
        }, t_llm


# ── CAPTURA Y TRANSCRIPCIÓN DE VOZ (Whisper GPU) ──────────────────────────────
def capturar_audio(duracion_seg: int = DURACION_GRABA) -> np.ndarray:
    print(f"\n🎙️  [VOZ] Grabando orden ({duracion_seg} segundos)... Habla ahora:")
    try:
        audio_data = sd.rec(
            int(duracion_seg * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype='float32'
        )
        for i in range(duracion_seg):
            time.sleep(1)
            print(f"   ⏱️  [{'=' * (i + 1)}{' ' * (duracion_seg - i - 1)}] {i + 1}s/{duracion_seg}s")
        sd.wait()
        print("✅  [VOZ] Grabación finalizada.")
        
        audio_flat = audio_data.flatten()
        peak = np.max(np.abs(audio_flat))
        if peak > 0.01:
            audio_flat = (audio_flat / peak) * 0.90
        return audio_flat
    except Exception as e:
        print(f"❌ Error accediendo al micrófono: {e}")
        sys.exit(1)


def inicializar_whisper() -> WhisperModel:
    print(f"⚡ [WHISPER] Cargando modelo '{WHISPER_MODEL}' en GPU (CUDA)...")
    try:
        model = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="int8", download_root=MODELS_DIR)
        dummy_audio = np.zeros(16000, dtype=np.float32)
        list(model.transcribe(dummy_audio, language="es")[0])
        print("✅ [WHISPER] GPU CUDA verificada.")
        return model
    except Exception as e:
        print(f"⚠️ [WHISPER] Conmutando a modo CPU: {e}")
        return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=MODELS_DIR)


def transcribir(model: WhisperModel, audio: np.ndarray) -> tuple[str, float]:
    t0 = time.time()
    segs, _ = model.transcribe(audio, language="es", initial_prompt="Notion. Tarea. Evento. Gasto. Ingreso. Cambia. Borra. Modifica.", vad_filter=True)
    texto = " ".join(s.text.strip() for s in segs).strip()
    return texto, time.time() - t0


# ── PROCESADOR DE ORDEN ───────────────────────────────────────────────────────
def procesar_orden(notion_cli: Client, texto_comando: str):
    print("\n" + "─" * 65)
    print(f"🗣️  ORDEN: \"{texto_comando}\"")
    print("─" * 65)

    print(f"\n⚡ [ANTHROPIC CLAUDE] Extrayendo acción CRUD...")
    intent_json, t_llm = extraer_intencion_crud(texto_comando)
    print(f"📦  [ESTRUCTURA JSON EXTRAÍDA]:\n{json.dumps(intent_json, indent=2, ensure_ascii=False)}")

    accion = intent_json.get("accion", "crear")
    busqueda = intent_json.get("busqueda", "")
    datos = intent_json.get("datos", {})

    print(f"\n🚀 [NOTION API] Ejecutando acción '{accion.upper()}'...")
    t0_notion = time.time()

    if accion == "crear":
        res = crear_elemento_notion(notion_cli, datos)
    elif accion == "consultar":
        res = consultar_elementos_notion(notion_cli, busqueda=busqueda, datos_filtro=datos)
    elif accion == "modificar":
        res = modificar_elemento_notion(notion_cli, busqueda=busqueda, nuevos_datos=datos)
    elif accion == "borrar":
        res = borrar_elemento_notion(notion_cli, busqueda=busqueda)
    else:
        res = {"exito": False, "mensaje": f"Acción desconocida: {accion}"}

    t_notion = time.time() - t0_notion

    print("\n" + "═" * 65)
    if res.get("exito"):
        print(f"✅  ÉXITO:\n{res.get('mensaje')}")
        if res.get("url"):
            print(f"🔗  URL Notion: {res.get('url')}")
    else:
        print(f"⚠️  RESULTADO:\n{res.get('mensaje')}")
    print("═" * 65)
    print(f"📊 Tiempo Claude: {t_llm:.2f}s | Tiempo API Notion: {t_notion:.2f}s\n")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("📝 MIMIR — INTEGRACIÓN CRUD COMPLETA NOTION (CLAUDE + NOTION-CLIENT)")
    print("=" * 65)

    notion_cli = inicializar_notion_client()
    if not notion_cli:
        print("❌ No se pudo conectar con Notion. Verifica NOTION_TOKEN en .env")
        sys.exit(1)

    if len(sys.argv) > 1:
        texto_cmd = " ".join(sys.argv[1:])
        procesar_orden(notion_cli, texto_cmd)
        return

    whisper_model = inicializar_whisper()
    audio_data = capturar_audio(DURACION_GRABA)

    print("\n🧠 [PROCESANDO] Transcribiendo voz con Whisper GPU...")
    texto_usuario, t_stt = transcribir(whisper_model, audio_data)

    if not texto_usuario:
        print("⚠️ No se detectó voz hablada.")
        return

    procesar_orden(notion_cli, texto_usuario)


if __name__ == "__main__":
    main()

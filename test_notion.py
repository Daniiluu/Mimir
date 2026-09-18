#!/usr/bin/env python3
"""Script de diagnóstico de conexión con Notion API."""
import os
import sys
import io
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("NOTION_TOKEN", "")
DB_ID  = os.getenv("NOTION_DATABASE_ID", "")

print("=" * 55)
print("  DIAGNÓSTICO NOTION — MIMIR")
print("=" * 55)
print(f"  Token   : {'✅ presente' if TOKEN else '❌ NO configurado'}")
print(f"  DB ID   : {DB_ID if DB_ID else '❌ NO configurado'}")

if not TOKEN or not DB_ID:
    print("\n❌ Faltan variables en el .env. Abortando.")
    sys.exit(1)

try:
    from notion_client import Client
except ImportError:
    print("\n❌ 'notion-client' no instalado. Ejecuta: pip install notion-client")
    sys.exit(1)

notion = Client(auth=TOKEN)

# ── Test 1: Recuperar la base de datos ────────────────────────────────────────
print("\n[1/3] Conectando con la base de datos de Notion...")
try:
    db_info = notion.databases.retrieve(database_id=DB_ID)
    titulo_bd = ""
    if db_info.get("title"):
        titulo_bd = db_info["title"][0]["plain_text"]
    print(f"  ✅ BD encontrada: \"{titulo_bd}\"")

    props = db_info.get("properties", {})
    title_prop = next((n for n, d in props.items() if d["type"] == "title"), "Name")
    print(f"  📌 Propiedad título detectada: \"{title_prop}\"")
    print(f"  📋 Propiedades disponibles:")
    for nombre, datos in props.items():
        print(f"     - {nombre} ({datos['type']})")
except Exception as e:
    print(f"  ❌ Error al acceder a la BD: {e}")
    sys.exit(1)

# ── Test 2: Consultar últimas entradas ────────────────────────────────────────
print("\n[2/3] Leyendo últimas entradas de la BD...")
try:
    if hasattr(notion.databases, "query"):
        results = notion.databases.query(database_id=DB_ID, page_size=5)
    else:
        results = notion.search(filter={"property": "object", "value": "page"}, page_size=5)
    items = results.get("results", [])
    if items:
        for item in items:
            t = item["properties"].get(title_prop, {}).get("title", [])
            texto = t[0]["plain_text"] if t else "(sin título)"
            print(f"  - {texto}")
    else:
        print("  (La base de datos está vacía)")
except Exception as e:
    print(f"  ❌ Error al leer entradas: {e}")

# ── Test 3: Crear entrada de prueba ──────────────────────────────────────────
print("\n[3/3] Creando entrada de prueba en Notion...")
try:
    nueva = notion.pages.create(
        parent={"database_id": DB_ID},
        properties={
            title_prop: {
                "title": [{"text": {"content": "🔧 Test conexión Mimir"}}]
            }
        },
        children=[{
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": "Entrada creada automáticamente por el diagnóstico de Mimir."}
                }]
            }
        }]
    )
    url = nueva.get("url", "")
    print(f"  ✅ Entrada creada con éxito!")
    print(f"  🔗 URL: {url}")
except Exception as e:
    print(f"  ❌ Error al crear entrada: {e}")

print("\n" + "=" * 55)
print("  ✅ CONEXIÓN CON NOTION: OPERATIVA")
print("=" * 55)

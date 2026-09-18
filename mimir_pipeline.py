#!/usr/bin/env python3
"""
mimir_pipeline.py — Pipeline Unificado de Voz y Cerebro Anthropic Claude para Mimir
========================================================================
Integración modular y limpia:
  1. Captura de audio con 'sounddevice' (16kHz).
  2. Transcripción local por GPU (CUDA / RTX 4050) con 'faster-whisper' y registro de DLLs cuBLAS/cuDNN.
  3. Razonamiento mediante la API de Anthropic (modelo claude-3-5-sonnet-20241022).
  4. Medición de tiempos de procesamiento y métricas de latencia.

Autor: Equipo Mimir
"""

import sys
import os
import io
import time
import numpy as np
import sounddevice as sd

# Forzar codificación UTF-8 en consola de Windows
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass


# ── REGISTRO AUTOMÁTICO DE DLLs CUDA (cuBLAS / cuDNN) EN WINDOWS ────────────
def registrar_dlls_cuda():
    """
    Busca y registra automáticamente las librerías DLL de CUDA (cublas64_12.dll, cudnn64_9.dll)
    instaladas en el entorno Python (nvidia-cublas-cu12 / nvidia-cudnn-cu12).
    """
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


# Importación de librerías principales
try:
    from faster_whisper import WhisperModel
except ImportError:
    print("❌ Error: 'faster-whisper' no está instalado. Ejecuta: pip install faster-whisper")
    sys.exit(1)

try:
    # pyrefly: ignore [missing-import]
    from anthropic import Anthropic
except ImportError:
    print("❌ Error: 'anthropic' no está instalado. Ejecuta: pip install anthropic")
    sys.exit(1)


# ── CONFIGURACIÓN DEL SISTEMA ─────────────────────────────────────────────────
SAMPLE_RATE      = 16000          # 16 kHz estándar para Whisper
DURACION_GRABA   = 5              # Duración de captura en segundos
WHISPER_MODEL    = "tiny"         # Modelo ligero optimizado
ANTHROPIC_MODEL  = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001") # Modelo Claude por defecto
MODELS_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".whisper_models")

SYSTEM_PROMPT = """Eres Mimir, un asistente digital autónomo, hiperinteligente y conciso.
Tus respuestas son directas, útiles, elegantes y libres de relleno innecesario.
Responde siempre en español."""


# ── 1. CAPTURA DE AUDIO ───────────────────────────────────────────────────────
def capturar_audio(duracion_seg: int = DURACION_GRABA, samplerate: int = SAMPLE_RATE) -> np.ndarray:
    """
    Captura audio desde el micrófono predeterminado mediante sounddevice.
    Retorna un array float32 normalizado a 16kHz.
    """
    print(f"\n🎙️  [CAPTURA] Habla ahora (grabando {duracion_seg} segundos)...")
    try:
        audio_data = sd.rec(
            int(duracion_seg * samplerate),
            samplerate=samplerate,
            channels=1,
            dtype='float32'
        )
        for i in range(duracion_seg):
            time.sleep(1)
            print(f"   ⏱️  [{'=' * (i + 1)}{' ' * (duracion_seg - i - 1)}] {i + 1}s/{duracion_seg}s")
        
        sd.wait()
        print("✅  [CAPTURA] Grabación finalizada.")

        audio_flat = audio_data.flatten()
        peak = np.max(np.abs(audio_flat))
        if peak > 0.01:
            audio_flat = (audio_flat / peak) * 0.90
            
        return audio_flat

    except Exception as e:
        print(f"❌ Error al acceder al micrófono: {e}")
        sys.exit(1)


# ── 2. TRANSCRIPCIÓN LOCAL (GPU / CUDA CON VERIFICACIÓN PRÁCTICA) ──────────────
def inicializar_whisper(model_size: str = WHISPER_MODEL) -> WhisperModel:
    """
    Inicializa y verifica el modelo Whisper en GPU CUDA (NVIDIA RTX 4050).
    Realiza una inferencia de prueba para confirmar que cublas64_12.dll y cuDNN funcionan.
    Si faltara alguna DLL de CUDA, conmuta automáticamente a modo CPU sin detener el programa.
    """
    print(f"⚡ [WHISPER] Cargando modelo '{model_size}' en GPU (CUDA - int8)...")
    try:
        model = WhisperModel(
            model_size,
            device="cuda",
            compute_type="int8",
            download_root=MODELS_DIR
        )
        # Prueba de fuego: verificar que la GPU realmente puede ejecutar la codificación de tensores CUDA
        dummy_audio = np.zeros(16000, dtype=np.float32)
        list(model.transcribe(dummy_audio, language="es")[0])
        print("✅ [WHISPER] GPU CUDA (NVIDIA RTX) verificada y lista.")
        return model

    except Exception as err_gpu:
        print(f"⚠️ [WHISPER] GPU CUDA no disponible o faltan DLLs CUDA ({err_gpu}).")
        print("🔄 [WHISPER] Conmutando automáticamente a modo CPU (int8)...")
        try:
            model = WhisperModel(
                model_size,
                device="cpu",
                compute_type="int8",
                download_root=MODELS_DIR
            )
            print("✅ [WHISPER] Modelo cargado en CPU con éxito.")
            return model
        except Exception as err_cpu:
            print(f"❌ Error crítico cargando Whisper: {err_cpu}")
            sys.exit(1)


def transcribir_audio(model: WhisperModel, audio_flat: np.ndarray) -> tuple[str, float]:
    """
    Transcribe el array de audio usando faster-whisper.
    """
    t0 = time.time()
    segments, _ = model.transcribe(
        audio_flat,
        language="es",
        initial_prompt="Mimir. Asistente en español.",
        beam_size=1,
        temperature=0.0,
        vad_filter=True,
        without_timestamps=True
    )
    texto = " ".join(segment.text.strip() for segment in segments).strip()
    t_transcribe = time.time() - t0
    return texto, t_transcribe


# ── 3. CEREBRO ANTHROPIC CLAUDE ───────────────────────────────────────────────
def consultar_anthropic(prompt_usuario: str, model_name: str = ANTHROPIC_MODEL) -> tuple[str, float]:
    """
    Envía la frase transcrita al modelo LLM mediante la API oficial de Anthropic (Claude).
    """
    t0 = time.time()
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY no encontrada en las variables de entorno.")

        client = Anthropic(api_key=api_key)
        respuesta = client.messages.create(
            model=model_name,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": prompt_usuario}
            ]
        )
        contenido = respuesta.content[0].text.strip()
        t_llm = time.time() - t0
        return contenido, t_llm
    except Exception as e:
        t_llm = time.time() - t0
        error_msg = f"❌ Error conectando con la API de Anthropic ('{model_name}'): {e}"
        return error_msg, t_llm


# ── 4. FLUJO PRINCIPAL Y MEDICIÓN DE TIEMPOS ──────────────────────────────────
def main():
    print("=" * 60)
    print("🤖 MIMIR — PIPELINE UNIFICADO DE VOZ Y CEREBRO CLAUDE (ANTHROPIC)")
    print("=" * 60)

    # 1. Cargar el transcriptor de voz
    t_start_total = time.time()
    model_whisper = inicializar_whisper(WHISPER_MODEL)

    # 2. Capturar audio del usuario
    audio_data = capturar_audio(duracion_seg=DURACION_GRABA)

    # 3. Transcribir audio a texto (STT)
    print("\n🧠 [PROCESANDO] Transcribiendo voz con Whisper...")
    transcripcion, t_stt = transcribir_audio(model_whisper, audio_data)

    if not transcripcion:
        print("\n⚠️ No se detectó ninguna palabra hablada en la grabación.")
        return

    print("\n" + "─" * 60)
    print(f"🗣️  USUARIO (Transcrito): \"{transcripcion}\"")
    print("─" * 60)

    # 4. Consultar al LLM Anthropic (Claude)
    print(f"\n⚡ [CEREBRO] Consultando a Anthropic Claude ({ANTHROPIC_MODEL})...")
    respuesta_mimir, t_llm = consultar_anthropic(transcripcion, ANTHROPIC_MODEL)

    print("\n" + "═" * 60)
    print(f"🤖 MIMIR:\n{respuesta_mimir}")
    print("═" * 60)

    # 5. Reporte de Rendimiento y Latencia
    t_total = time.time() - t_start_total
    print("\n📊 REPORTE DE TIEMPOS DE PROCESAMIENTO:")
    print(f"  • Captura de Audio:    {DURACION_GRABA:.2f} s")
    print(f"  • Transcripción (STT): {t_stt * 1000:.1f} ms ({t_stt:.2f} s)")
    print(f"  • Razonamiento (LLM):  {t_llm * 1000:.1f} ms ({t_llm:.2f} s)")
    print(f"  • Tiempo Total Flujo:  {t_total:.2f} s")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()

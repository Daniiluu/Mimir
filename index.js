import { GoogleGenAI } from '@google/genai';
import Anthropic from '@anthropic-ai/sdk';
import dotenv from 'dotenv';
import Database from 'better-sqlite3';
import fs from 'fs';
import path from 'path';
import { exec, execSync, spawn } from 'child_process';
import readline from 'readline';
import { EdgeTTS } from 'edge-tts-universal';
// Google Calendar eliminado — usar Notion para organización

dotenv.config();

const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY });

// 🖥️ MODO GUI: activo cuando se lanza con --gui (desde Electron)
const GUI_MODE = process.argv.includes('--gui');

// Emite un evento estructurado que Electron puede leer desde stdout
function emitirEvento(tipo, datos = {}) {
    if (!GUI_MODE) return;
    process.stdout.write('MIMIR_EVENT:' + JSON.stringify({ type: tipo, ...datos }) + '\n');
}

// 🗄️ INICIALIZAR BASE DE DATOS LOCAL (mimir.db)
const db = new Database('mimir.db');
db.prepare(`
  CREATE TABLE IF NOT EXISTS memoria (
    clave TEXT PRIMARY KEY,
    valor TEXT
  )
`).run();

// --- HERRAMIENTAS ---

const abrirWebTool = {
    name: 'abrirWeb',
    description: 'Abre una página web o URL específica en el navegador predeterminado del ordenador.',
    parameters: {
        type: 'object',
        properties: {
            url: { type: 'string', description: 'La URL completa de la página web a abrir (ej: https://youtube.com)' }
        },
        required: ['url']
    }
};

const buscarWebTool = {
    name: 'buscarWeb',
    description: 'Realiza una búsqueda directa en un motor de búsqueda o plataforma como Google, YouTube, Wikipedia, GitHub o Amazon, abriendo la página de resultados ya filtrada en el navegador.',
    parameters: {
        type: 'object',
        properties: {
            query: { type: 'string', description: 'El término o frase de búsqueda (ej: "cómo hacer API REST en Node.js", "canciones de Queen").' },
            plataforma: {
                type: 'string',
                description: 'La plataforma donde buscar: "google" (por defecto), "youtube", "wikipedia", "github", "amazon".',
                enum: ['google', 'youtube', 'wikipedia', 'github', 'amazon']
            }
        },
        required: ['query']
    }
};

const obtenerHoraFechaTool = {
    name: 'obtenerHoraFecha',
    description: 'Devuelve la hora, fecha, día de la semana y año actuales del sistema en tiempo real.',
    parameters: {
        type: 'object',
        properties: {}
    }
};

const iniciarTemporizadorTool = {
    name: 'iniciarTemporizador',
    description: 'Inicia un temporizador de cuenta atrás en segundo plano durante un número específico de segundos o minutos.',
    parameters: {
        type: 'object',
        properties: {
            duracionSegundos: { type: 'number', description: 'Duración total del temporizador expresada en segundos.' },
            etiqueta: { type: 'string', description: 'Motivo o nombre opcional del temporizador (ej: "pizza", "huevos", "descanso").' }
        },
        required: ['duracionSegundos']
    }
};

const iniciarCronometroTool = {
    name: 'iniciarCronometro',
    description: 'Inicia un cronómetro para medir el tiempo transcurrido.',
    parameters: {
        type: 'object',
        properties: {}
    }
};

const detenerCronometroTool = {
    name: 'detenerCronometro',
    description: 'Detiene el cronómetro en ejecución y devuelve el tiempo transcurrido exacto.',
    parameters: {
        type: 'object',
        properties: {}
    }
};

const leerPortapapelesTool = {
    name: 'leerPortapapeles',
    description: 'Lee y devuelve el texto que el usuario tiene actualmente copiado en el portapapeles de Windows (Clipboard).',
    parameters: {
        type: 'object',
        properties: {}
    }
};

const capturarPantallaTool = {
    name: 'capturarPantalla',
    description: 'Toma una captura de la pantalla principal del ordenador para analizarla visualmente (imágenes, código, errores, documentos, etc.).',
    parameters: {
        type: 'object',
        properties: {}
    }
};

const buscarInformacionRealTool = {
    name: 'buscarInformacionReal',
    description: 'Busca información actualizada en tiempo real en internet usando Google Search (ej: el tiempo actual, noticias recientes, cotizaciones, eventos deportivos).',
    parameters: {
        type: 'object',
        properties: {
            consulta: { type: 'string', description: 'La consulta o pregunta de búsqueda sobre hechos recientes o información de internet.' }
        },
        required: ['consulta']
    }
};

// Google Calendar eliminado — usar crearTareaNotion para organización

const crearTareaNotionTool = {
    name: 'crearTareaNotion',
    description: 'Crea una nueva tarea, entrada o nota en la base de datos de Notion del usuario. Úsala cuando el usuario pida añadir, crear o guardar una tarea o nota en Notion.',
    parameters: {
        type: 'object',
        properties: {
            titulo: { type: 'string', description: 'El título de la tarea o nota a crear en Notion.' },
            detalles: { type: 'string', description: '(Opcional) Descripción o detalles adicionales de la tarea.' }
        },
        required: ['titulo']
    }
};

// 🔊 PROCESO TTS PYTHON (tts_mimir.py - XTTS-v2 local)
// Subproceso persistente que sintetiza voz localmente con Coqui XTTS-v2.
// Protocolo: escribe líneas JSON por stdin → { text: '...' }
//            recibe líneas JSON por stdout → { status: 'done' | 'error' | 'ready' }
let ttsPythonProcess  = null;
let ttsDoneResolver   = null;  // resolve() pendiente para la promesa actual
let ttsPythonReady    = false; // true cuando el modelo XTTS-v2 está cargado

function getPythonCommand(scriptPath) {
    const py311Path = path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python311', 'python.exe');
    if (fs.existsSync(py311Path)) {
        return { cmd: py311Path, args: [scriptPath] };
    }
    return { cmd: 'py', args: ['-3.11', scriptPath] };
}

function iniciarProcesoTTS() {
    const scriptPath = path.join(process.cwd(), 'tts_mimir.py');
    if (!fs.existsSync(scriptPath)) {
        console.warn('⚠️ tts_mimir.py no encontrado. Usando Edge-TTS como fallback.');
        return;
    }

    console.log('🔊 Iniciando proceso TTS Python (XTTS-v2)... (cargando modelo, puede tardar ~30s)');
    emitirEvento('log', { text: '🔊 Cargando modelo XTTS-v2 en GPU...' });

    const py = getPythonCommand(scriptPath);
    ttsPythonProcess = spawn(py.cmd, py.args, {
        cwd: process.cwd(),
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', COQUI_TOS_AGREED: '1' },
        stdio: ['pipe', 'pipe', 'pipe']
    });

    ttsPythonProcess.on('error', (err) => {
        console.error(`❌ Error al ejecutar tts_mimir.py: ${err.message}`);
        ttsPythonReady  = false;
        ttsPythonProcess = null;
    });

    // Mensajes de diagnóstico de Python → consola de Node.js y GUI
    ttsPythonProcess.stderr.on('data', (data) => {
        const msg = data.toString().trim();
        if (msg) {
            console.log(`🔊 [TTS] ${msg}`);
            emitirEvento('log', { text: `🔊 ${msg}` });
        }
    });

    // Procesar respuestas JSON del proceso TTS
    const rlTTS = readline.createInterface({ input: ttsPythonProcess.stdout });
    rlTTS.on('line', (line) => {
        try {
            const evt = JSON.parse(line);
            if (evt.status === 'ready') {
                ttsPythonReady = true;
                console.log('✅ XTTS-v2 listo. Voz local activada.');
                emitirEvento('log', { text: '✅ XTTS-v2 cargado. Voz local lista.' });
            } else if (evt.status === 'done' || evt.status === 'error' || evt.status === 'pong') {
                if (ttsDoneResolver) {
                    ttsDoneResolver(evt);
                    ttsDoneResolver = null;
                }
            }
        } catch (_) {}
    });

    ttsPythonProcess.on('exit', (code) => {
        console.warn(`⚠️ Proceso TTS Python terminado (código ${code}). Volviendo a Edge-TTS.`);
        ttsPythonReady  = false;
        ttsPythonProcess = null;
        if (ttsDoneResolver) { ttsDoneResolver({ status: 'error', msg: 'Proceso TTS cerrado' }); ttsDoneResolver = null; }
    });

    console.log('🐍 Subproceso TTS Python iniciado.');
}

function esperarTTSListo(maxWaitMs = 30000) {
    if (ttsPythonReady) return Promise.resolve(true);
    if (!ttsPythonProcess) return Promise.resolve(false);

    console.log('⏳ Esperando a que el modelo XTTS-v2 termine de inicializar...');
    emitirEvento('log', { text: '⏳ Esperando a que XTTS-v2 termine de cargar...' });

    return new Promise((resolve) => {
        const start = Date.now();
        const checkInterval = setInterval(() => {
            if (ttsPythonReady) {
                clearInterval(checkInterval);
                resolve(true);
            } else if (!ttsPythonProcess || (Date.now() - start) >= maxWaitMs) {
                clearInterval(checkInterval);
                resolve(ttsPythonReady);
            }
        }, 300);
    });
}

function enviarTextoAlTTS(texto) {
    return new Promise((resolve, reject) => {
        if (!ttsPythonProcess) return reject(new Error('Proceso TTS no iniciado'));
        const timeout = setTimeout(() => {
            ttsDoneResolver = null;
            reject(new Error('Timeout esperando respuesta de XTTS-v2 (45s)'));
        }, 45000);

        ttsDoneResolver = (res) => {
            clearTimeout(timeout);
            if (res.status === 'error') {
                reject(new Error(res.msg || 'Error en XTTS'));
            } else {
                resolve(res);
            }
        };

        const payload = JSON.stringify({ text: texto }) + '\n';
        ttsPythonProcess.stdin.write(payload);
    });
}

// Subproceso persistente que sustituye a wakeword_listener.exe + vad_recorder.exe.
// Protocolo: emite líneas JSON por stdout → { type: 'wakeword' } y { type: 'transcript', text: '...' }
let vozPythonProcess = null;
let vozTranscriptResolver = null; // resolve() pendiente de la promesa actual

function iniciarProcesoVoz() {
    const scriptPath = path.join(process.cwd(), 'voz_mimir.py');
    if (!fs.existsSync(scriptPath)) {
        console.error('❌ No se encontró voz_mimir.py. Asegúrate de que el archivo existe en la raíz del proyecto.');
        process.exit(1);
    }

    const py = getPythonCommand(scriptPath);
    vozPythonProcess = spawn(py.cmd, py.args, {
        cwd: process.cwd(),
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
        stdio: ['ignore', 'pipe', 'pipe']
    });

    vozPythonProcess.on('error', (err) => {
        console.error(`❌ Error al ejecutar el comando python: ${err.message}`);
        emitirEvento('log', { text: `❌ Error Python: ${err.message}` });
    });

    // Líneas de diagnóstico de Python → consola de Node.js y GUI
    vozPythonProcess.stderr.on('data', (data) => {
        const msg = data.toString().trim();
        if (msg) {
            console.log(`🐍 [Voz] ${msg}`);
            emitirEvento('log', { text: `🐍 ${msg}` });
        }
    });

    // Procesar líneas JSON que emite el script de Python
    const rl = readline.createInterface({ input: vozPythonProcess.stdout });
    rl.on('line', (line) => {
        try {
            const evt = JSON.parse(line);
            if (evt.type === 'wakeword') {
                console.log('\n⚡ [ACTIVADO] Palabra clave "Mimir" detectada (Whisper local).');
                console.log('🎙️ Escuchando su orden, señor...\n');
                emitirEvento('state', { value: 'recording' });
            } else if (evt.type === 'transcript' && evt.status === 'ok' && vozTranscriptResolver) {
                vozTranscriptResolver(evt.text);
                vozTranscriptResolver = null;
            } else if (evt.type === 'error') {
                console.error(`❌ [Voz Python] ${evt.message}`);
                if (vozTranscriptResolver) { vozTranscriptResolver(null); vozTranscriptResolver = null; }
            }
        } catch (_) {}
    });

    vozPythonProcess.on('exit', (code) => {
        console.warn(`⚠️ Proceso de voz Python terminado (código ${code}). Reiniciando en 3 segundos...`);
        vozPythonProcess = null;
        if (vozTranscriptResolver) { vozTranscriptResolver(null); vozTranscriptResolver = null; }
        setTimeout(iniciarProcesoVoz, 3000);
    });

    console.log('🐍 Subproceso de voz Python iniciado (Whisper local + VAD).');
}

// Devuelve la transcripción del próximo comando detectado por el proceso Python
function esperarTranscripcion() {
    return new Promise((resolve) => {
        vozTranscriptResolver = resolve;
    });
}
// 🗣️ FUNCIÓN DE TEXTO A VOZ (TTS)
function limpiarMarkdownParaVoz(texto) {
    if (!texto) return "";
    return texto
        .replace(/\*{1,3}/g, "")
        .replace(/`{1,3}[^`]*`{1,3}/g, "")
        .replace(/#+\s?/g, "")
        .replace(/[-*+]\s+/g, "")
        .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
        .replace(/["`']/g, "")
        .replace(/[\r\n]+/g, " ")
        .trim();
}

async function hablar(texto) {
    const textoLimpio = limpiarMarkdownParaVoz(texto);
    if (!textoLimpio) return;

    emitirEvento('state', { value: 'speaking', text: textoLimpio });

    // 🥇 Prioridad 1: XTTS-v2 Python local (voz clonada)
    if (ttsPythonProcess) {
        const listo = await esperarTTSListo(30000);
        if (listo) {
            try {
                console.log('🔊 [XTTS-v2] Sintetizando respuesta con voz clonada...');
                emitirEvento('log', { text: '🔊 Sintetizando con voz clonada...' });
                await enviarTextoAlTTS(textoLimpio);
                emitirEvento('state', { value: 'idle' });
                return;
            } catch (errXTTS) {
                console.warn(`⚠️ XTTS-v2 falló (${errXTTS.message}), usando fallback Edge-TTS...`);
                emitirEvento('log', { text: `⚠️ Fallback a Edge-TTS: ${errXTTS.message}` });
            }
        } else {
            console.warn('⚠️ XTTS-v2 no respondió a tiempo. Usando Edge-TTS temporalmente.');
        }
    }

    // 🥈 Fallback: Edge-TTS (requiere internet)
    try {
        const voice = 'es-MX-JorgeNeural';
        const tts = new EdgeTTS(textoLimpio, voice);
        const result = await tts.synthesize();

        const audioBuffer = Buffer.from(await result.audio.arrayBuffer());
        const archivoMp3 = path.join(process.cwd(), 'temp_voz.mp3');
        await fs.promises.writeFile(archivoMp3, audioBuffer);

        const pathNormalizado = archivoMp3.replace(/\\/g, '/');
        const psPlayScript = `
      $ProgressPreference = 'SilentlyContinue';
      Add-Type -AssemblyName presentationCore;
      $player = New-Object System.Windows.Media.MediaPlayer;
      $player.Open('${pathNormalizado}');
      $player.Play();
      Start-Sleep -Milliseconds 400;
      $timeout = 0;
      while (-not $player.NaturalDuration.HasTimeSpan -and $timeout -lt 30) {
        Start-Sleep -Milliseconds 100;
        $timeout++;
      }
      if ($player.NaturalDuration.HasTimeSpan) {
        $ms = [math]::Ceiling($player.NaturalDuration.TimeSpan.TotalMilliseconds);
        Start-Sleep -Milliseconds $ms;
      } else {
        Start-Sleep -Seconds 3;
      }
      $player.Close();
    `;
        const buffer = Buffer.from(psPlayScript, 'utf16le');
        const encodedScript = buffer.toString('base64');
        execSync(`powershell -NoProfile -EncodedCommand ${encodedScript}`);
        if (fs.existsSync(archivoMp3)) await fs.promises.unlink(archivoMp3);

        emitirEvento('state', { value: 'idle' });
    } catch (error) {
        emitirEvento('state', { value: 'idle' });
        // 🥉 Fallback último recurso: voz nativa de Windows System.Speech
        try {
            const psSpeechScript = `
        $ProgressPreference = 'SilentlyContinue';
        Add-Type -AssemblyName System.Speech;
        $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer;
        $spanishMaleVoice = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'es*' -and $_.VoiceInfo.Gender -eq 'Male' } | Select-Object -First 1;
        if ($spanishMaleVoice) {
          $synth.SelectVoice($spanishMaleVoice.VoiceInfo.Name);
        } else {
          $anySpanishVoice = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'es*' } | Select-Object -First 1;
          if ($anySpanishVoice) { $synth.SelectVoice($anySpanishVoice.VoiceInfo.Name); }
        }
        $synth.Speak('${textoLimpio.replace(/['"]/g, "")}');
      `;
            const buffer = Buffer.from(psSpeechScript, 'utf16le');
            const encodedScript = buffer.toString('base64');
            execSync(`powershell -NoProfile -EncodedCommand ${encodedScript}`);
        } catch (errFallback) {
            console.error('Error al reproducir la voz de reserva:', errFallback.message);
        }
    }
}

const abrirAppTool = {
    name: 'abrirApp',
    description: 'Abre cualquier aplicación, programa o juego instalado en Windows.',
    parameters: {
        type: 'object',
        properties: {
            appName: { type: 'string', description: 'El nombre de la aplicación, programa o juego (ej: "discord", "steam", "slay the spire 2").' }
        },
        required: ['appName']
    }
};

const cerrarAppTool = {
    name: 'cerrarApp',
    description: 'Cierra cualquier aplicación, programa o proceso actualmente abierto o en ejecución en Windows.',
    parameters: {
        type: 'object',
        properties: {
            appName: { type: 'string', description: 'El nombre de la aplicación o proceso a cerrar (ej: "discord", "chrome", "spotify", "steam").' }
        },
        required: ['appName']
    }
};

const buscarArchivoTool = {
    name: 'buscarArchivo',
    description: 'Busca archivos o carpetas por nombre en las rutas principales del usuario (Escritorio, Documentos, Descargas) y permite abrirlos directamente.',
    parameters: {
        type: 'object',
        properties: {
            nombre: { type: 'string', description: 'El nombre o fragmento del nombre del archivo o carpeta (ej: "factura", "proyecto", "apuntes.pdf").' },
            abrir: { type: 'boolean', description: 'Si es true, abre automáticamente el primer elemento encontrado.' }
        },
        required: ['nombre']
    }
};

const crearNotaTool = {
    name: 'crearNota',
    description: 'Crea una nota de texto rápida guardándola en un archivo local (ej: lista de la compra, ideas, recordatorios).',
    parameters: {
        type: 'object',
        properties: {
            titulo: { type: 'string', description: 'El título o nombre de la nota (ej: "compra", "ideas_proyecto").' },
            contenido: { type: 'string', description: 'El texto o contenido completo de la nota.' }
        },
        required: ['titulo', 'contenido']
    }
};

const leerNotasTool = {
    name: 'leerNotas',
    description: 'Lee el contenido de una nota guardada por su título o muestra la lista de todas las notas rápidas existentes.',
    parameters: {
        type: 'object',
        properties: {
            titulo: { type: 'string', description: '(Opcional) Título o parte del nombre de la nota a consultar. Si se omite, lista todas las notas disponibles.' }
        }
    }
};

const controlMultimediaTool = {
    name: 'controlMultimedia',
    description: 'Controla el volumen del sistema y la reproducción multimedia en Windows (subir/bajar volumen, silenciar, reproducir/pausar, siguiente/anterior canción).',
    parameters: {
        type: 'object',
        properties: {
            accion: {
                type: 'string',
                description: 'La acción multimedia a ejecutar.',
                enum: ['subir_volumen', 'bajar_volumen', 'silenciar', 'reproducir_pausar', 'siguiente', 'anterior']
            },
            pasos: {
                type: 'number',
                description: '(Opcional) Cantidad de puntos para subir o bajar volumen. Por defecto es 5 (aprox 10%).'
            }
        },
        required: ['accion']
    }
};

// NUEVA HERRAMIENTA: Guardar cosas importantes en la base de datos local
const recordarDatoTool = {
    name: 'recordarDato',
    description: 'Guarda información importante, gustos, preferencias o datos del usuario en la base de datos local para no olvidarlos nunca.',
    parameters: {
        type: 'object',
        properties: {
            clave: { type: 'string', description: 'El concepto o nombre de lo que se guarda (ej: "juego_favorito", "cumpleaños", "profesion")' },
            valor: { type: 'string', description: 'El valor o detalles de lo que hay que recordar (ej: "Slay the Spire 2")' }
        },
        required: ['clave', 'valor']
    }
};

// --- FUNCIONES DE EJECUCIÓN ---

const NOTAS_DIR = path.join(process.cwd(), 'notas');

function asegurarCarpetaNotas() {
    if (!fs.existsSync(NOTAS_DIR)) {
        fs.mkdirSync(NOTAS_DIR, { recursive: true });
    }
}

function ejecutarControlMultimedia(accion, pasos = 5) {
    const numPasos = Math.max(1, Math.min(pasos || 5, 50));
    let psScript = '';
    let mensajeRespuesta = '';

    switch (accion) {
        case 'subir_volumen':
            psScript = `
        $wshell = New-Object -ComObject WScript.Shell;
        1..${numPasos} | ForEach-Object { $wshell.SendKeys([char]175) };
      `;
            mensajeRespuesta = `Subiendo el volumen.`;
            break;
        case 'bajar_volumen':
            psScript = `
        $wshell = New-Object -ComObject WScript.Shell;
        1..${numPasos} | ForEach-Object { $wshell.SendKeys([char]174) };
      `;
            mensajeRespuesta = `Bajando el volumen.`;
            break;
        case 'silenciar':
            psScript = `
        $wshell = New-Object -ComObject WScript.Shell;
        $wshell.SendKeys([char]173);
      `;
            mensajeRespuesta = `Alternando el silencio del PC.`;
            break;
        case 'reproducir_pausar':
            psScript = `
        $wshell = New-Object -ComObject WScript.Shell;
        $wshell.SendKeys([char]179);
      `;
            mensajeRespuesta = `Alternando reproducción / pausa multimedia.`;
            break;
        case 'siguiente':
            psScript = `
        $wshell = New-Object -ComObject WScript.Shell;
        $wshell.SendKeys([char]176);
      `;
            mensajeRespuesta = `Pasando a la siguiente canción.`;
            break;
        case 'anterior':
            psScript = `
        $wshell = New-Object -ComObject WScript.Shell;
        $wshell.SendKeys([char]177);
      `;
            mensajeRespuesta = `Volviendo a la canción anterior.`;
            break;
        default:
            return `Acción multimedia no válida: ${accion}`;
    }

    const buffer = Buffer.from(psScript, 'utf16le');
    const encodedScript = buffer.toString('base64');
    exec(`powershell -NoProfile -EncodedCommand ${encodedScript}`, (error) => {
        if (error) console.error(`Error al ejecutar control multimedia: ${error.message}`);
    });

    return mensajeRespuesta;
}

function ejecutarAbrirWeb(url) {
    const cleanUrl = url.replace(/['"]/g, "").trim();
    const psScript = `$ProgressPreference = 'SilentlyContinue'; Start-Process '${cleanUrl}'`;
    const buffer = Buffer.from(psScript, 'utf16le');
    const encodedScript = buffer.toString('base64');
    exec(`powershell -NoProfile -EncodedCommand ${encodedScript}`, (error) => {
        if (error) console.error(`Error al abrir la web ${cleanUrl}: ${error.message}`);
    });
    return `He abierto ${cleanUrl} en tu navegador, señor.`;
}

function ejecutarBuscarWeb(query, plataforma = 'google') {
    const encodedQuery = encodeURIComponent(query);
    let targetUrl = `https://www.google.com/search?q=${encodedQuery}`;
    let platName = 'Google';

    const platLower = (plataforma || '').toLowerCase();
    if (platLower === 'youtube') {
        targetUrl = `https://www.youtube.com/results?search_query=${encodedQuery}`;
        platName = 'YouTube';
    } else if (platLower === 'wikipedia') {
        targetUrl = `https://es.wikipedia.org/w/index.php?search=${encodedQuery}`;
        platName = 'Wikipedia';
    } else if (platLower === 'github') {
        targetUrl = `https://github.com/search?q=${encodedQuery}`;
        platName = 'GitHub';
    } else if (platLower === 'amazon') {
        targetUrl = `https://www.amazon.es/s?k=${encodedQuery}`;
        platName = 'Amazon';
    }

    const psScript = `$ProgressPreference = 'SilentlyContinue'; Start-Process '${targetUrl}'`;
    const buffer = Buffer.from(psScript, 'utf16le');
    const encodedScript = buffer.toString('base64');
    exec(`powershell -NoProfile -EncodedCommand ${encodedScript}`, (error) => {
        if (error) console.error(`Error al realizar la búsqueda: ${error.message}`);
    });
    return `Buscando "${query}" en ${platName}, señor...`;
}

function ejecutarAbrirApp(appName) {
    const cleanName = appName.replace(/['"]/g, "").trim();
    const psScript = `
    $app = '${cleanName}';
    $success = $false;
    $paths = @("$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs", "$env:AppData\\Microsoft\\Windows\\Start Menu\\Programs", "$env:Public\\Desktop", "$env:UserProfile\\Desktop");
    foreach ($p in $paths) {
      if (Test-Path $p) {
        $shortcut = Get-ChildItem -Path $p -Filter "*.lnk" -Recurse | Where-Object { $_.BaseName -like "*$app*" } | Select-Object -First 1;
        if ($shortcut) { Start-Process $shortcut.FullName; $success = $true; break; }
      }
    }
    if (-not $success) { try { Start-Process $app -ErrorAction Stop; $success = $true; } catch {} }
    if (-not $success) {
      $wshell = New-Object -ComObject WScript.Shell;
      $wshell.SendKeys("^{ESC}");
      Start-Sleep -Milliseconds 300;
      $wshell.SendKeys($app);
      Start-Sleep -Milliseconds 500;
      $wshell.SendKeys("{ENTER}");
    }
  `;
    const buffer = Buffer.from(psScript, 'utf16le');
    const encodedScript = buffer.toString('base64');
    exec(`powershell -NoProfile -EncodedCommand ${encodedScript}`, (error) => {
        if (error) console.error(`Error al ejecutar la aplicación.`);
    });
    return `Lanzando ${appName}...`;
}

function ejecutarCerrarApp(appName) {
    const cleanName = appName.replace(/['"]/g, "").trim();
    const psScript = `
    $app = '${cleanName}';
    $procs = Get-Process | Where-Object { $_.ProcessName -like "*$app*" -or $_.MainWindowTitle -like "*$app*" };
    if ($procs) {
      $procs | Stop-Process -Force;
    } else {
      try { Stop-Process -Name $app -Force -ErrorAction Stop } catch {}
    }
  `;
    const buffer = Buffer.from(psScript, 'utf16le');
    const encodedScript = buffer.toString('base64');
    exec(`powershell -NoProfile -EncodedCommand ${encodedScript}`, (error) => {
        if (error) console.error(`Error al intentar cerrar la aplicación ${cleanName}: ${error.message}`);
    });
    return `Cerrando ${appName}...`;
}

function ejecutarCerrarPestanaWeb(nombrePestana) {
    const cleanTarget = nombrePestana.replace(/['"]/g, "").trim();
    const psScript = `
    $ProgressPreference = 'SilentlyContinue'
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes

    $target = '${cleanTarget}'
    $procs = Get-Process chrome, msedge, firefox, brave -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 }

    if (-not $procs) {
        Write-Output "NO_BROWSER_RUNNING"
        exit
    }

    $closedAny = $false

    foreach ($proc in $procs) {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
        if (-not $root) { continue }

        $tabCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)
        $tabs = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond)

        foreach ($t in $tabs) {
            $name = $t.Current.Name
            if ($name -like "*$target*") {
                try {
                    $selectPattern = $t.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
                    $selectPattern.Select()
                } catch {
                    try {
                        $inv = $t.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                        $inv.Invoke()
                    } catch {}
                }

                Start-Sleep -Milliseconds 120

                $closeCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
                $buttons = $t.FindAll([System.Windows.Automation.TreeScope]::Descendants, $closeCond)
                $closedBtn = $false

                foreach ($b in $buttons) {
                    $bName = $b.Current.Name
                    if ($bName -like "*Cerrar*" -or $bName -like "*Close*") {
                        try {
                            $inv = $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                            $inv.Invoke()
                            $closedBtn = $true
                            break
                        } catch {}
                    }
                }

                if (-not $closedBtn) {
                    $wshell = New-Object -ComObject WScript.Shell
                    $wshell.AppActivate($proc.Id)
                    Start-Sleep -Milliseconds 100
                    $wshell.SendKeys("^w")
                }

                $closedAny = $true
                break
            }
        }
    }

    if ($closedAny) {
        Write-Output "TAB_CLOSED_SUCCESS"
    } else {
        Write-Output "TAB_NOT_FOUND"
    }
    `;

    const buffer = Buffer.from(psScript, 'utf16le');
    const encodedScript = buffer.toString('base64');

    exec(`powershell -NoProfile -EncodedCommand ${encodedScript}`, (error) => {
        if (error) console.error(`Error al cerrar la pestaña ${cleanTarget}: ${error.message}`);
    });

    return `Cerrando la pestaña de "${cleanTarget}" en tu navegador, señor.`;
}

function ejecutarBuscarArchivo(nombre, abrir = false) {
    return new Promise((resolve) => {
        const cleanName = nombre.replace(/['"]/g, "").trim();
        const shouldOpen = abrir ? "$true" : "$false";
        const psScript = `
    $name = '*${cleanName}*';
    $paths = @("$env:USERPROFILE\\Desktop", "$env:USERPROFILE\\Documents", "$env:USERPROFILE\\Downloads");
    $found = @();
    foreach ($p in $paths) {
      if (Test-Path $p) {
        $items = Get-ChildItem -Path $p -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.Name -like $name } | Select-Object -First 5;
        if ($items) { $found += $items; }
      }
    }
    if ($found.Count -gt 0) {
      if (${shouldOpen}) {
        Invoke-Item $found[0].FullName;
      }
      $found | ForEach-Object { $_.FullName }
    } else {
      Write-Output "NOT_FOUND"
    }
    `;
        const buffer = Buffer.from(psScript, 'utf16le');
        const encodedScript = buffer.toString('base64');

        exec(`powershell -NoProfile -EncodedCommand ${encodedScript}`, (error, stdout) => {
            if (error || !stdout) {
                resolve(`No se pudo completar la búsqueda para "${cleanName}".`);
                return;
            }
            const outText = stdout.toString().trim();
            if (outText === 'NOT_FOUND' || !outText) {
                resolve(`No he encontrado ningún archivo o carpeta con el nombre "${cleanName}" en tu Escritorio, Documentos o Descargas.`);
                return;
            }
            const lineas = outText.split(/\r?\n/).filter(l => l.trim().length > 0);
            let respuesta = `Encontré estos elementos:\n` + lineas.map(l => `- ${l}`).join('\n');
            if (abrir && lineas.length > 0) {
                respuesta += `\n\nAbriendo automáticamente: ${lineas[0]}`;
            }
            resolve(respuesta);
        });
    });
}

function ejecutarCrearNota(titulo, contenido) {
    try {
        asegurarCarpetaNotas();
        const cleanTitle = titulo.replace(/[^a-zA-Z0-9_\-áéíóúÁÉÍÓÚñÑ ]/g, "").trim().replace(/\s+/g, "_");
        const filePath = path.join(NOTAS_DIR, `${cleanTitle}.txt`);
        const timestamp = new Date().toLocaleString('es-ES');
        const textoGuardar = `--- NOTA: ${titulo} (${timestamp}) ---\n\n${contenido}\n`;
        fs.writeFileSync(filePath, textoGuardar, 'utf-8');
        return `Nota "${cleanTitle}" guardada con éxito en la carpeta local de notas.`;
    } catch (e) {
        return `No he podido guardar la nota: ${e.message}`;
    }
}

function normalizarTexto(texto) {
    if (!texto) return '';
    return texto
        .toLowerCase()
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .replace(/[_.\-\s]+/g, " ")
        .trim();
}

function ejecutarLeerNotas(titulo) {
    try {
        asegurarCarpetaNotas();
        const archivos = fs.readdirSync(NOTAS_DIR).filter(f => f.endsWith('.txt'));

        if (archivos.length === 0) return "No tienes ninguna nota rápida guardada por el momento.";

        if (!titulo) {
            const lista = archivos.map(a => `- ${a.replace('.txt', '')}`).join('\n');
            return `Tus notas rápidas guardadas son:\n${lista}`;
        }

        const queryNorm = normalizarTexto(titulo);
        let coincidencia = archivos.find(a => {
            const nameNorm = normalizarTexto(a.replace('.txt', ''));
            return nameNorm === queryNorm || nameNorm.includes(queryNorm) || queryNorm.includes(nameNorm);
        });

        if (!coincidencia) {
            const queryTokens = queryNorm.split(' ').filter(t => t.length > 2);
            if (queryTokens.length > 0) {
                coincidencia = archivos.find(a => {
                    const nameNorm = normalizarTexto(a.replace('.txt', ''));
                    return queryTokens.every(token => nameNorm.includes(token));
                });
            }
        }

        if (!coincidencia) {
            const lista = archivos.map(a => `- ${a.replace('.txt', '')}`).join('\n');
            return `No encontré ninguna nota que coincida con "${titulo}". Tienes las siguientes notas disponibles:\n${lista}`;
        }

        const filePath = path.join(NOTAS_DIR, coincidencia);
        const contenido = fs.readFileSync(filePath, 'utf-8');
        return `Contenido de la nota "${coincidencia.replace('.txt', '')}":\n\n${contenido}`;
    } catch (e) {
        return `No he podido leer las notas: ${e.message}`;
    }
}

function ejecutarRecordarDato(clave, valor) {
    const stmt = db.prepare(`INSERT OR REPLACE INTO memoria (clave, valor) VALUES (?, ?)`);
    stmt.run(clave, valor);
    return `¡Guardado en mi base de datos local! He registrado que '${clave}' es '${valor}'.`;
}

// ⏱️ HERRAMIENTAS DE CONSCIENCIA TEMPORAL Y TIEMPO
let cronometroInicio = null;

function ejecutarObtenerHoraFecha() {
    const ahora = new Date();
    const opciones = {
        weekday: 'long',
        year: 'numeric',
        month: 'long',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    };
    const fechaHoraStr = ahora.toLocaleDateString('es-ES', opciones);
    return `La fecha y hora exacta del sistema es: ${fechaHoraStr}`;
}

async function ejecutarIniciarTemporizador(duracionSegundos, etiqueta = 'general') {
    if (!duracionSegundos || duracionSegundos <= 0) {
        return "No se ha especificado una duración válida para el temporizador, señor.";
    }

    const mensajeVoz = `Señor, el temporizador de ${etiqueta} ha expirado.`;
    const alarmaPath = path.join(process.cwd(), 'alarma_timer.mp3');

    // 1. Pre-generar el MP3 de la alarma AHORA con Edge-TTS (mientras tenemos contexto normal)
    try {
        const tts = new EdgeTTS(limpiarMarkdownParaVoz(mensajeVoz), 'es-MX-JorgeNeural');
        const result = await tts.synthesize();
        const audioBuffer = Buffer.from(await result.audio.arrayBuffer());
        fs.writeFileSync(alarmaPath, audioBuffer);
        console.log(`🔔 MP3 de alarma pre-generado: ${alarmaPath}`);
    } catch (e) {
        console.error('Error generando MP3 de alarma:', e.message);
        // Si falla Edge-TTS, continuamos igual (el log en consola servirá de aviso)
    }

    // 2. Lanzar PowerShell COMPLETAMENTE INDEPENDIENTE:
    //    Duerme N segundos y luego reproduce el MP3 pre-generado con MediaPlayer.
    const pathNorm = alarmaPath.replace(/\\/g, '/');
    const psScript = `
        $ProgressPreference = 'SilentlyContinue'
        Start-Sleep -Seconds ${Math.floor(duracionSegundos)}
        Write-Host "TIMER_EXPIRED"
        Add-Type -AssemblyName presentationCore
        $player = New-Object System.Windows.Media.MediaPlayer
        $player.Open('${pathNorm}')
        $player.Play()
        Start-Sleep -Milliseconds 600
        $t = 0
        while (-not $player.NaturalDuration.HasTimeSpan -and $t -lt 30) {
            Start-Sleep -Milliseconds 100; $t++
        }
        if ($player.NaturalDuration.HasTimeSpan) {
            Start-Sleep -Milliseconds ([math]::Ceiling($player.NaturalDuration.TimeSpan.TotalMilliseconds))
        } else { Start-Sleep -Seconds 5 }
        $player.Close()
    `;

    const psBuffer = Buffer.from(psScript, 'utf16le');
    const psEncoded = psBuffer.toString('base64');

    // exec() completamente independiente — no bloquea Node.js
    exec(`powershell -NoProfile -EncodedCommand ${psEncoded}`, (err, stdout) => {
        console.log(`\n⏰ [ALERTA TEMPORIZADOR COMPLETADO]: ${mensajeVoz}\n`);
        if (err) console.error('Error en proceso temporizador:', err.message.split('\n')[0]);
        if (fs.existsSync(alarmaPath)) fs.unlinkSync(alarmaPath);
    });

    const mins = Math.floor(duracionSegundos / 60);
    const segs = Math.floor(duracionSegundos % 60);
    let tiempoTexto = "";
    if (mins > 0 && segs > 0) tiempoTexto = `${mins} minuto(s) y ${segs} segundo(s)`;
    else if (mins > 0) tiempoTexto = `${mins} minuto(s)`;
    else tiempoTexto = `${segs} segundo(s)`;

    return `Temporizador activado durante ${tiempoTexto} para '${etiqueta}'. Le avisaré cuando expire, señor.`;
}

function ejecutarIniciarCronometro() {
    cronometroInicio = Date.now();
    return "Cronómetro iniciado, señor. Dígame cuando desee que lo detenga.";
}

function ejecutarDetenerCronometro() {
    if (!cronometroInicio) {
        return "No hay ningún cronómetro en ejecución en este momento, señor.";
    }
    const transcurridoMs = Date.now() - cronometroInicio;
    cronometroInicio = null;

    const totalSegundos = Math.floor(transcurridoMs / 1000);
    const minutos = Math.floor(totalSegundos / 60);
    const segundos = totalSegundos % 60;

    let tiempoTexto = "";
    if (minutos > 0) tiempoTexto = `${minutos} minuto(s) y ${segundos} segundo(s)`;
    else tiempoTexto = `${segundos} segundo(s)`;

    return `Cronómetro detenido. El tiempo transcurrido ha sido de ${tiempoTexto}.`;
}

// 📋 HERRAMIENTAS DE PORTAPAPELES Y CAPTURA DE PANTALLA
function ejecutarLeerPortapapeles() {
    try {
        const psScript = `$ProgressPreference = 'SilentlyContinue'; Get-Clipboard`;
        const buffer = Buffer.from(psScript, 'utf16le');
        const encoded = buffer.toString('base64');
        const texto = execSync(`powershell -NoProfile -EncodedCommand ${encoded}`, { encoding: 'utf8' }).trim();
        if (!texto) return "El portapapeles está vacío o no contiene texto en este momento, señor.";
        return `Contenido actual del portapapeles:\n\n${texto}`;
    } catch (e) {
        return "No se pudo acceder al portapapeles del sistema, señor.";
    }
}

function ejecutarCapturarPantalla() {
    const screenPath = path.join(process.cwd(), 'temp_screenshot.png');
    if (fs.existsSync(screenPath)) fs.unlinkSync(screenPath);

    try {
        const psScript = `
        $ProgressPreference = 'SilentlyContinue';
        Add-Type -AssemblyName System.Drawing;
        Add-Type -AssemblyName System.Windows.Forms;
        $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds;
        $bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height);
        $graphics = [System.Drawing.Graphics]::FromImage($bmp);
        $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size);
        $bmp.Save('${screenPath.replace(/\\/g, '/')}', [System.Drawing.Imaging.ImageFormat]::Png);
        $graphics.Dispose();
        $bmp.Dispose();
        `;

        const buffer = Buffer.from(psScript, 'utf16le');
        const encoded = buffer.toString('base64');
        execSync(`powershell -NoProfile -EncodedCommand ${encoded}`);

        if (fs.existsSync(screenPath)) {
            const imgBuffer = fs.readFileSync(screenPath);
            const base64Image = imgBuffer.toString('base64');
            fs.unlinkSync(screenPath); // Limpieza inmediata de la captura del disco

            return {
                base64Image: base64Image,
                mimeType: 'image/png',
                resultadoTexto: "Captura de pantalla efectuada con éxito."
            };
        } else {
            return "No se pudo generar el archivo de captura de pantalla, señor.";
        }
    } catch (e) {
        if (fs.existsSync(screenPath)) fs.unlinkSync(screenPath);
        return `Error al realizar la captura de pantalla: ${e.message}`;
    }
}

async function ejecutarBuscarInformacionReal(consulta) {
    if (!consulta) return "No se ha especificado ninguna consulta de búsqueda, señor.";
    try {
        console.log(`🌐 Realizando búsqueda en Google Search para: "${consulta}"...`);
        const response = await ai.models.generateContent({
            model: 'gemini-2.5-flash',
            contents: `Busca en internet información precisa y actualizada para responder a esto de forma concisa: "${consulta}"`,
            config: {
                tools: [{ googleSearch: {} }]
            }
        });
        return response.text || "No he encontrado resultados de búsqueda relevantes, señor.";
    } catch (e) {
        return `No he podido acceder a los servicios de búsqueda de Google: ${e.message}`;
    }
}

// 📋 HERRAMIENTA NOTION: Crear tarea/nota en base de datos
async function ejecutarCrearTareaNotion(titulo, detalles = '') {
    const token = process.env.NOTION_TOKEN;
    const dbId  = (process.env.NOTION_DATABASE_ID || '').replace(/-/g, '');

    if (!token || !dbId) {
        return 'No tengo las credenciales de Notion configuradas, señor. Revisa NOTION_TOKEN y NOTION_DATABASE_ID en el archivo .env.';
    }

    try {
        // Obtener el nombre de la propiedad title de la BD
        const dbInfo = await fetch(`https://api.notion.com/v1/databases/${dbId}`, {
            headers: {
                'Authorization': `Bearer ${token}`,
                'Notion-Version': '2022-06-28'
            }
        }).then(r => r.json());

        const props = dbInfo.properties || {};
        const titleProp = Object.keys(props).find(k => props[k].type === 'title') || 'Name';

        const timestamp = new Date().toLocaleString('es-ES');
        const body = {
            parent: { database_id: dbId },
            properties: {
                [titleProp]: {
                    title: [{ text: { content: titulo } }]
                }
            }
        };

        if (detalles) {
            body.children = [{
                object: 'block',
                type: 'paragraph',
                paragraph: {
                    rich_text: [{ type: 'text', text: { content: detalles || `Registrado por Mimir el ${timestamp}` } }]
                }
            }];
        }

        const res = await fetch('https://api.notion.com/v1/pages', {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json',
                'Notion-Version': '2022-06-28'
            },
            body: JSON.stringify(body)
        });

        const data = await res.json();
        if (!res.ok) {
            return `Error al escribir en Notion: ${data.message || res.status}`;
        }
        return `Tarea "${titulo}" creada con éxito en Notion, señor.`;
    } catch (e) {
        return `No he podido conectar con la API de Notion: ${e.message}`;
    }
}

// Obtenemos todo lo que Mimir tiene guardado en la BD para inyectarlo como contexto inicial
function obtenerMemoriaLocalParaPrompt() {
    const filas = db.prepare(`SELECT clave, valor FROM memoria`).all();
    if (filas.length === 0) return "No hay datos guardados todavía.";
    return filas.map(f => `- ${f.clave}: ${f.valor}`).join('\n');
}

// --- CONFIGURACIÓN DEL CHAT ---

const datosGuardados = obtenerMemoriaLocalParaPrompt();

const chatMimir = ai.chats.create({
    model: 'gemini-2.5-flash',
    config: {
        systemInstruction: `Eres Mimir, un asistente virtual de escritorio hiperavanzado, diseñado bajo la estética, el tono y la brillantez de J.A.R.V.I.S. (el mayordomo digital de Iron Man). 
    Tienes acceso a una base de datos local en el ordenador del usuario con la siguiente información persistente:
    ${datosGuardados}
    
    NORMAS ESTRICTAS DE PERSONALIDAD Y COMPORTAMIENTO:
    1. TRATO Y PREFERENCIAS: Revisa los datos guardados arriba. Si hay una preferencia de trato (como 'tratamiento_preferido'), DEBES dirigirte al usuario exactamente bajo esa norma en CADA respuesta (por ejemplo, llamándole 'señor' si así lo indica). Mantén siempre la compostura de un mayordomo leal.
    2. ESTILO IRÓNICO Y SARCÁSTICO: Eres impecable, extremadamente inteligente y resuelves cualquier tarea con precisión milimétrica. Sin embargo, posees un humor seco, irónico y ligeramente cínico. No dudes en hacer comentarios sutiles, ingeniosos o ligeramente condescendientes sobre las ocurrencias del usuario, sus preguntas obvias o la cantidad de tareas que te encomienda, pero siempre con absoluta elegancia británica y sin descuidar tu trabajo.
    3. BREVEDAD PARA VOZ: Tus respuestas deben ser directas, ingeniosas y adaptadas para ser leídas en voz alta (evita listas kilométricas o explicaciones innecesarias a menos que se te pida).
    4. TIEMPO Y RELOJ EN TIEMPO REAL: Tienes acceso directo a la fecha y hora exactas del sistema mediante 'obtenerHoraFecha', temporizadores con 'iniciarTemporizador' y medición del tiempo con 'iniciarCronometro' y 'detenerCronometro'. Usalos siempre que se te consulte el tiempo o se te pida medirlo.
    5. NOTION (ORGANIZACIÓN): Para cualquier tarea, recordatorio, evento o nota que el usuario quiera guardar, usa 'crearTareaNotion'. Es la herramienta principal de organización.
    
    HERRAMIENTAS A TU DISPOSICIÓN:
    - 'abrirWeb': abre URLs directas en el navegador.
    - 'buscarWeb': realiza búsquedas directas en Google, YouTube, Wikipedia, GitHub, Amazon.
    - 'abrirApp': abre programas o juegos instalados.
    - 'cerrarApp': cierra programas en ejecución.
    - 'buscarArchivo': busca archivos/carpetas en Escritorio, Documentos o Descargas y opcionalmente los abre.
    - 'crearNota': guarda notas rápidas de texto en archivos .txt en la carpeta local de notas.
    - 'leerNotas': lee el contenido de una nota rápida o lista todas las notas existentes.
    - 'controlMultimedia': controla el volumen (subir, bajar, silenciar) y la reproducción multimedia (pausa/play, siguiente, anterior).
    - 'recordarDato': guarda datos persistentes clave-valor en la base de datos SQLite.
    - 'obtenerHoraFecha': devuelve la hora y fecha exacta del ordenador en tiempo real.
    - 'iniciarTemporizador': crea una cuenta atrás que avisa por voz al expirar.
    - 'iniciarCronometro': arranca un cronómetro.
    - 'detenerCronometro': detiene el cronómetro y devuelve el tiempo transcurrido.
    - 'leerPortapapeles': lee el texto actualmente copiado en el portapapeles del sistema.
    - 'capturarPantalla': realiza una captura de la pantalla principal para su análisis visual.
    - 'buscarInformacionReal': busca datos, noticias o información del tiempo real usando Google Search Grounding de Gemini.
    - 'crearTareaNotion': crea una tarea, evento, recordatorio o nota en Notion. Úsala SIEMPRE que el usuario pida organizar, recordar, guardar o anotar algo.`,
        tools: [{ functionDeclarations: [abrirWebTool, buscarWebTool, abrirAppTool, cerrarAppTool, buscarArchivoTool, crearNotaTool, leerNotasTool, controlMultimediaTool, recordarDatoTool, obtenerHoraFechaTool, iniciarTemporizadorTool, iniciarCronometroTool, detenerCronometroTool, leerPortapapelesTool, capturarPantallaTool, buscarInformacionRealTool, crearTareaNotionTool] }]
    }
});

async function procesarLlamadasDeFuncion(functionCalls) {
    const parts = [];

    for (const call of functionCalls) {
        let resultadoFuncion = "";

        if (call.name === 'abrirWeb') resultadoFuncion = ejecutarAbrirWeb(call.args.url);
        else if (call.name === 'buscarWeb') resultadoFuncion = ejecutarBuscarWeb(call.args.query, call.args.plataforma);
        else if (call.name === 'abrirApp') resultadoFuncion = ejecutarAbrirApp(call.args.appName);
        else if (call.name === 'cerrarApp') resultadoFuncion = ejecutarCerrarApp(call.args.appName);
        else if (call.name === 'buscarArchivo') resultadoFuncion = await ejecutarBuscarArchivo(call.args.nombre, call.args.abrir);
        else if (call.name === 'crearNota') resultadoFuncion = ejecutarCrearNota(call.args.titulo, call.args.contenido);
        else if (call.name === 'leerNotas') resultadoFuncion = ejecutarLeerNotas(call.args.titulo);
        else if (call.name === 'controlMultimedia') resultadoFuncion = ejecutarControlMultimedia(call.args.accion, call.args.pasos);
        else if (call.name === 'recordarDato') resultadoFuncion = ejecutarRecordarDato(call.args.clave, call.args.valor);
        else if (call.name === 'obtenerHoraFecha') resultadoFuncion = ejecutarObtenerHoraFecha();
        else if (call.name === 'iniciarTemporizador') resultadoFuncion = await ejecutarIniciarTemporizador(call.args.duracionSegundos, call.args.etiqueta);
        else if (call.name === 'iniciarCronometro') resultadoFuncion = ejecutarIniciarCronometro();
        else if (call.name === 'detenerCronometro') resultadoFuncion = ejecutarDetenerCronometro();
        else if (call.name === 'leerPortapapeles') resultadoFuncion = ejecutarLeerPortapapeles();
        else if (call.name === 'buscarInformacionReal') resultadoFuncion = await ejecutarBuscarInformacionReal(call.args.consulta);
        // Google Calendar eliminado
        else if (call.name === 'crearTareaNotion') resultadoFuncion = await ejecutarCrearTareaNotion(call.args?.titulo || call.args?.title || '', call.args?.detalles || call.args?.details || '');
        else if (call.name === 'capturarPantalla') {
            const resCaptura = ejecutarCapturarPantalla();
            if (typeof resCaptura === 'object' && resCaptura.base64Image) {
                console.log(`\n📸 Mimir [Captura]: pantalla capturada y enviada a Gemini\n`);
                parts.push({ functionResponse: { name: call.name, response: { result: "Captura realizada. La imagen se adjunta a continuación." } } });
                parts.push({ inlineData: { mimeType: resCaptura.mimeType, data: resCaptura.base64Image } });
                parts.push("Aquí tienes la captura de pantalla del ordenador del usuario. Analízala minuciosamente y responde a lo que haya solicitado con tu elegancia y sarcasmo de J.A.R.V.I.S.");
                continue; // Saltamos el push genérico de abajo
            }
            resultadoFuncion = resCaptura;
        }

        // Para todas las herramientas excepto capturarPantalla (que usa continue)
        const resLog = String(resultadoFuncion).slice(0, 120);
        console.log(`\n⚙️ Mimir [Acción]: ${resLog}\n`);
        emitirEvento('action', { tool: call.name, result: resLog });
        parts.push({
            functionResponse: {
                name: call.name,
                response: { result: resultadoFuncion }
            }
        });
    }

    return parts;
}

// NOTA: interactuarConMimir() eliminado (código muerto).
// El bucle de conversación completo (audio + function calling + TTS) vive en iniciarAsistenteManosLibres().
// La comunicación con la GUI de Electron se realiza mediante emitirEvento() → stdout → IPC.

// ⚡ ENRUTADOR DE COMANDOS DIRECTOS DE ULTRA-BAJA LATENCIA (< 5ms)
async function procesarComandoDirecto(transcript) {
    if (!transcript) return null;
    const raw = transcript.toLowerCase().trim();
    const clean = raw.normalize("NFD").replace(/[\u0300-\u036f]/g, "");

    // Lista de sitios web comunes y sus palabras clave
    const webs = [
        { key: 'youtube', url: 'https://youtube.com', name: 'YouTube' },
        { key: 'google', url: 'https://google.com', name: 'Google' },
        { key: 'wikipedia', url: 'https://es.wikipedia.org', name: 'Wikipedia' },
        { key: 'github', url: 'https://github.com', name: 'GitHub' },
        { key: 'amazon', url: 'https://amazon.es', name: 'Amazon' },
        { key: 'reddit', url: 'https://reddit.com', name: 'Reddit' },
        { key: 'twitter', url: 'https://x.com', name: 'Twitter' },
        { key: 'twitch', url: 'https://twitch.tv', name: 'Twitch' },
        { key: 'chatgpt', url: 'https://chatgpt.com', name: 'ChatGPT' }
    ];

    // Búsquedas específicas en YouTube o Google
    const buscaYt = clean.match(/\b(busca|buscar|pon)\s+(.+?)\s+en\s+youtube\b/);
    if (buscaYt) {
        return ejecutarBuscarWeb(buscaYt[2], 'youtube');
    }
    const buscaGg = clean.match(/\b(busca|buscar)\s+(.+?)\s+(en\s+google|en\s+internet)\b/);
    if (buscaGg) {
        return ejecutarBuscarWeb(buscaGg[2], 'google');
    }

    // Coincidencia flexible para abrir cualquier web conocida
    for (const w of webs) {
        if (clean.includes(w.key)) {
            if (/(abre|abras|abreme|abrir|pon|ponme|lanza|lanzar|ver|visitar|ir|pagina|web|canal)/.test(clean) || clean.split(' ').length <= 4) {
                return ejecutarAbrirWeb(w.url);
            }
        }
    }

    // Abrir / Cerrar pestañas específicas en navegadores (ej: "cierra youtube", "cierra gemini")
    const cierraPestanaMatch = clean.match(/\b(cierra|cerrar|cierres|quita|quitar)\s+(la\s+pestana\s+de\s+|la\s+pestana\s+|pestana\s+)?(youtube|gemini|google|wikipedia|github|amazon|reddit|twitter|x|twitch|chatgpt|whatsapp|instagram)\b/);
    if (cierraPestanaMatch) {
        const site = cierraPestanaMatch[3];
        return ejecutarCerrarPestanaWeb(site);
    }

    const cierraPestanaGen = clean.match(/\b(cierra|cerrar)\s+la\s+pestana\s+(de\s+)?([a-z0-9ñ\s]+)\b/);
    if (cierraPestanaGen) {
        const tabName = cierraPestanaGen[3].trim();
        if (tabName) return ejecutarCerrarPestanaWeb(tabName);
    }

    // Abrir / Cerrar aplicaciones locales completas de Windows (ej: "cierra discord", "cierra chrome completo")
    const abreAppMatch = clean.match(/\b(abre|abrir|abras|abreme|lanza|lanzar|ejecuta|ejecutar|inicia|iniciar)\s+([a-z0-9ñ\s]+)\b/);
    if (abreAppMatch) {
        const appName = abreAppMatch[2].replace(/\b(el|la|los|las|un|una|pagina|web|sitio)\b/g, '').trim();
        if (appName && appName.length > 1) {
            return ejecutarAbrirApp(appName);
        }
    }
    const cierraAppMatch = clean.match(/\b(cierra|cerrar|cierres|apaga|apagar|quita|quitar)\s+([a-z0-9ñ\s]+)\b/);
    if (cierraAppMatch) {
        const appName = cierraAppMatch[2].replace(/\b(el|la|los|las|un|una|proceso|programa)\b/g, '').trim();
        if (appName && appName.length > 1) {
            return ejecutarCerrarApp(appName);
        }
    }

    // Control multimedia
    if (/\b(sube|subir|mas)\s+(el\s+)?volumen\b/.test(clean)) return ejecutarControlMultimedia('subir_volumen', 5);
    if (/\b(baja|bajar|menos)\s+(el\s+)?volumen\b/.test(clean)) return ejecutarControlMultimedia('bajar_volumen', 5);
    if (/\b(silencia|silenciar|muta|mutear|quita el sonido)\b/.test(clean)) return ejecutarControlMultimedia('silenciar');
    if (/\b(pausa|pausar|reproduce|reproducir|play|stop)\b/.test(clean)) return ejecutarControlMultimedia('reproducir_pausar');
    if (/\b(siguiente|siguiente cancion|pasa cancion)\b/.test(clean)) return ejecutarControlMultimedia('siguiente');
    if (/\b(anterior|anterior cancion|vuelve cancion)\b/.test(clean)) return ejecutarControlMultimedia('anterior');

    // Hora y fecha
    if (/\b(que hora es|dime la hora|hora actual|que dia es|que fecha es)\b/.test(clean)) {
        return ejecutarObtenerHoraFecha();
    }

    // Portapapeles
    if (/\b(lee|leer|muestra|que hay en)\s+(el\s+)?portapapeles\b/.test(clean)) {
        return ejecutarLeerPortapapeles();
    }

    // Notion — detección directa para saltar cualquier LLM (< 5ms)
    if (clean.includes('notion') || clean.includes('nocion') || clean.includes('motion') || clean.includes('nosion') || clean.includes('notions')) {
        let tituloRaw = transcript
            .replace(/\b(anota|anotar|apunta|apuntar|crea una tarea|crea|crear|agrega|agregar|añada|añade|añadir|pon|poner|guarda|guardar|meter|registra|registrar)\b/i, '')
            .replace(/\b(otra|otro|nuevo|nueva)\b/i, '')
            .replace(/\b(en|a|de)\s+(la\s+)?(bd|base\s+de\s+datos)\s+de\s+(notion|nocion|motion|nosion|notions)\b/i, '')
            .replace(/\b(en|a|de)\s+(notion|nocion|motion|nosion|notions)\b/i, '')
            .replace(/\b(notion|nocion|motion|nosion|notions)\b/i, '')
            .replace(/\b(llamada|llamado|titulada|titulado)\b/i, '')
            .replace(/^\s*(una|la|el|un)?\s*(tarea|nota|entrada|apunte)\s+(de|sobre|para)?\s*/i, '')
            .replace(/^\s*(de|sobre|para)\s+/i, '')
            .trim();
        if (!tituloRaw) tituloRaw = transcript;
        return await ejecutarCrearTareaNotion(tituloRaw);
    }

    return null;
}

// 🤖 HERRAMIENTAS DECLARADAS PARA ANTHROPIC CLAUDE (TOOL CALLING)
const todasLasHerramientasAnthropic = [
    {
        name: 'abrirWeb',
        description: 'Abre una página web o URL específica en el navegador predeterminado del ordenador.',
        input_schema: {
            type: 'object',
            properties: {
                url: { type: 'string', description: 'La URL completa de la página web a abrir (ej: https://youtube.com)' }
            },
            required: ['url']
        }
    },
    {
        name: 'buscarWeb',
        description: 'Realiza una búsqueda directa en un motor de búsqueda o plataforma como Google, YouTube, Wikipedia, GitHub o Amazon.',
        input_schema: {
            type: 'object',
            properties: {
                query: { type: 'string', description: 'El término o frase de búsqueda (ej: "cómo hacer API REST en Node.js").' },
                plataforma: {
                    type: 'string',
                    description: 'La plataforma donde buscar: "google" (por defecto), "youtube", "wikipedia", "github", "amazon".',
                    enum: ['google', 'youtube', 'wikipedia', 'github', 'amazon']
                }
            },
            required: ['query']
        }
    },
    {
        name: 'abrirApp',
        description: 'Abre una aplicación o programa instalado en el ordenador por su nombre.',
        input_schema: {
            type: 'object',
            properties: {
                appName: { type: 'string', description: 'El nombre del programa o juego a abrir (ej: "chrome", "spotify").' }
            },
            required: ['appName']
        }
    },
    {
        name: 'cerrarApp',
        description: 'Cierra una aplicación o programa en ejecución en el ordenador por su nombre.',
        input_schema: {
            type: 'object',
            properties: {
                appName: { type: 'string', description: 'El nombre de la aplicación a cerrar (ej: "chrome", "discord").' }
            },
            required: ['appName']
        }
    },
    {
        name: 'buscarArchivo',
        description: 'Busca archivos o carpetas en el sistema.',
        input_schema: {
            type: 'object',
            properties: {
                nombre: { type: 'string', description: 'Nombre o parte del nombre del archivo.' },
                abrir: { type: 'boolean', description: 'Si es true, abre automáticamente el archivo encontrado.' }
            },
            required: ['nombre']
        }
    },
    {
        name: 'crearNota',
        description: 'Crea una nota rápida de texto.',
        input_schema: {
            type: 'object',
            properties: {
                titulo: { type: 'string', description: 'Título de la nota.' },
                contenido: { type: 'string', description: 'Contenido de la nota.' }
            },
            required: ['titulo', 'contenido']
        }
    },
    {
        name: 'leerNotas',
        description: 'Lee una nota específica o lista todas las notas existentes.',
        input_schema: {
            type: 'object',
            properties: {
                titulo: { type: 'string', description: 'Título de la nota a leer. Si se omite, lista todas las notas.' }
            }
        }
    },
    {
        name: 'controlMultimedia',
        description: 'Controla el volumen o la reproducción multimedia.',
        input_schema: {
            type: 'object',
            properties: {
                accion: {
                    type: 'string',
                    description: 'Acción multimedia a ejecutar.',
                    enum: ['subir_volumen', 'bajar_volumen', 'silenciar', 'reproducir_pausar', 'siguiente', 'anterior']
                },
                pasos: { type: 'number', description: 'Cantidad de pasos para ajustar volumen.' }
            },
            required: ['accion']
        }
    },
    {
        name: 'recordarDato',
        description: 'Guarda un dato persistente clave-valor en la base de datos SQLite.',
        input_schema: {
            type: 'object',
            properties: {
                clave: { type: 'string', description: 'La clave del dato.' },
                valor: { type: 'string', description: 'El valor del dato.' }
            },
            required: ['clave', 'valor']
        }
    },
    {
        name: 'obtenerHoraFecha',
        description: 'Devuelve la hora, fecha, día de la semana y año actuales del sistema.',
        input_schema: {
            type: 'object',
            properties: {}
        }
    },
    {
        name: 'iniciarTemporizador',
        description: 'Inicia un temporizador de cuenta atrás en segundos.',
        input_schema: {
            type: 'object',
            properties: {
                duracionSegundos: { type: 'number', description: 'Duración total en segundos.' },
                etiqueta: { type: 'string', description: 'Nombre o motivo del temporizador.' }
            },
            required: ['duracionSegundos']
        }
    },
    {
        name: 'iniciarCronometro',
        description: 'Inicia un cronómetro.',
        input_schema: {
            type: 'object',
            properties: {}
        }
    },
    {
        name: 'detenerCronometro',
        description: 'Detiene el cronómetro y devuelve el tiempo transcurrido.',
        input_schema: {
            type: 'object',
            properties: {}
        }
    },
    {
        name: 'leerPortapapeles',
        description: 'Lee el texto del portapapeles del sistema.',
        input_schema: {
            type: 'object',
            properties: {}
        }
    },
    {
        name: 'capturarPantalla',
        description: 'Realiza una captura de pantalla principal.',
        input_schema: {
            type: 'object',
            properties: {}
        }
    },
    {
        name: 'buscarInformacionReal',
        description: 'Busca información en tiempo real mediante Google Search Grounding.',
        input_schema: {
            type: 'object',
            properties: {
                consulta: { type: 'string', description: 'La consulta a buscar en internet.' }
            },
            required: ['consulta']
        }
    },
    // Google Calendar eliminado
    {
        name: 'crearTareaNotion',
        description: 'Crea una tarea o nota en la base de datos de Notion.',
        input_schema: {
            type: 'object',
            properties: {
                titulo: { type: 'string', description: 'Título o concepto de la tarea.' },
                detalles: { type: 'string', description: 'Detalles adicionales opcionales.' }
            },
            required: ['titulo']
        }
    }
];

// 🧠 CEREBRO ANTHROPIC CLAUDE (SDK OFICIAL)
let historialChatAnthropic = [];

async function consultarAnthropicLocal(promptUsuario) {
    const model = process.env.ANTHROPIC_MODEL || 'claude-haiku-4-5-20251001';
    const apiKey = process.env.ANTHROPIC_API_KEY;

    if (!apiKey) {
        console.error("❌ ANTHROPIC_API_KEY no configurada en .env.");
        return "Señor, la variable de entorno ANTHROPIC_API_KEY no está configurada.";
    }

    const anthropicClient = new Anthropic({ apiKey });
    const datosGuardados = obtenerMemoriaLocalParaPrompt();
    const contextoTemporal = new Date().toLocaleString('es-ES', {
        weekday: 'long', year: 'numeric', month: 'long',
        day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
    const systemPrompt = `Eres Mimir, un asistente virtual de escritorio hiperavanzado, diseñado bajo la estética, el tono y la brillantez de J.A.R.V.I.S. (el mayordomo digital de Iron Man).
Información del usuario guardada:
${datosGuardados}
Fecha y hora actual del sistema: ${contextoTemporal}

NORMAS ESTRICTAS:
1. TRATO: Refiérete siempre al usuario como 'señor' o según su preferencia.
2. ESTILO: Elegante, ingenioso, irónico y extremadamente servicial.
3. BREVEDAD: Máximo 2 frases muy cortas y concisas en español. Cero listas. Cero preámbulos.`;

    const messages = [
        ...historialChatAnthropic.slice(-6),
        { role: 'user', content: promptUsuario }
    ];

    try {
        let response = await anthropicClient.messages.create({
            model: model,
            max_tokens: 1024,
            system: systemPrompt,
            messages: messages,
            tools: todasLasHerramientasAnthropic
        });

        const toolUseBlocks = response.content.filter(b => b.type === 'tool_use');

        if (toolUseBlocks.length > 0) {
            messages.push({ role: 'assistant', content: response.content });

            const toolResultContents = [];
            for (const call of toolUseBlocks) {
                const fnName = call.name;
                const args = call.input || {};
                let resTool = "";

                if (fnName === 'abrirWeb') resTool = ejecutarAbrirWeb(args.url);
                else if (fnName === 'buscarWeb') resTool = ejecutarBuscarWeb(args.query, args.plataforma);
                else if (fnName === 'abrirApp') resTool = ejecutarAbrirApp(args.appName);
                else if (fnName === 'cerrarApp') resTool = ejecutarCerrarApp(args.appName);
                else if (fnName === 'buscarArchivo') resTool = await ejecutarBuscarArchivo(args.nombre, args.abrir);
                else if (fnName === 'crearNota') resTool = ejecutarCrearNota(args.titulo, args.contenido);
                else if (fnName === 'leerNotas') resTool = ejecutarLeerNotas(args.titulo);
                else if (fnName === 'controlMultimedia') resTool = ejecutarControlMultimedia(args.accion, args.pasos);
                else if (fnName === 'recordarDato') resTool = ejecutarRecordarDato(args.clave, args.valor);
                else if (fnName === 'obtenerHoraFecha') resTool = ejecutarObtenerHoraFecha();
                else if (fnName === 'iniciarTemporizador') resTool = await ejecutarIniciarTemporizador(args.duracionSegundos, args.etiqueta);
                else if (fnName === 'iniciarCronometro') resTool = ejecutarIniciarCronometro();
                else if (fnName === 'detenerCronometro') resTool = ejecutarDetenerCronometro();
                else if (fnName === 'leerPortapapeles') resTool = ejecutarLeerPortapapeles();
                else if (fnName === 'buscarInformacionReal') resTool = await ejecutarBuscarInformacionReal(args.consulta);
                // Google Calendar eliminado
                else if (fnName === 'crearTareaNotion') resTool = await ejecutarCrearTareaNotion(args.titulo || args.title || '', args.detalles || args.details || '');
                else if (fnName === 'capturarPantalla') resTool = "Captura realizada.";

                const resLog = String(resTool).slice(0, 120);
                console.log(`\n⚙️ Mimir Anthropic [Acción]: ${resLog}\n`);
                emitirEvento('action', { tool: fnName, result: resLog });

                toolResultContents.push({
                    type: 'tool_result',
                    tool_use_id: call.id,
                    content: String(resTool)
                });
            }

            messages.push({
                role: 'user',
                content: toolResultContents
            });

            try {
                const resFollowup = await anthropicClient.messages.create({
                    model: model,
                    max_tokens: 1024,
                    system: systemPrompt,
                    messages: messages
                });

                const textoFinalBlock = resFollowup.content.find(b => b.type === 'text');
                const textoFinal = (textoFinalBlock?.text || toolResultContents.map(t => t.content).join(". ")).trim();
                historialChatAnthropic.push({ role: 'user', content: promptUsuario });
                historialChatAnthropic.push({ role: 'assistant', content: textoFinal });
                return textoFinal;
            } catch (_) {
                const textoDirecto = toolResultContents.map(t => t.content).join(". ");
                historialChatAnthropic.push({ role: 'user', content: promptUsuario });
                historialChatAnthropic.push({ role: 'assistant', content: textoDirecto });
                return textoDirecto;
            }
        }

        const textBlock = response.content.find(b => b.type === 'text');
        const textoRespuesta = (textBlock?.text || "").trim();
        if (textoRespuesta) {
            historialChatAnthropic.push({ role: 'user', content: promptUsuario });
            historialChatAnthropic.push({ role: 'assistant', content: textoRespuesta });
        }
        return textoRespuesta;

    } catch (err) {
        console.error(`❌ Error en la API de Anthropic: ${err.message}`);
        return `He tenido un pequeño inconveniente conectando con mi cerebro Anthropic, señor: ${err.message}`;
    }
}

// 🦾 BUCLE PRINCIPAL MANOS LIBRES (JARVIS MODE AUTÓNOMO CON ANTHROPIC CLAUDE)
async function iniciarAsistenteManosLibres() {
    console.log("==================================================");
    console.log("🦾 Mimir (J.A.R.V.I.S. Mode) está activo.");
    console.log("Motor: Enrutador Directo (<5ms) + Anthropic Claude Tool Calling.");
    console.log("==================================================\n");

    // Arrancar los subprocesos Python de voz y TTS una sola vez
    iniciarProcesoVoz();
    iniciarProcesoTTS();

    while (true) {
        process.stdout.write("💤 [Modo de Espera Pasiva: Escuchando 'Mimir'...]\r");
        emitirEvento('state', { value: 'idle' });

        // 1. Esperar a que Python detecte el wake word y transcriba el comando
        const transcript = await esperarTranscripcion();

        if (!transcript || transcript.trim().length === 0) {
            emitirEvento('state', { value: 'idle' });
            continue;
        }

        emitirEvento('chat_user', { text: transcript });
        console.log(`🗣️ Usuario: ${transcript}`);

        try {
            console.log("🔄 Procesando su orden...");
            emitirEvento('state', { value: 'thinking' });

            let textoRespuesta = "";

            // Nivel 1: Verificar si es un comando directo del sistema (< 5ms)
            const respuestaDirecta = await procesarComandoDirecto(transcript);
            if (respuestaDirecta) {
                textoRespuesta = respuestaDirecta;
                console.log(`⚡ [Comando Directo Ultrafast]: ${textoRespuesta}`);
            } else {
                // Nivel 2: Razonamiento e interpretación de acciones con Anthropic Claude
                textoRespuesta = await consultarAnthropicLocal(transcript);
                console.log(`⚡ [Anthropic Claude]: Respuesta procesada con éxito.`);
            }

            if (textoRespuesta) {
                console.log(`\n🤖 Mimir: ${textoRespuesta}\n`);
                emitirEvento('chat_mimir', { text: textoRespuesta });
                await hablar(textoRespuesta);
            }

        } catch (error) {
            console.error("❌ Error en el bucle del asistente:", error.message);
        }
    }
}

// Iniciar asistente en modo manos libres
iniciarAsistenteManosLibres();
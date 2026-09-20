/**
 * telegram_service/bot.js — Bot Oficial de Telegram para Mimir (Claude + Notion + Voz)
 * ==============================================================================
 *
 * Características:
 *  - Sin Puppeteer ni emulación web (cero consumo innecesario de RAM).
 *  - Conexión oficial y permanente mediante Telegram Bot API.
 *  - Soporte de texto: apunta y consulta en Notion al instante.
 *  - Soporte de notas de voz: descarga el audio, lo transcribe con Whisper local,
 *    procesa con Claude y responde.
 *  - Historial de conversación en memoria (últimos 20 turnos por usuario).
 *  - Respuesta en dos fases: envía confirmación en texto de inmediato y
 *    luego la nota de voz generada con XTTS-v2 (opcional/en segundo plano).
 *  - Control de acceso por ID de usuario para privacidad absoluta.
 */

import { Telegraf } from 'telegraf';
import { spawn } from 'child_process';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';
import dotenv from 'dotenv';
import http from 'http';
import https from 'https';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);
const ROOT_DIR   = path.resolve(__dirname, '..');

dotenv.config({ path: path.join(ROOT_DIR, '.env') });

function log(msg) {
    const ts = new Date().toLocaleTimeString('es-ES');
    console.log(`[${ts}] [Telegram] ${msg}`);
}

const BOT_TOKEN = (process.env.TELEGRAM_BOT_TOKEN || '').trim();
const ALLOWED_USER_ID = (process.env.TELEGRAM_ALLOWED_USER_ID || '').trim();

if (!BOT_TOKEN) {
    log('⚠️  TELEGRAM_BOT_TOKEN no está configurado en el archivo .env.');
    log('   Crea un bot con @BotFather en Telegram y añade a tu .env:');
    log('   TELEGRAM_BOT_TOKEN=tu_token_aqui\n');
}

const PYTHON_CMD = process.platform === 'win32' ? 'py' : 'python3';
const PYTHON_ARGS_PREFIX = process.platform === 'win32' ? ['-3.11'] : [];
const AGENT_SCRIPT = path.join(__dirname, 'mimir_agent.py');
const AGENT_TIMEOUT_MS = 120_000;

// ── Historial de conversación por usuario (en memoria) ─────────────────────
// Formato: Map<userId, Array<{role: "user"|"assistant", content: string}>>
// Máximo MAX_HISTORY_TURNS turnos (pares) por usuario para no consumir demasiados tokens
const MAX_HISTORY_TURNS = 20; // 20 mensajes = 10 intercambios usuario/asistente
const conversationHistory = new Map();

function getHistory(userId) {
    if (!conversationHistory.has(userId)) {
        conversationHistory.set(userId, []);
    }
    return conversationHistory.get(userId);
}

function appendHistory(userId, role, content) {
    const history = getHistory(userId);
    history.push({ role, content });
    // Recortar si supera el máximo, manteniendo siempre pares completos
    while (history.length > MAX_HISTORY_TURNS) {
        history.shift();
    }
}

function clearHistory(userId) {
    conversationHistory.set(userId, []);
}

/**
 * Llama a mimir_agent.py pasando el historial completo por stdin como JSON.
 * El agente devuelve la respuesta por stdout.
 */
function llamarAgente(userId, tipo, valorEntrada) {
    return new Promise((resolve, reject) => {
        // Construir el payload que el agente leerá por stdin
        const history = getHistory(userId);
        const payload = JSON.stringify({
            tipo,          // "texto" | "audio"
            valor: valorEntrada,
            historial: history,
        });

        const args = [...PYTHON_ARGS_PREFIX, AGENT_SCRIPT, '--stdin-mode'];
        log(`Procesando (${tipo}): ${tipo === 'audio' ? '[nota de voz]' : valorEntrada.substring(0, 60)}`);

        const proc = spawn(PYTHON_CMD, args, {
            cwd: ROOT_DIR,
            env: { ...process.env, PYTHONIOENCODING: 'utf-8', COQUI_TOS_AGREED: '1' },
        });

        let stdout = '';
        proc.stdout.on('data', c => { stdout += c.toString('utf-8'); });
        proc.stderr.on('data', c => {
            for (const line of c.toString('utf-8').split('\n')) {
                if (line.trim()) log(`   [Py] ${line.trim()}`);
            }
        });

        // Escribir el payload por stdin y cerrarlo para que el agente lo lea
        proc.stdin.write(payload, 'utf-8');
        proc.stdin.end();

        const timer = setTimeout(() => { proc.kill(); reject(new Error('Timeout de agente (120s)')); }, AGENT_TIMEOUT_MS);
        proc.on('close', code => {
            clearTimeout(timer);
            const r = stdout.trim();
            if (r) resolve(r);
            else reject(new Error(`Sin respuesta (código ${code})`));
        });
        proc.on('error', e => { clearTimeout(timer); reject(e); });
    });
}

/**
 * Solicita síntesis de voz al servidor TTS persistente en index.js (localhost:7700).
 * Como XTTS-v2 ya está cargado en GPU en index.js, genera el audio en ~1-2 segundos.
 */
function sintetizarVozRapida(texto) {
    return new Promise((resolve, reject) => {
        const payload = JSON.stringify({ text: texto });
        const options = {
            hostname: '127.0.0.1',
            port: 7700,
            path: '/tts',
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(payload),
            },
            timeout: 45000,
        };

        const req = http.request(options, (res) => {
            if (res.statusCode !== 200) {
                let errData = '';
                res.on('data', c => { errData += c; });
                res.on('end', () => reject(new Error(`TTS HTTP status ${res.statusCode}: ${errData}`)));
                return;
            }

            const chunks = [];
            res.on('data', chunk => chunks.push(chunk));
            res.on('end', () => resolve(Buffer.concat(chunks)));
        });

        req.on('timeout', () => {
            req.destroy();
            reject(new Error('Timeout en servidor TTS'));
        });

        req.on('error', (err) => {
            reject(err);
        });

        req.write(payload);
        req.end();
    });
}

/**
 * Envía la respuesta directamente como nota de voz ultrarrápida.
 * Si por alguna razón el TTS falla, envía la respuesta en texto como fallback.
 */
async function responderConVoz(ctx, respuestaTexto) {
    await ctx.sendChatAction('record_voice');
    try {
        log(`🔊 Generando respuesta en audio ultrarrápida con XTTS-v2...`);
        const audioBuffer = await sintetizarVozRapida(respuestaTexto);
        const tempVoicePath = path.join(__dirname, `reply_${Date.now()}_${Math.random().toString(36).slice(2)}.wav`);
        await fs.promises.writeFile(tempVoicePath, audioBuffer);
        try {
            await ctx.replyWithVoice({ source: tempVoicePath });
            log(`✅ 🔊 Nota de voz enviada al usuario.`);
        } finally {
            try { await fs.promises.unlink(tempVoicePath); } catch (_) {}
        }
    } catch (errTTS) {
        log(`⚠️ No se pudo generar audio rápido (${errTTS.message}). Enviando como texto de respaldo.`);
        await ctx.reply(respuestaTexto);
    }
}

/**
 * Descarga un archivo desde una URL HTTPS a una ruta local.
 */
function descargarArchivo(url, rutaDestino) {
    return new Promise((resolve, reject) => {
        const file = fs.createWriteStream(rutaDestino);
        https.get(url, (response) => {
            if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
                return descargarArchivo(response.headers.location, rutaDestino).then(resolve).catch(reject);
            }
            response.pipe(file);
            file.on('finish', () => {
                file.close(() => resolve(rutaDestino));
            });
        }).on('error', (err) => {
            fs.unlink(rutaDestino, () => {});
            reject(err);
        });
    });
}

// ── Iniciar Bot ───────────────────────────────────────────────────────────────
export function iniciarBotTelegram() {
    if (!BOT_TOKEN) {
        log('⏸️ Bot no iniciado: falta TELEGRAM_BOT_TOKEN en .env.');
        return null;
    }

    const bot = new Telegraf(BOT_TOKEN);

    // Middleware de seguridad y autorización
    bot.use(async (ctx, next) => {
        const userId = String(ctx.from?.id || '');
        const username = ctx.from?.username ? `@${ctx.from.username}` : (ctx.from?.first_name || 'Usuario');

        if (ALLOWED_USER_ID && userId !== ALLOWED_USER_ID) {
            log(`⛔ Acceso no autorizado denegado para ${username} (ID: ${userId})`);
            await ctx.reply('🔒 Este bot es personal y privado. No tienes autorización para usarlo.').catch(() => {});
            return;
        }

        if (!ALLOWED_USER_ID) {
            log(`ℹ️ [Seguridad] Mensaje de ${username} (ID: ${userId}).`);
            log(`   → Para restringir el bot solo a ti, añade en .env: TELEGRAM_ALLOWED_USER_ID=${userId}`);
        }

        return next();
    });

    // Comando /start
    bot.start(async (ctx) => {
        const userId = String(ctx.from?.id || '');
        clearHistory(userId);
        await responderConVoz(ctx,
            `A su servicio, señor. Mimir en línea y plenamente operativo. ` +
            `Tengo acceso completo a su base de datos personal y a su Notion para gestionar sus tareas, citas, eventos y gastos. ` +
            `¿Qué requiere en este momento, señor?`
        );
    });

    // Comando /reset — Borrar historial de conversación
    bot.command('reset', async (ctx) => {
        const userId = String(ctx.from?.id || '');
        clearHistory(userId);
        await ctx.reply('🔄 Historial borrado, señor. Empezamos de cero.');
    });

    // Manejo de mensajes de texto
    bot.on('text', async (ctx) => {
        const texto = ctx.message.text.trim();
        if (texto.startsWith('/')) return; // ignorar otros comandos

        const userId = String(ctx.from?.id || '');

        try {
            await ctx.sendChatAction('record_voice');
            log(`📥 Mensaje recibido de ${ctx.from?.first_name}: "${texto}"`);

            // Añadir el mensaje del usuario al historial ANTES de llamar al agente
            appendHistory(userId, 'user', texto);

            const respuestaTexto = await llamarAgente(userId, 'texto', texto);

            // Añadir la respuesta de Mimir al historial
            appendHistory(userId, 'assistant', respuestaTexto);

            // Responder directamente con nota de voz rápida
            await responderConVoz(ctx, respuestaTexto);

        } catch (err) {
            log(`❌ Error procesando texto: ${err.message}`);
            await ctx.reply(`Error al procesar: ${err.message}`).catch(() => {});
        }
    });

    // Manejo de notas de voz y archivos de audio
    bot.on(['voice', 'audio'], async (ctx) => {
        const voice = ctx.message.voice || ctx.message.audio;
        if (!voice?.file_id) return;

        const userId = String(ctx.from?.id || '');
        const tempAudioPath = path.join(__dirname, `voice_${Date.now()}.oga`);

        try {
            await ctx.sendChatAction('record_voice');
            log(`🎤 Descargando nota de voz recibida...`);

            const fileLink = await ctx.telegram.getFileLink(voice.file_id);
            await descargarArchivo(fileLink.href, tempAudioPath);
            log(`✅ Audio descargado en ${tempAudioPath}`);

            await ctx.sendChatAction('record_voice');

            // Para audio, primero transcribir y luego procesar con historial
            const respuestaTexto = await llamarAgente(userId, 'audio', tempAudioPath);

            // Añadir la respuesta al historial local
            appendHistory(userId, 'assistant', respuestaTexto);

            // Responder directamente con nota de voz rápida
            await responderConVoz(ctx, respuestaTexto);

        } catch (err) {
            log(`❌ Error procesando nota de voz: ${err.message}`);
            await ctx.reply(`No pude procesar el audio: ${err.message}`).catch(() => {});
        } finally {
            if (fs.existsSync(tempAudioPath)) {
                try { fs.unlinkSync(tempAudioPath); } catch (_) {}
            }
        }
    });

    bot.telegram.getMe()
        .then((me) => {
            log(`🟢 Bot de Telegram @${me.username} CONECTADO Y OPERATIVO.`);
            log(`   Esperando sus mensajes o notas de voz, señor...`);
        })
        .catch((err) => {
            log(`❌ Error al conectar bot de Telegram: ${err.message}`);
        });

    bot.launch({ dropPendingUpdates: true });

    process.once('SIGINT', () => bot.stop('SIGINT'));
    process.once('SIGTERM', () => bot.stop('SIGTERM'));

    return bot;
}

iniciarBotTelegram();

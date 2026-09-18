import fs from 'fs';
import path from 'path';
import http from 'http';
import { exec } from 'child_process';
import { google } from 'googleapis';

const CREDENTIALS_PATH = path.join(process.cwd(), 'credentials.json');
const TOKEN_PATH = path.join(process.cwd(), 'token.json');
const SCOPES = ['https://www.googleapis.com/auth/calendar'];

let oauth2Client = null;

// Inicializa y devuelve la API de Google Calendar configurada
async function obtenerClienteCalendar() {
    if (!fs.existsSync(CREDENTIALS_PATH)) {
        return null;
    }
    
    if (!oauth2Client) {
        try {
            const content = fs.readFileSync(CREDENTIALS_PATH, 'utf-8');
            const credentials = JSON.parse(content);
            const { client_secret, client_id } = credentials.installed || credentials.web;
            const port = 3000;
            const redirectUri = `http://localhost:${port}/oauth2callback`;
            oauth2Client = new google.auth.OAuth2(client_id, client_secret, redirectUri);
        } catch (e) {
            console.error("❌ [Calendario] Error al analizar credentials.json:", e.message);
            return null;
        }
    }

    if (fs.existsSync(TOKEN_PATH)) {
        try {
            const tokenContent = fs.readFileSync(TOKEN_PATH, 'utf-8');
            oauth2Client.setCredentials(JSON.parse(tokenContent));
            return google.calendar({ version: 'v3', auth: oauth2Client });
        } catch (e) {
            console.error("❌ [Calendario] Error al cargar token.json:", e.message);
        }
    }

    // Iniciar flujo OAuth de forma asíncrona si no está autenticado
    return new Promise((resolve) => {
        const authUrl = oauth2Client.generateAuthUrl({
            access_type: 'offline',
            scope: SCOPES,
        });

        console.log('\n🔑 [Calendario] Se requiere autenticación. Abriendo el navegador...');
        const port = 3000;
        
        const server = http.createServer(async (req, res) => {
            if (req.url.startsWith('/oauth2callback')) {
                const urlObj = new URL(req.url, `http://${req.headers.host}`);
                const code = urlObj.searchParams.get('code');
                if (code) {
                    try {
                        const { tokens } = await oauth2Client.getToken(code);
                        oauth2Client.setCredentials(tokens);
                        fs.writeFileSync(TOKEN_PATH, JSON.stringify(tokens, null, 2));
                        
                        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
                        res.end(`
                            <html>
                            <body style="font-family: sans-serif; text-align: center; padding-top: 50px; background-color: #0a0a0f; color: #dde4ef;">
                                <h1 style="color: #00d4ff;">¡Conexión Exitosa!</h1>
                                <p>Mimir se ha conectado correctamente a tu Google Calendar.</p>
                                <p>Puedes cerrar esta pestaña y volver a hablarle al asistente.</p>
                            </body>
                            </html>
                        `);
                        console.log('✅ [Calendario] Autenticación completada con éxito. Token guardado.');
                        server.close();
                        resolve(google.calendar({ version: 'v3', auth: oauth2Client }));
                    } catch (err) {
                        res.writeHead(500, { 'Content-Type': 'text/html' });
                        res.end(`Error: ${err.message}`);
                        server.close();
                        resolve(null);
                    }
                }
            }
        });

        server.listen(port, () => {
            console.log(`\n🔗 Si el navegador no se abre de forma automática, copia y abre esta URL en tu navegador para autorizar a Mimir:\n`);
            console.log(`${authUrl}\n`);
            exec(`start "" "${authUrl}"`, (err) => {
                // Silenciamos error si no puede abrir automáticamente ya que imprimimos arriba
            });
        });
    });
}

export async function crearEventoCalendario(titulo, fechaInicio, duracionMinutos = 60, descripcion = "") {
    const calendar = await obtenerClienteCalendar();
    if (!calendar) {
        return "El servicio de Google Calendar no está configurado o requiere credenciales. Por favor, asegúrate de colocar credentials.json en la raíz.";
    }

    try {
        const start = new Date(fechaInicio);
        const end = new Date(start.getTime() + duracionMinutos * 60 * 1000);

        const event = {
            summary: titulo,
            description: descripcion,
            start: {
                dateTime: start.toISOString(),
                timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
            },
            end: {
                dateTime: end.toISOString(),
                timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
            }
        };

        const res = await calendar.events.insert({
            calendarId: 'primary',
            resource: event,
        });

        return `Evento creado con éxito: "${res.data.summary}" agendado para el ${start.toLocaleString('es-ES')}.`;
    } catch (e) {
        console.error("Error al crear evento:", e.message);
        return `Error al agendar en el calendario: ${e.message}`;
    }
}

export async function buscarEventosCalendario(fechaInicio, fechaFin, consulta = "") {
    const calendar = await obtenerClienteCalendar();
    if (!calendar) {
        return "El servicio de Google Calendar no está configurado o requiere credenciales.";
    }

    try {
        const params = {
            calendarId: 'primary',
            timeMin: fechaInicio ? new Date(fechaInicio).toISOString() : new Date().toISOString(),
            singleEvents: true,
            orderBy: 'startTime',
            maxResults: 15
        };

        if (fechaFin) {
            params.timeMax = new Date(fechaFin).toISOString();
        }
        if (consulta) {
            params.q = consulta;
        }

        const res = await calendar.events.list(params);
        const events = res.data.items;
        if (!events || events.length === 0) {
            return "No se encontraron eventos agendados para este período.";
        }

        let lista = "Aquí están tus planes agendados:\n";
        events.forEach((event) => {
            const start = event.start.dateTime || event.start.date;
            const startFormatted = new Date(start).toLocaleString('es-ES', {
                weekday: 'short',
                day: 'numeric',
                month: 'short',
                hour: '2-digit',
                minute: '2-digit'
            });
            lista += `- ${startFormatted}: ${event.summary} ${event.description ? `(${event.description})` : ''}\n`;
        });

        return lista;
    } catch (e) {
        console.error("Error al listar eventos:", e.message);
        return `Error al consultar el calendario: ${e.message}`;
    }
}

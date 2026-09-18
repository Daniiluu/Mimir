import { app, BrowserWindow, ipcMain } from 'electron';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { spawn } from 'child_process';
import { createInterface } from 'readline';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = dirname(__filename);

let mimiProcess = null;
let mainWindow  = null;

function enviarEvento(event) {
    if (mainWindow && !mainWindow.isDestroyed()) {
        try {
            mainWindow.webContents.send('mimir-event', event);
        } catch (_) {}
    }
}

// ── Spawn mimir-core con flag --gui ────────────────────────────────────────
function iniciarMimirCore() {
    mimiProcess = spawn('node', ['index.js', '--gui'], {
        cwd: __dirname,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
        stdio: ['ignore', 'pipe', 'pipe']
    });

    // Leer stdout línea por línea y reenviar eventos estructurados a la UI
    mimiProcess.stdout.setEncoding('utf8');
    mimiProcess.stderr.setEncoding('utf8');
    const rl = createInterface({ input: mimiProcess.stdout });
    rl.on('line', (line) => {
        if (line.startsWith('MIMIR_EVENT:')) {
            try {
                const event = JSON.parse(line.slice('MIMIR_EVENT:'.length));
                enviarEvento(event);
            } catch (_) {}
        }
    });

    mimiProcess.stderr.on('data', (data) => {
        const msg = data.toString().trim();
        if (msg) enviarEvento({ type: 'log', text: msg });
    });

    mimiProcess.on('exit', (code) => {
        enviarEvento({ type: 'exit', code });
    });
}

// ── Ventana principal ─────────────────────────────────────────────────────
function createWindow() {
    mainWindow = new BrowserWindow({
        width: 900,
        height: 680,
        minWidth: 700,
        minHeight: 500,
        frame: false,
        transparent: false,
        backgroundColor: '#0a0a0f',
        webPreferences: {
            preload: join(__dirname, 'preload.cjs'),
            nodeIntegration: false,
            contextIsolation: true,
            sandbox: false
        },
        show: false
    });

    mainWindow.once('ready-to-show', () => {
        mainWindow.show();
        iniciarMimirCore();
    });

    mainWindow.loadFile(join(__dirname, 'ui', 'index.html'));
}

// ── Ciclo de vida de la app ───────────────────────────────────────────────
app.whenReady().then(() => {
    createWindow();
    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
});

app.on('window-all-closed', () => {
    if (mimiProcess) { mimiProcess.kill(); mimiProcess = null; }
    if (process.platform !== 'darwin') app.quit();
});

// ── Window Controls IPC ───────────────────────────────────────────────────
ipcMain.handle('win:minimize', () => mainWindow?.minimize());
ipcMain.handle('win:maximize', () => {
    mainWindow?.isMaximized() ? mainWindow.unmaximize() : mainWindow?.maximize();
});
ipcMain.handle('win:close', () => {
    if (mimiProcess) { mimiProcess.kill(); mimiProcess = null; }
    mainWindow?.close();
});

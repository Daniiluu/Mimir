const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
    window: {
        minimize: () => ipcRenderer.invoke('win:minimize'),
        maximize: () => ipcRenderer.invoke('win:maximize'),
        close:    () => ipcRenderer.invoke('win:close')
    },
    onMimirEvent: (callback) => {
        ipcRenderer.on('mimir-event', (_event, data) => callback(data));
    }
});

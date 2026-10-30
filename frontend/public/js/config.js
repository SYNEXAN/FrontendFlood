/**
 * Configuration Manager for Flood & Traffic Monitoring Frontend
 */
const CONFIG = {
    DEFAULT_BACKEND: "https://hush-harmless-erased.ngrok-free.dev",
    STORAGE_KEY: "FLOOD_DEMO_CUSTOM_BACKEND",

    getBackendUrl() {
        const custom = localStorage.getItem(this.STORAGE_KEY);
        return (custom && custom.trim()) ? custom.trim().replace(/\/+$/, "") : this.DEFAULT_BACKEND;
    },

    setBackendUrl(url) {
        if (!url || !url.trim()) {
            localStorage.removeItem(this.STORAGE_KEY);
        } else {
            localStorage.setItem(this.STORAGE_KEY, url.trim().replace(/\/+$/, ""));
        }
    },

    resetToDefault() {
        localStorage.removeItem(this.STORAGE_KEY);
    }
};

window.CONFIG = CONFIG;

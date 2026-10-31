const express = require('express');
const path = require('path');
const cors = require('cors');

const app = express();
const PORT = process.env.PORT || 10000;
const DEFAULT_BACKEND_URL = process.env.DEFAULT_BACKEND_URL || "https://venture-curves-between-manufacture.trycloudflare.com";
let activeBackendUrl = DEFAULT_BACKEND_URL;

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Dynamic backend update endpoint (called automatically by Kaggle GPU notebook on boot)
app.post('/api/backend-url', (req, res) => {
    const { url } = req.body;
    if (url && typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))) {
        activeBackendUrl = url.trim().replace(/\/+$/, '');
        console.log(`[INFO] 🔥 Live AI Backend URL updated to: ${activeBackendUrl}`);
        return res.json({ success: true, activeBackendUrl });
    }
    res.status(400).json({ error: "Invalid URL" });
});

// API Config Endpoint
app.get('/api/config', (req, res) => {
    res.json({
        defaultBackendUrl: activeBackendUrl,
        kaggleConfigured: !!(process.env.KAGGLE_USERNAME && process.env.KAGGLE_KEY)
    });
});

// Server-side Heartbeat Status Check (Dùng fetch an toàn, tránh 100% lỗi ERR_HTTP_HEADERS_SENT)
app.get('/api/status', async (req, res) => {
    const targetUrl = req.query.target || activeBackendUrl;
    if (!targetUrl || typeof targetUrl !== 'string' || !targetUrl.startsWith('http')) {
        return res.json({ online: false, error: 'Chưa có URL Backend nào được cấu hình' });
    }

    let checkUrl = targetUrl.replace(/\/+$/, '') + '/config';
    const t0 = Date.now();

    try {
        const response = await fetch(checkUrl, {
            method: 'GET',
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'ngrok-skip-browser-warning': 'true'
            },
            signal: AbortSignal.timeout(5000)
        });

        const latencyMs = Date.now() - t0;
        return res.json({
            online: response.ok,
            statusCode: response.status,
            latencyMs
        });
    } catch (err) {
        return res.json({
            online: false,
            error: err.name === 'TimeoutError' ? 'timeout' : err.message
        });
    }
});

// Secure Server-side Auto-Wake Endpoint (No CORS issue, No leaked credentials)
app.post('/api/wake-up', async (req, res) => {
    const username = process.env.KAGGLE_USERNAME;
    const key = process.env.KAGGLE_KEY;

    if (!username || !key) {
        return res.status(200).json({
            status: "manual_mode",
            message: "Kaggle credentials not set in Render environment. Please start Kaggle notebook manually."
        });
    }

    const kernelId = process.env.KAGGLE_KERNEL_ID || `${username}/synexan-flood-backend-gpu`;
    const postData = JSON.stringify({ id: kernelId });
    const auth = Buffer.from(`${username}:${key}`).toString('base64');

    try {
        const kaggleRes = await fetch('https://www.kaggle.com/api/v1/kernels/push', {
            method: 'POST',
            headers: {
                'Authorization': `Basic ${auth}`,
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(postData).toString()
            },
            body: postData,
            signal: AbortSignal.timeout(10000)
        });

        const data = await kaggleRes.text();
        console.log("[INFO] Kaggle API response:", kaggleRes.status, data);
        return res.json({
            status: "trigger_sent",
            statusCode: kaggleRes.status,
            message: "Auto-wake signal dispatched to Kaggle GPU."
        });
    } catch (err) {
        console.error("[ERROR] Kaggle wake request failed:", err);
        return res.status(500).json({ status: "error", error: err.message });
    }
});

// Fallback all routes to index.html for SPA
app.get('*', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, '0.0.0.0', () => {
    console.log(`🚀 Flood Demo Frontend Server running on port ${PORT}`);
    console.log(`📡 Default AI Backend target: ${activeBackendUrl || "(Chưa có URL)"}`);
});

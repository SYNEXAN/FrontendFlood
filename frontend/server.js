const express = require('express');
const path = require('path');
const cors = require('cors');
const https = require('https');

const app = express();
const PORT = process.env.PORT || 10000;
const DEFAULT_BACKEND_URL = process.env.DEFAULT_BACKEND_URL || "https://rocky-spot-tom-recognition.trycloudflare.com";
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

// Server-side Heartbeat Status Check (Tránh hoàn toàn lỗi CORS của trình duyệt)
app.get('/api/status', (req, res) => {
    const targetUrl = req.query.target || activeBackendUrl;
    let checkUrl = targetUrl;
    if (!checkUrl.endsWith('/config')) {
        checkUrl = checkUrl.replace(/\/+$/, '') + '/config';
    }

    const t0 = Date.now();
    try {
        const urlObj = new URL(checkUrl);
        const reqOptions = {
            hostname: urlObj.hostname,
            port: urlObj.port || (urlObj.protocol === 'https:' ? 443 : 80),
            path: urlObj.pathname,
            method: 'GET',
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'ngrok-skip-browser-warning': 'true'
            },
            timeout: 5000
        };

        const clientReq = https.request(reqOptions, (backendRes) => {
            const latencyMs = Date.now() - t0;
            if (backendRes.statusCode >= 200 && backendRes.statusCode < 400) {
                res.json({ online: true, statusCode: backendRes.statusCode, latencyMs });
            } else {
                res.json({ online: false, statusCode: backendRes.statusCode, latencyMs });
            }
        });

        clientReq.on('timeout', () => {
            clientReq.destroy();
            res.json({ online: false, error: 'timeout' });
        });

        clientReq.on('error', (err) => {
            res.json({ online: false, error: err.message });
        });

        clientReq.end();
    } catch (err) {
        res.json({ online: false, error: err.message });
    }
});

// Secure Server-side Auto-Wake Endpoint (No CORS issue, No leaked credentials)
app.post('/api/wake-up', (req, res) => {
    const username = process.env.KAGGLE_USERNAME;
    const key = process.env.KAGGLE_KEY;

    if (!username || !key) {
        return res.status(200).json({
            status: "manual_mode",
            message: "Kaggle credentials not set in Render environment. Please start Kaggle notebook manually."
        });
    }

    // Call Kaggle API securely
    const kernelId = process.env.KAGGLE_KERNEL_ID || `${username}/synexan-flood-backend-gpu`;
    const postData = JSON.stringify({
        id: kernelId
    });

    const auth = Buffer.from(`${username}:${key}`).toString('base64');
    const options = {
        hostname: 'www.kaggle.com',
        port: 443,
        path: '/api/v1/kernels/push',
        method: 'POST',
        headers: {
            'Authorization': `Basic ${auth}`,
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(postData)
        }
    };

    const request = https.request(options, (response) => {
        let data = '';
        response.on('data', chunk => { data += chunk; });
        response.on('end', () => {
            console.log("[INFO] Kaggle API response:", response.statusCode, data);
            res.json({
                status: "trigger_sent",
                statusCode: response.statusCode,
                message: "Auto-wake signal dispatched to Kaggle GPU."
            });
        });
    });

    request.on('error', (err) => {
        console.error("[ERROR] Kaggle wake request failed:", err);
        res.status(500).json({ status: "error", error: err.message });
    });

    request.write(postData);
    request.end();
});

// Fallback all routes to index.html for SPA
app.get('*', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, () => {
    console.log(`🚀 Flood Demo Frontend Server running on port ${PORT}`);
    console.log(`📡 Default AI Backend target: ${activeBackendUrl}`);
});

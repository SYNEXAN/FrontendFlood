const express = require('express');
const path = require('path');
const cors = require('cors');
const https = require('https');

const app = express();
const PORT = process.env.PORT || 10000;
const DEFAULT_BACKEND_URL = process.env.DEFAULT_BACKEND_URL || "https://hush-harmless-erased.ngrok-free.dev";

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// API Config Endpoint
app.get('/api/config', (req, res) => {
    res.json({
        defaultBackendUrl: DEFAULT_BACKEND_URL,
        kaggleConfigured: !!(process.env.KAGGLE_USERNAME && process.env.KAGGLE_KEY)
    });
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
    console.log(`📡 Default AI Backend target: ${DEFAULT_BACKEND_URL}`);
});

const express = require('express');
const path = require('path');
const cors = require('cors');
const fs = require('fs');

const app = express();
const PORT = process.env.PORT || 10000;
const DEFAULT_BACKEND_URL = process.env.DEFAULT_BACKEND_URL || "";
let activeBackendUrl = DEFAULT_BACKEND_URL;

// ============================================================================
// UPSTASH REDIS REST API CLIENT (Serverless Persistent Key-Value Storage)
// ============================================================================
async function getStoredBackendUrl() {
    const redisUrl = process.env.UPSTASH_REDIS_REST_URL;
    const redisToken = process.env.UPSTASH_REDIS_REST_TOKEN;
    if (redisUrl && redisToken) {
        try {
            const res = await fetch(`${redisUrl}/get/activeBackendUrl`, {
                method: 'GET',
                headers: { Authorization: `Bearer ${redisToken}` },
                signal: AbortSignal.timeout(3500)
            });
            if (res.ok) {
                const data = await res.json();
                if (data && data.result) {
                    return data.result;
                }
            }
        } catch (err) {
            console.error('[WARN] Failed to read activeBackendUrl from Upstash Redis:', err.message);
        }
    }
    return activeBackendUrl || DEFAULT_BACKEND_URL;
}

async function setStoredBackendUrl(url) {
    activeBackendUrl = url;
    const redisUrl = process.env.UPSTASH_REDIS_REST_URL;
    const redisToken = process.env.UPSTASH_REDIS_REST_TOKEN;
    if (redisUrl && redisToken) {
        try {
            await fetch(redisUrl, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${redisToken}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(['SET', 'activeBackendUrl', url, 'EX', 43200]),
                signal: AbortSignal.timeout(3500)
            });
            console.log(`[INFO] 💾 Saved activeBackendUrl to Upstash Redis: ${url}`);
        } catch (err) {
            console.error('[WARN] Failed to write activeBackendUrl to Upstash Redis:', err.message);
        }
    }
}

async function clearStoredBackendUrl() {
    activeBackendUrl = "";
    const redisUrl = process.env.UPSTASH_REDIS_REST_URL;
    const redisToken = process.env.UPSTASH_REDIS_REST_TOKEN;
    if (redisUrl && redisToken) {
        try {
            await fetch(redisUrl, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${redisToken}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(['DEL', 'activeBackendUrl']),
                signal: AbortSignal.timeout(3500)
            });
            console.log('[INFO] 🗑️ Cleared activeBackendUrl from Upstash Redis');
        } catch (err) {
            console.error('[WARN] Failed to delete activeBackendUrl from Upstash Redis:', err.message);
        }
    }
}

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Dynamic backend update endpoint (called automatically by Kaggle GPU notebook on boot)
app.post('/api/backend-url', async (req, res) => {
    const { url } = req.body;
    if (url && typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))) {
        const cleanUrl = url.trim().replace(/\/+$/, '');
        await setStoredBackendUrl(cleanUrl);
        console.log(`[INFO] 🔥 Live AI Backend URL updated to: ${cleanUrl}`);
        return res.json({ success: true, activeBackendUrl: cleanUrl });
    }
    res.status(400).json({ error: "Invalid URL" });
});

// API Config Endpoint
app.get('/api/config', async (req, res) => {
    const liveUrl = await getStoredBackendUrl();
    res.json({
        defaultBackendUrl: liveUrl,
        wakeGpuConfigured: true
    });
});

// Server-side Heartbeat Status Check
app.get('/api/status', async (req, res) => {
    const currentUrl = await getStoredBackendUrl();
    const targetUrl = req.query.target || currentUrl;
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

// Endpoint bảo mật kích hoạt GitHub Actions tự động xoay vòng & khởi động GPU Kaggle
app.post('/api/wake-gpu', async (req, res) => {
    try {
        const ghToken = process.env.GH_PAT || process.env.GITHUB_TOKEN;
        const ghRepo = process.env.GITHUB_REPOSITORY || 'SYNEXAN/FrontendFlood';

        if (!ghToken) {
            return res.status(503).json({
                success: false,
                error: 'Chưa cấu hình GH_PAT trên Environment Variables để kích hoạt GitHub Actions tự động.'
            });
        }

        const ghRes = await fetch(`https://api.github.com/repos/${ghRepo}/dispatches`, {
            method: 'POST',
            headers: {
                'Accept': 'application/vnd.github.v3+json',
                'Authorization': `Bearer ${ghToken}`,
                'User-Agent': 'FrontendFlood-Vercel-Serverless',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                event_type: 'wake-kaggle',
                client_payload: { triggered_by: 'vercel_web' }
            })
        });

        if (ghRes.status === 204 || ghRes.ok) {
            return res.json({
                success: true,
                message: 'Đã gửi tín hiệu kích hoạt GPU Backend đến GitHub Actions thành công! Hệ thống đang tự động kiểm tra quota và khởi động GPU.'
            });
        } else {
            const errBody = await ghRes.text();
            return res.status(ghRes.status).json({
                success: false,
                error: `GitHub API trả về mã ${ghRes.status}: ${errBody}`
            });
        }
    } catch (err) {
        return res.status(500).json({ success: false, error: err.message });
    }
});

// Endpoint chủ động tắt máy chủ GPU tức thì theo yêu cầu người dùng
app.post('/api/backend-shutdown', async (req, res) => {
    try {
        const currentUrl = await getStoredBackendUrl();
        if (currentUrl) {
            console.log(`[INFO] 🛑 Đang gửi tín hiệu dừng máy chủ GPU tới: ${currentUrl}/shutdown`);
            await fetch(`${currentUrl}/shutdown`, {
                method: 'POST',
                signal: AbortSignal.timeout(3500)
            }).catch(() => {});
        }
        await clearStoredBackendUrl();
        return res.json({ success: true, message: "Đã gửi tín hiệu tắt máy chủ GPU thành công." });
    } catch (err) {
        return res.status(500).json({ error: err.message });
    }
});

// Fallback all routes to index.html for SPA
app.get('*', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

if (process.env.NODE_ENV !== 'test' && !process.env.VERCEL) {
    app.listen(PORT, '0.0.0.0', () => {
        console.log(`🚀 Flood Demo Frontend Server running on port ${PORT}`);
        console.log(`📡 Default AI Backend target: ${DEFAULT_BACKEND_URL || 'Upstash Redis Managed'}`);
    });
}

module.exports = app;

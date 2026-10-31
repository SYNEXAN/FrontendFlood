const express = require('express');
const path = require('path');
const cors = require('cors');
const https = require('https');
const fs = require('fs');

const app = express();
const PORT = process.env.PORT || 10000;
const DEFAULT_BACKEND_URL = process.env.DEFAULT_BACKEND_URL || "https://mask-rica-categories-lined.trycloudflare.com";
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

// Helper to make HTTPS requests returning a Promise
function kaggleApiRequest(options, postBody = null) {
    return new Promise((resolve, reject) => {
        const req = https.request(options, (res) => {
            let data = '';
            res.on('data', chunk => { data += chunk; });
            res.on('end', () => {
                let json = null;
                try { json = JSON.parse(data); } catch(e) {}
                resolve({ statusCode: res.statusCode, headers: res.headers, raw: data, json });
            });
        });
        req.on('error', err => reject(err));
        req.on('timeout', () => { req.destroy(); reject(new Error('Kaggle API Timeout')); });
        if (postBody) {
            req.write(postBody);
        }
        req.end();
    });
}

// Smart parser for user input (supports kaggle.json, KGAT token, username:key, or raw key)
async function resolveKaggleAuth(authInput) {
    let username = '';
    let key = '';
    let token = '';

    if (typeof authInput === 'object' && authInput !== null) {
        username = authInput.username || '';
        key = authInput.key || '';
        token = authInput.token || '';
    } else if (typeof authInput === 'string') {
        const trimmed = authInput.trim();
        if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
            try {
                const parsed = JSON.parse(trimmed);
                username = parsed.username || '';
                key = parsed.key || '';
                token = parsed.token || '';
            } catch (e) {}
        } else if (trimmed.startsWith('KGAT_')) {
            token = trimmed;
        } else if (trimmed.includes(':') && !trimmed.startsWith('http')) {
            const parts = trimmed.split(':');
            username = parts[0].trim();
            key = parts.slice(1).join(':').trim();
        } else if (trimmed.length === 32) {
            key = trimmed;
        } else {
            token = trimmed;
        }
    }

    let authHeader = '';
    if (token) {
        authHeader = `Bearer ${token}`;
    } else if (username && key) {
        const basic = Buffer.from(`${username}:${key}`).toString('base64');
        authHeader = `Basic ${basic}`;
    } else {
        throw new Error('Vui lòng dán file kaggle.json hoặc nhập Kaggle Token (KGAT_...).');
    }

    // If username is not known (e.g. user provided KGAT token), discover it from Kaggle API
    if (!username) {
        const listBody = JSON.stringify({ group: 'PROFILE', pageSize: 5 });
        const listRes = await kaggleApiRequest({
            hostname: 'api.kaggle.com',
            port: 443,
            path: '/v1/kernels.KernelsApiService/ListKernels',
            method: 'POST',
            headers: {
                'Authorization': authHeader,
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(listBody)
            },
            timeout: 10000
        }, listBody);

        if (listRes.json && Array.isArray(listRes.json.kernels) && listRes.json.kernels.length > 0) {
            const ref = listRes.json.kernels[0].ref || '';
            username = ref.split('/')[0];
        } else {
            throw new Error('Xác thực Token thành công nhưng tài khoản chưa có Kernel để tự nhận diện Username. Vui lòng dán nội dung file kaggle.json hoặc nhập username:key.');
        }
    }

    return { username, key, token, authHeader };
}

// Endpoint kiểm tra nhanh tính hợp lệ của Token/Kaggle Key
app.post('/api/kaggle-check', async (req, res) => {
    try {
        const { authInput } = req.body;
        if (!authInput) {
            return res.status(400).json({ error: 'Chưa cung cấp thông tin xác thực.' });
        }
        const auth = await resolveKaggleAuth(authInput);
        res.json({
            success: true,
            username: auth.username,
            message: `Xác thực thành công tài khoản Kaggle: ${auth.username}`
        });
    } catch (err) {
        res.status(400).json({ error: err.message });
    }
});

// Endpoint tự động tạo & chạy notebook trên tài khoản Kaggle mới
app.post('/api/kaggle-auto-run', async (req, res) => {
    try {
        const { authInput } = req.body;
        if (!authInput) {
            return res.status(400).json({ error: 'Chưa cung cấp thông tin xác thực Kaggle.' });
        }

        const auth = await resolveKaggleAuth(authInput);
        console.log(`[INFO] 🚀 Khởi chạy GPU Tesla T4 cho tài khoản: ${auth.username}`);

        // Read official notebook
        const nbPath = path.join(__dirname, 'public', 'synexan_flood_gpu.ipynb');
        let rawContent = '';
        if (fs.existsSync(nbPath)) {
            rawContent = fs.readFileSync(nbPath, 'utf8');
        } else {
            const fallbackPath = path.join(__dirname, '..', 'notebooks', 'synexan_flood_gpu.ipynb');
            rawContent = fs.readFileSync(fallbackPath, 'utf8');
        }

        const nb = JSON.parse(rawContent);
        if (Array.isArray(nb.cells)) {
            nb.cells.forEach(cell => {
                if (cell.cell_type === 'code') {
                    cell.outputs = [];
                    cell.execution_count = null;
                }
                if (Array.isArray(cell.source)) {
                    cell.source = cell.source.join('');
                }
            });
        }

        const kernelSlug = `${auth.username}/synexan-flood-backend-gpu`;
        const savePayload = JSON.stringify({
            slug: kernelSlug,
            newTitle: "synexan-flood-backend-gpu",
            text: JSON.stringify(nb),
            language: "python",
            kernelType: "notebook",
            isPrivate: true,
            enableGpu: true,
            enableInternet: true
        });

        const kaggleRes = await kaggleApiRequest({
            hostname: 'api.kaggle.com',
            port: 443,
            path: '/v1/kernels.KernelsApiService/SaveKernel',
            method: 'POST',
            headers: {
                'Authorization': auth.authHeader,
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(savePayload)
            },
            timeout: 30000
        }, savePayload);

        if (kaggleRes.statusCode >= 200 && kaggleRes.statusCode < 300) {
            const ver = kaggleRes.json ? kaggleRes.json.versionNumber : 1;
            return res.json({
                success: true,
                username: auth.username,
                kernelSlug: kernelSlug,
                versionNumber: ver,
                message: `Đã kích hoạt GPU Tesla T4 thành công cho tài khoản ${auth.username}! (Phiên bản #${ver})`
            });
        } else {
            const errMsg = kaggleRes.json ? (kaggleRes.json.message || kaggleRes.json.error) : kaggleRes.raw;
            return res.status(kaggleRes.statusCode || 500).json({
                error: `Kaggle API lỗi (HTTP ${kaggleRes.statusCode}): ${errMsg || 'Không xác định'}`
            });
        }
    } catch (err) {
        console.error('[ERROR] Kaggle auto-run failed:', err);
        res.status(500).json({ error: err.message });
    }
});

// Fallback all routes to index.html for SPA
app.get('*', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, () => {
    console.log(`🚀 Flood Demo Frontend Server running on port ${PORT}`);
    console.log(`📡 Default AI Backend target: ${activeBackendUrl}`);
});

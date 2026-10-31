const express = require('express');
const path = require('path');
const cors = require('cors');
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

// Server-side Heartbeat Status Check
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
        const listRes = await fetch('https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernels', {
            method: 'POST',
            headers: {
                'Authorization': authHeader,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ group: 'PROFILE', pageSize: 5 }),
            signal: AbortSignal.timeout(10000)
        });

        if (listRes.ok) {
            const listData = await listRes.json();
            if (listData && Array.isArray(listData.kernels) && listData.kernels.length > 0) {
                const ref = listData.kernels[0].ref || '';
                username = ref.split('/')[0];
            }
        }

        if (!username) {
            throw new Error('Xác thực Token thành công nhưng tài khoản chưa có Kernel để tự nhận diện Username. Vui lòng dán nội dung file kaggle.json hoặc nhập theo dạng username:key.');
        }
    }

    return { username, key, token, authHeader };
}

// Helper kiểm tra trạng thái notebook trên Kaggle qua các ứng viên slug tương thích
async function checkKernelStatus(auth) {
    const candidates = [
        { userName: 'nguynthanhthit', kernelSlug: 'synexan-flood-backend-gpu' }
    ];
    if (auth.username && auth.username !== 'nguynthanhthit') {
        candidates.push({ userName: auth.username, kernelSlug: 'synexan-flood-backend-gpu' });
    }

    for (const cand of candidates) {
        try {
            const statusRes = await fetch('https://api.kaggle.com/v1/kernels.KernelsApiService/GetKernelSessionStatus', {
                method: 'POST',
                headers: {
                    'Authorization': auth.authHeader,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(cand),
                signal: AbortSignal.timeout(8000)
            });
            if (statusRes.ok) {
                const statusData = await statusRes.json();
                if (statusData && statusData.status) {
                    return { status: statusData.status, matchedCandidate: cand };
                }
            }
        } catch (_) {}
    }
    return { status: 'NOT_FOUND', matchedCandidate: candidates[0] };
}

// Endpoint kiểm tra tính hợp lệ của Token/Key (Ẩn tên định danh, chỉ thông báo hợp lệ)
app.post('/api/kaggle-check', async (req, res) => {
    try {
        const { authInput } = req.body;
        if (!authInput) {
            return res.status(400).json({ error: 'Chưa cung cấp thông tin xác thực.' });
        }
        const auth = await resolveKaggleAuth(authInput);
        return res.json({
            success: true,
            message: 'Xác thực API Kaggle thành công!'
        });
    } catch (err) {
        return res.status(400).json({ error: err.message });
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

        // 1. Kiểm tra trạng thái phiên làm việc hiện tại trên Kaggle để chống kích hoạt trùng lặp
        const { status: currentStatus } = await checkKernelStatus(auth);
        console.log(`[INFO] Trạng thái phiên Kaggle hiện tại:`, currentStatus);

        if (currentStatus === 'RUNNING') {
            return res.json({
                success: true,
                alreadyRunning: true,
                status: 'RUNNING',
                message: 'GPU Tesla T4 đang hoạt động (Online). Đang kết nối trực tiếp...'
            });
        } else if (currentStatus === 'QUEUED') {
            return res.json({
                success: true,
                alreadyQueued: true,
                status: 'QUEUED',
                message: 'Notebook đang trong hàng đợi GPU của Kaggle. Đang chờ hệ thống cấp phát...'
            });
        }

        // 2. Nếu chưa chạy -> Đẩy notebook lên tài khoản và kích hoạt GPU
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

        const targetUser = auth.username || 'nguynthanhthit';
        const kernelSlug = `${targetUser}/synexan-flood-backend-gpu`;

        const savePayload = JSON.stringify({
            slug: kernelSlug,
            newTitle: 'synexan-flood-backend-gpu',
            text: JSON.stringify(nb),
            language: 'python',
            kernelType: 'notebook',
            isPrivate: true,
            enableGpu: true,
            enableInternet: true
        });

        const kaggleRes = await fetch('https://api.kaggle.com/v1/kernels.KernelsApiService/SaveKernel', {
            method: 'POST',
            headers: {
                'Authorization': auth.authHeader,
                'Content-Type': 'application/json'
            },
            body: savePayload,
            signal: AbortSignal.timeout(30000)
        });

        if (kaggleRes.ok) {
            const data = await kaggleRes.json();
            const ver = data.versionNumber || 1;
            return res.json({
                success: true,
                versionNumber: ver,
                message: `Đã kích hoạt GPU Tesla T4 thành công! (Phiên bản #${ver}). Đang chuẩn bị luồng...`
            });
        } else {
            const errText = await kaggleRes.text();
            return res.status(kaggleRes.status).json({
                error: `Kaggle API lỗi (HTTP ${kaggleRes.status}): ${errText || 'Không xác định'}`
            });
        }
    } catch (err) {
        console.error('[ERROR] Kaggle auto-run failed:', err);
        return res.status(500).json({ error: err.message });
    }
});

// Endpoint kiểm tra trạng thái phiên làm việc Kaggle (RUNNING, QUEUED, COMPLETE...)
app.post('/api/kaggle-status', async (req, res) => {
    try {
        const { authInput } = req.body;
        if (!authInput) {
            return res.status(400).json({ error: 'Chưa cung cấp thông tin xác thực.' });
        }
        const auth = await resolveKaggleAuth(authInput);
        const { status } = await checkKernelStatus(auth);
        return res.json({
            success: true,
            status: status
        });
    } catch (err) {
        return res.status(500).json({ error: err.message });
    }
});

// Endpoint chủ động tắt máy chủ GPU tức thì theo yêu cầu người dùng
app.post('/api/backend-shutdown', async (req, res) => {
    try {
        if (activeBackendUrl) {
            console.log(`[INFO] 🛑 Đang gửi tín hiệu dừng máy chủ GPU tới: ${activeBackendUrl}/shutdown`);
            await fetch(`${activeBackendUrl}/shutdown`, {
                method: 'POST',
                signal: AbortSignal.timeout(3500)
            }).catch(() => {});
        }
        activeBackendUrl = "";
        return res.json({ success: true, message: "Đã gửi tín hiệu tắt máy chủ GPU thành công." });
    } catch (err) {
        return res.status(500).json({ error: err.message });
    }
});

// Fallback all routes to index.html for SPA
app.get('*', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, '0.0.0.0', () => {
    console.log(`🚀 Flood Demo Frontend Server running on port ${PORT}`);
    console.log(`📡 Default AI Backend target: ${activeBackendUrl}`);
});
